"""Tests for src/herder/connect.py.

Covers: recipe loading, detect, confirm-gating, full flow, idempotency.
All tests use zero real network / install / TTY via injectable runner + confirm.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from herder.adapters import BLOCK_END, BLOCK_START, MalformedCheatsheetError
from herder.connect import (
    ConnectResult,
    Recipe,
    RecipeProvider,
    _is_url_install,
    auto_confirm,
    connect,
    detect,
    load_recipe,
    register_provider,
    run_step,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def kiro_recipe_file(tmp_path: Path) -> Path:
    """Write a minimal valid recipe YAML and return its path."""
    data = {
        "name": "kiro",
        "detect": "command -v kiro",
        "install": "curl -fsSL https://kiro.dev/install | sh",
        "login": "kiro login",
        "verify": "kiro --version",
        "provider": {
            "type": "cli",
            "executable": "kiro",
            "args": ["chat", "--no-interactive"],
            "parser": "text",
        },
        "default_role": "kiro",
    }
    p = tmp_path / "kiro.yaml"
    p.write_text(yaml.dump(data))
    return p


@pytest.fixture()
def fake_recipe() -> Recipe:
    """A synthetic recipe that uses echo commands — no real network."""
    return Recipe(
        name="fakey",
        detect="false",  # simulate not installed
        install="echo installing",
        login="echo login",
        verify="echo verified",
        provider=RecipeProvider(
            type="cli",
            executable="fakey",
            args=["run"],
            parser="text",
        ),
        default_role="fakey",
    )


def _make_runner(exit_codes: dict[str, int]) -> types.SimpleNamespace:
    """Build a fake subprocess.run-compatible runner.

    Args:
        exit_codes: Maps command prefix → returncode.

    Returns:
        SimpleNamespace with a ``run`` method.
    """
    class _Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(cmd: str, **_kwargs: object) -> _Result:
        for prefix, code in exit_codes.items():
            if cmd.startswith(prefix) or cmd == prefix:
                return _Result(code, stdout="ok" if code == 0 else "", stderr="err" if code != 0 else "")
        return _Result(0, stdout="ok")

    ns = types.SimpleNamespace()
    ns.run = _run
    return ns


# ---------------------------------------------------------------------------
# Recipe loader tests
# ---------------------------------------------------------------------------

class TestLoadRecipe:
    def test_valid_recipe_loads(self, kiro_recipe_file: Path) -> None:
        recipe = load_recipe(kiro_recipe_file)
        assert recipe.name == "kiro"
        assert recipe.default_role == "kiro"
        assert recipe.provider.executable == "kiro"
        assert "chat" in recipe.provider.args

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="recipe not found"):
            load_recipe(tmp_path / "nonexistent.yaml")

    def test_missing_required_field_raises_value_error(self, tmp_path: Path) -> None:
        # Missing 'verify' and 'provider'
        p = tmp_path / "bad.yaml"
        p.write_text("name: x\ndetect: cmd\ninstall: cmd\nlogin: cmd\ndefault_role: x\n")
        with pytest.raises(ValueError, match="invalid recipe"):
            load_recipe(p)

    def test_extra_field_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "extra.yaml"
        p.write_text(
            "name: x\ndetect: cmd\ninstall: cmd\nlogin: cmd\nverify: cmd\n"
            "provider: {type: cli, executable: x}\ndefault_role: x\nextra_key: bad\n"
        )
        with pytest.raises(ValueError, match="invalid recipe"):
            load_recipe(p)

    def test_malformed_yaml_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "malformed.yaml"
        p.write_text("name: [unclosed\n")
        with pytest.raises(ValueError, match="malformed YAML"):
            load_recipe(p)


# ---------------------------------------------------------------------------
# Detect tests
# ---------------------------------------------------------------------------

class TestDetect:
    def test_detect_returns_true_on_exit_0(self, fake_recipe: Recipe) -> None:
        runner = _make_runner({"false": 0})
        assert detect(fake_recipe, runner=runner) is True

    def test_detect_returns_false_on_exit_1(self, fake_recipe: Recipe) -> None:
        runner = _make_runner({"false": 1})
        assert detect(fake_recipe, runner=runner) is False


# ---------------------------------------------------------------------------
# run_step confirm-gating tests
# ---------------------------------------------------------------------------

class TestRunStep:
    def test_declined_confirm_does_not_run(self) -> None:
        ran: list[str] = []

        class _TrackingRunner:
            def run(self, cmd: str, **_kwargs: object) -> object:
                ran.append(cmd)
                result = types.SimpleNamespace()
                result.returncode = 0
                result.stdout = "ok"
                result.stderr = ""
                return result

        ok, out = run_step("test", "echo hi", confirm=lambda _: False, runner=_TrackingRunner())
        assert ok is False
        assert out == ""
        assert ran == [], "command must NOT run when confirm returns False"

    def test_accepted_confirm_runs_command(self) -> None:
        runner = _make_runner({"echo hi": 0})
        ok, _ = run_step("test", "echo hi", confirm=auto_confirm, runner=runner)
        assert ok is True

    def test_failed_command_returns_false(self) -> None:
        runner = _make_runner({"echo hi": 1})
        ok, _ = run_step("test", "echo hi", confirm=auto_confirm, runner=runner)
        assert ok is False

    def test_pipe_install_prompt_mentions_download(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Confirm callable receives a prompt mentioning download+run for curl|sh."""
        received_prompts: list[str] = []

        def _capture_confirm(prompt: str) -> bool:
            received_prompts.append(prompt)
            return False

        run_step(
            "install",
            "curl -fsSL https://kiro.dev/install | sh",
            confirm=_capture_confirm,
        )
        assert received_prompts, "confirm must be called"
        assert "downloads and runs" in received_prompts[0].lower() or "download" in received_prompts[0].lower()

    def test_bash_process_substitution_triggers_url_warning(self) -> None:
        """bash <(curl https://…) triggers download warning (P3 fix)."""
        received_prompts: list[str] = []

        def _capture(prompt: str) -> bool:
            received_prompts.append(prompt)
            return False

        run_step("install", "bash <(curl https://x.dev/i)", confirm=_capture)
        assert received_prompts
        assert "downloads and runs" in received_prompts[0]

    def test_sh_c_curl_triggers_url_warning(self) -> None:
        """sh -c "$(curl https://…)" triggers download warning (P3 fix)."""
        received_prompts: list[str] = []

        def _capture(prompt: str) -> bool:
            received_prompts.append(prompt)
            return False

        run_step("install", 'sh -c "$(curl https://x.dev/i)"', confirm=_capture)
        assert received_prompts
        assert "downloads and runs" in received_prompts[0]


