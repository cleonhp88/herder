"""Tests for LocalRuntime (Phase 3 — Task 3.1 + 3.2)."""
from __future__ import annotations

from pathlib import Path

from herder.permissions import Permissions
from herder.runtimes.local import LocalRuntime


def test_local_runtime_runs_command(tmp_path: Path) -> None:
    rt = LocalRuntime()
    assert rt.name == "local"
    res = rt.run(
        ["/bin/sh", "-c", "printf hi"],
        prompt="",
        cwd=tmp_path,
        timeout=10,
        env={},
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel_check=None,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=Permissions.from_json("{}"),
    )
    assert res.status == "done"
    assert "hi" in res.output


def test_local_runtime_non_cancellable_path(tmp_path: Path) -> None:
    # cancel_check None + no paths → simple runner path still works
    rt = LocalRuntime()
    res = rt.run(
        ["/bin/sh", "-c", "printf x"],
        prompt="",
        cwd=tmp_path,
        timeout=10,
        env={},
        stdout_path=None,
        stderr_path=None,
        cancel_check=None,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=Permissions.from_json("{}"),
    )
    assert res.status == "done"


def test_local_runtime_wraps_sandbox_when_profile_given(tmp_path: Path, monkeypatch) -> None:
    # When sandbox_profile is set, argv must be wrapped via sandbox.wrap.
    rt = LocalRuntime()
    seen: dict[str, list[str]] = {}
    import herder.runtimes.local as mod

    def fake_run_with_terminate(argv: list[str], **kw: object) -> object:
        seen["argv"] = argv
        from herder.models import Result
        return Result(status="done", exit_code=0)

    monkeypatch.setattr(mod, "run_with_terminate", fake_run_with_terminate)
    rt.run(
        ["echo", "hi"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env={},
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile="(version 1)(allow default)",
        perms=Permissions.from_json("{}"),
    )
    assert seen["argv"][0] == "sandbox-exec"  # wrapped


def test_execute_uses_injected_runtime(tmp_path: Path) -> None:
    # A fake runtime captures the call; execute() must route a cli provider to it.
    from herder.providers.run import execute
    from herder.config import Provider
    from herder.models import Result

    captured: dict[str, list[str]] = {}

    class FakeRuntime:
        name = "fake"

        def run(self, argv: list[str], **kw: object) -> Result:
            captured["argv"] = argv
            return Result(status="done", exit_code=0, output="ok")

    prov = Provider(
        type="cli",
        executable="/bin/echo",
        args=["hi"],
        input="stdin",
        parser="text",
    )
    res = execute(
        prov,
        "prompt",
        cwd=tmp_path,
        run_dir=tmp_path,
        env={},
        timeout=5,
        runtime=FakeRuntime(),
    )
    assert res.status == "done"
    assert captured["argv"][0] == "/bin/echo"


def test_execute_defaults_to_local_runtime(tmp_path: Path) -> None:
    from herder.providers.run import execute
    from herder.config import Provider

    prov = Provider(
        type="cli",
        executable="/bin/sh",
        args=["-c", "printf z"],
        input="stdin",
        parser="text",
    )
    res = execute(prov, "", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=5)
    assert res.status == "done"
    assert "z" in res.output
