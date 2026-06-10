"""Tests for provider health probing (doctor module)."""
from pathlib import Path

from herder.config import Config, Provider
from herder.doctor import probe_provider
from herder.models import Result
from herder.providers import ollama_http
from herder.services.doctor import integrity_warnings


def test_probe_ok(tmp_path: Path) -> None:
    """Verify that a working provider is classified as ok."""
    p = Provider(type="cli", executable="cat", args=[], input="stdin", timeout=5)
    h = probe_provider("fake", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "ok"
    assert h.auth_status == "ok"


def test_probe_missing_binary(tmp_path: Path) -> None:
    """Verify that missing binary is classified as fail/missing."""
    p = Provider(type="cli", executable="no-such-bin-xyz", input="stdin", timeout=5)
    h = probe_provider("missing", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "fail"
    assert h.auth_status == "missing"


def test_probe_detects_prompted(tmp_path: Path) -> None:
    """Verify that login-like output is flagged as prompted."""
    # printf with string argument emits the string and exits 0
    p = Provider(
        type="cli",
        executable="printf",
        args=["Please sign in with your API key"],
        input="arg",
        timeout=5,
    )
    h = probe_provider("prompted", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "prompted"
    assert h.auth_status in ("missing", "expired")


def test_probe_detects_prompted_on_stderr(tmp_path: Path) -> None:
    """Verify that login prompts on stderr are detected."""
    # sh -c with 2>&1 redirect: write to stderr and exit 0
    p = Provider(
        type="cli",
        executable="sh",
        args=["-c", "echo 'please sign in' >&2"],
        input="stdin",
        timeout=5,
    )
    h = probe_provider("stderr_prompt", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "prompted"
    assert h.auth_status in ("missing", "expired")


def test_probe_ollama_ok(monkeypatch, tmp_path: Path) -> None:
    """Verify that ollama provider responds ok when server is reachable."""

    def mock_run(provider: Provider, prompt: str, *, timeout: int) -> Result:
        return Result("done", 0, output="OK")

    monkeypatch.setattr(ollama_http, "run", mock_run)
    p = Provider(type="ollama", base_url="http://x:11434", model="m")
    h = probe_provider("ol", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "ok"
    assert h.auth_status == "ok"


def test_probe_ollama_unreachable(monkeypatch, tmp_path: Path) -> None:
    """Verify that ollama provider reports fail/missing when unreachable."""

    def mock_run(provider: Provider, prompt: str, *, timeout: int) -> Result:
        return Result("failed", -1, error_type="unavailable")

    monkeypatch.setattr(ollama_http, "run", mock_run)
    p = Provider(type="ollama", base_url="http://x:11434", model="m")
    h = probe_provider("ol", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "fail"
    assert h.auth_status == "missing"


def test_probe_ollama_timeout(monkeypatch, tmp_path: Path) -> None:
    """Verify that ollama provider reports tty_required on timeout."""

    def mock_run(provider: Provider, prompt: str, *, timeout: int) -> Result:
        return Result("timeout", -1, error_type="timeout")

    monkeypatch.setattr(ollama_http, "run", mock_run)
    p = Provider(type="ollama", base_url="http://x:11434", model="m")
    h = probe_provider("ol", p, env={}, cwd=tmp_path)
    assert h.noninteractive_status == "tty_required"
    assert h.auth_status == "unknown"


def test_integrity_warns_world_writable_config(tmp_path: Path) -> None:
    """Verify that integrity_warnings detects group/world-writable config files.

    Args:
        tmp_path: Temporary directory for test config file.
    """
    c = tmp_path / "c.yaml"
    c.write_text("providers:\n  echo: {type: cli, executable: /bin/cat, input: stdin}\n"
                 "roles: {r: {provider: echo}}\nworker: {global_concurrency: 1}\n")
    import os
    os.chmod(c, 0o666)  # Make world-writable
    from herder.config import load_config
    cfg = load_config(str(c))
    warns = integrity_warnings(cfg, str(c))
    assert any("group/world-writable" in w for w in warns), f"Expected warning about config, got: {warns}"


def test_integrity_warns_world_writable_executable(tmp_path: Path) -> None:
    """Verify that integrity_warnings detects group/world-writable provider executables.

    Args:
        tmp_path: Temporary directory for test.
    """
    exe = tmp_path / "fake_provider"
    exe.write_text("#!/bin/sh\necho OK\n")
    exe.chmod(0o777)  # Make world-writable

    c = tmp_path / "c.yaml"
    c.write_text(f"providers:\n  bad: {{type: cli, executable: {exe}, input: stdin}}\n"
                 "roles: {r: {provider: bad}}\nworker: {global_concurrency: 1}\n")
    from herder.config import load_config
    cfg = load_config(str(c))
    warns = integrity_warnings(cfg)
    assert any("world-writable" in w and "executable" in w for w in warns), \
        f"Expected warning about executable, got: {warns}"


def test_integrity_no_warns_when_secure(tmp_path: Path) -> None:
    """Verify that integrity_warnings returns empty when files are secure.

    Args:
        tmp_path: Temporary directory for test.
    """
    exe = tmp_path / "safe_provider"
    exe.write_text("#!/bin/sh\necho OK\n")
    exe.chmod(0o755)  # Owner-only writable

    c = tmp_path / "c.yaml"
    c.write_text(f"providers:\n  good: {{type: cli, executable: {exe}, input: stdin}}\n"
                 "roles: {r: {provider: good}}\nworker: {global_concurrency: 1}\n")
    c.chmod(0o600)  # Owner-only readable/writable
    from herder.config import load_config
    cfg = load_config(str(c))
    warns = integrity_warnings(cfg, str(c))
    assert len(warns) == 0, f"Expected no warnings for secure files, got: {warns}"