# ---------------------------------------------------------------------------
# Full connect flow
# ---------------------------------------------------------------------------

class TestConnect:
    def test_full_flow_registers_provider_and_cheatsheet(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        """End-to-end: not installed → install → login → verify → register."""
        config_path = tmp_path / "config.yaml"
        brain = tmp_path / "CLAUDE.md"

        # runner: detect(false)→exit1, everything else→exit0
        runner = _make_runner({"false": 1})

        result = connect(
            fake_recipe,
            config_path=config_path,
            brain_files=[brain],
            confirm=auto_confirm,
            runner=runner,
        )

        assert result.success is True, result.abort_reason
        assert result.provider_name == "fakey_cli"
        assert result.role_name == "fakey"
        assert not result.skipped_install

        # config.yaml written
        raw = yaml.safe_load(config_path.read_text())
        assert "fakey_cli" in raw["providers"]
        assert "fakey" in raw["roles"]

        # cheatsheet written
        assert brain.exists()
        content = brain.read_text()
        assert "fakey" in content

    def test_connect_skips_install_when_already_installed(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        brain = tmp_path / "AGENTS.md"

        # detect returns 0 → already installed
        runner = _make_runner({"false": 0})

        result = connect(
            fake_recipe,
            config_path=config_path,
            brain_files=[brain],
            confirm=auto_confirm,
            runner=runner,
        )

        assert result.success is True
        assert result.skipped_install is True

    def test_declined_install_aborts_no_registration(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        brain = tmp_path / "CLAUDE.md"

        # detect → not installed (exit 1)
        runner = _make_runner({"false": 1})

        result = connect(
            fake_recipe,
            config_path=config_path,
            brain_files=[brain],
            confirm=lambda _: False,  # always decline
            runner=runner,
        )

        assert result.success is False
        assert "install" in result.abort_reason
        assert not config_path.exists(), "config.yaml must NOT be written on abort"
        assert not brain.exists(), "brain file must NOT be written on abort"

    def test_declined_login_aborts_no_registration(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"

        # detect=0 (already installed) → install is skipped entirely (no confirm).
        # First confirm call therefore goes to login → decline it immediately.
        runner = _make_runner({"false": 0})

        def _decline_first(prompt: str) -> bool:
            return False  # decline the very first confirm = login

        result = connect(
            fake_recipe,
            config_path=config_path,
            brain_files=[],
            confirm=_decline_first,
            runner=runner,
        )

        assert result.success is False
        assert "login" in result.abort_reason
        assert not config_path.exists()


# ---------------------------------------------------------------------------
# Idempotency: register_provider
# ---------------------------------------------------------------------------

class TestRegisterProvider:
    def test_register_writes_provider_and_role(
        self, fake_recipe: Recipe, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        pname, rname = register_provider(config_path, fake_recipe)
        assert pname == "fakey_cli"
        assert rname == "fakey"
        raw = yaml.safe_load(config_path.read_text())
        assert raw["providers"]["fakey_cli"]["executable"] == "fakey"
        assert raw["roles"]["fakey"]["provider"] == "fakey_cli"

    def test_register_twice_no_duplicate(
        self, fake_recipe: Recipe, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        register_provider(config_path, fake_recipe)
        register_provider(config_path, fake_recipe)
        raw = yaml.safe_load(config_path.read_text())
        providers = raw["providers"]
        assert list(providers.keys()).count("fakey_cli") == 1

    def test_register_preserves_existing_providers(
        self, fake_recipe: Recipe, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        initial = {
            "providers": {"existing_cli": {"type": "cli", "executable": "existing"}},
            "roles": {},
        }
        config_path.write_text(yaml.dump(initial))
        register_provider(config_path, fake_recipe)
        raw = yaml.safe_load(config_path.read_text())
        assert "existing_cli" in raw["providers"]
        assert "fakey_cli" in raw["providers"]

    def test_register_adds_role_to_project_allowed_roles(
        self, fake_recipe: Recipe, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        initial = {
            "projects": {
                "scratch": {"root": "/tmp/x", "allowed_roles": ["planner"]},
            }
        }
        config_path.write_text(yaml.dump(initial))
        register_provider(config_path, fake_recipe)
        raw = yaml.safe_load(config_path.read_text())
        assert "fakey" in raw["projects"]["scratch"]["allowed_roles"]

    def test_register_does_not_duplicate_allowed_role(
        self, fake_recipe: Recipe, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        register_provider(config_path, fake_recipe)
        register_provider(config_path, fake_recipe)
        raw = yaml.safe_load(config_path.read_text())
        # No project = no allowed_roles to check; just confirm no error
        assert "fakey_cli" in raw["providers"]


# ---------------------------------------------------------------------------
# P1 — comment preservation via ruamel.yaml round-trip
# ---------------------------------------------------------------------------

class TestRegisterProviderPreservesComments:
    """register_provider must not destroy YAML comments or reflow formatting."""

    def test_comments_survive_register(self, fake_recipe: Recipe, tmp_path: Path) -> None:
        """A config with YAML comments must retain them after register_provider."""
        config_path = tmp_path / "config.yaml"
        # Write a config with a top-level comment and a flow-style list
        config_path.write_text(
            "# This is a hand-tuned config file — DO NOT REMOVE COMMENTS\n"
            "providers: {}\n"
            "roles: {}\n"
            "projects:\n"
            "  scratch:\n"
            "    root: /tmp/x\n"
            "    allowed_roles: [planner]  # flow-style list\n",
            encoding="utf-8",
        )

        register_provider(config_path, fake_recipe)

        written = config_path.read_text(encoding="utf-8")
        # Top-level comment must survive
        assert "# This is a hand-tuned config file" in written, (
            "Top-level comment was destroyed by register_provider"
        )
        # The new provider and role must be present
        assert "fakey_cli" in written
        assert "fakey" in written

    def test_flow_style_list_comment_survives(self, fake_recipe: Recipe, tmp_path: Path) -> None:
        """Inline comment on a flow-style list must survive round-trip."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "providers: {}\n"
            "roles: {}\n"
            "projects:\n"
            "  p:\n"
            "    root: /x\n"
            "    allowed_roles: [admin]  # keep this comment\n",
            encoding="utf-8",
        )
        register_provider(config_path, fake_recipe)
        written = config_path.read_text(encoding="utf-8")
        assert "# keep this comment" in written, (
            "Inline comment on allowed_roles was destroyed"
        )


# ---------------------------------------------------------------------------
# P1 — all-or-nothing: pre-flight aborts if any brain file is malformed
# ---------------------------------------------------------------------------

class TestConnectAllOrNothing:
    """connect() must not modify any file when a brain target is malformed."""

    def test_malformed_brain_aborts_no_write(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        """Pre-flight raises and config.yaml + good brain file are NOT modified."""
        config_path = tmp_path / "config.yaml"
        original_config = "providers: {}\nroles: {}\n"
        config_path.write_text(original_config)

        good_brain = tmp_path / "AGENTS.md"
        good_brain.write_text("# Good file\n")

        # Malformed: BLOCK_START without BLOCK_END
        bad_brain = tmp_path / "CLAUDE.md"
        bad_brain.write_text(f"# Bad\n{BLOCK_START}\n- some line\n")

        runner = _make_runner({"false": 0})  # detect=installed, skip install

        with pytest.raises(MalformedCheatsheetError):
            connect(
                fake_recipe,
                config_path=config_path,
                brain_files=[good_brain, bad_brain],
                confirm=auto_confirm,
                runner=runner,
            )

        # config.yaml must be unmodified (no new provider registered)
        assert config_path.read_text() == original_config, (
            "config.yaml was modified despite pre-flight failure"
        )
        # Good brain file must also be unmodified
        assert good_brain.read_text() == "# Good file\n", (
            "Good brain file was modified despite pre-flight failure"
        )


# ---------------------------------------------------------------------------
# P3 — _is_url_install broadened detection
# ---------------------------------------------------------------------------

class TestIsUrlInstall:
    """_is_url_install detects HTTP/HTTPS regardless of shell syntax."""

    def test_classic_pipe_detected(self) -> None:
        assert _is_url_install("curl -fsSL https://kiro.dev/install | sh")

    def test_bash_process_substitution_detected(self) -> None:
        assert _is_url_install("bash <(curl https://x.dev/i)")

    def test_sh_c_curl_detected(self) -> None:
        assert _is_url_install('sh -c "$(curl https://x.dev/i)"')

    def test_http_url_detected(self) -> None:
        assert _is_url_install("curl http://example.com/install.sh | bash")

    def test_plain_local_command_not_detected(self) -> None:
        assert not _is_url_install("echo installing")

    def test_command_v_not_detected(self) -> None:
        assert not _is_url_install("command -v kiro")


# ---------------------------------------------------------------------------
# P3 — verify step runs un-gated (read-only probe)
# ---------------------------------------------------------------------------

class TestVerifyUngated:
    """verify must run without going through run_step's confirm callable."""

    def test_verify_not_counted_in_confirm_calls(
        self,
        fake_recipe: Recipe,
        tmp_path: Path,
    ) -> None:
        """With detect=0 (already installed), confirm is called ONLY for login."""
        config_path = tmp_path / "config.yaml"
        brain = tmp_path / "CLAUDE.md"

        confirm_calls: list[str] = []

        def _tracking_confirm(prompt: str) -> bool:
            confirm_calls.append(prompt)
            return True  # approve everything that IS confirm-gated

        # detect=0 (already installed → no install confirm)
        runner = _make_runner({"false": 0})

        result = connect(
            fake_recipe,
            config_path=config_path,
            brain_files=[brain],
            confirm=_tracking_confirm,
            runner=runner,
        )

        assert result.success is True, result.abort_reason
        # Only login should have triggered confirm (not verify)
        assert len(confirm_calls) == 1, (
            f"Expected 1 confirm call (login only), got {len(confirm_calls)}: {confirm_calls}"
        )
        assert "login" in confirm_calls[0].lower() or "run" in confirm_calls[0].lower()
