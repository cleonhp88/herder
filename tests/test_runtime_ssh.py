"""Tests for SSHRuntime — pure argv builder and fail-closed security guards.

Task 6.1 covers build_ssh_argv (pure unit tests, no SSH connection needed).
Task 6.2 covers SSHRuntime fail-closed guards (untrusted job refusal and
secrets-leak refusal — both fire before any subprocess is spawned).

The secrets guard checks ``secret_keys`` (the resolved allow list from
effective_allow_env), NOT the full env dict, which always contains base keys
(PATH/HOME/…) that are not secrets.
"""
from pathlib import Path

import pytest

from herder.runtimes.ssh import build_ssh_argv


def test_ssh_argv_basic() -> None:
    argv = build_ssh_argv(
        inner_argv=["mytool", "--x"],
        host="u@h",
        remote_root="/tmp/herder",
        env={},
        control_path="/tmp/cm-1",
        ssh_opts=[],
    )
    assert argv[0] == "ssh"
    assert "u@h" in argv
    assert any("ControlPath=/tmp/cm-1" in a for a in argv)
    # remote command runs under remote_root and execs the inner argv
    remote = argv[-1]
    assert "cd /tmp/herder" in remote and "mytool" in remote


def test_ssh_argv_includes_env_exports() -> None:
    argv = build_ssh_argv(
        inner_argv=["t"],
        host="u@h",
        remote_root="/r",
        env={"FOO": "bar"},
        control_path="/tmp/cm",
        ssh_opts=[],
    )
    assert "FOO=bar" in argv[-1]


def test_ssh_argv_respects_ssh_opts() -> None:
    argv = build_ssh_argv(
        inner_argv=["t"],
        host="u@h",
        remote_root="/r",
        env={},
        control_path="/tmp/cm",
        ssh_opts=["-p", "2222"],
    )
    assert "-p" in argv and "2222" in argv


# ---------------------------------------------------------------------------
# Task 6.2 — Fail-closed guards
# ---------------------------------------------------------------------------
from herder.runtimes.ssh import SSHRuntime  # noqa: E402
from herder.permissions import Permissions  # noqa: E402


def _rt(allow_secrets: bool = False) -> SSHRuntime:
    return SSHRuntime(
        name="m",
        host="u@h",
        remote_root="/tmp/herder",
        ssh_opts=[],
        allow_remote_secrets=allow_secrets,
    )


def test_ssh_refuses_untrusted_job(tmp_path: Path) -> None:
    rt = _rt()
    perms = Permissions.from_json('{"network": false}')
    with pytest.raises(RuntimeError, match="cannot confine untrusted"):
        rt.run(
            ["t"],
            prompt="",
            cwd=tmp_path,
            timeout=5,
            env={},
            stdout_path=tmp_path / "o",
            stderr_path=tmp_path / "e",
            cancel_check=None,
            heartbeat=None,
            heartbeat_interval=30.0,
            sandbox_profile=None,
            perms=perms,
        )


def test_ssh_refuses_secrets_without_flag(tmp_path: Path) -> None:
    rt = _rt(allow_secrets=False)
    # network:true so the untrusted guard passes and we reach the secrets check.
    # secret_keys must be non-empty to trigger the guard — env alone is NOT enough
    # because env always contains base keys (PATH/HOME/…) that are not secrets.
    perms = Permissions.from_json('{"network": true}')
    with pytest.raises(RuntimeError, match="secrets to remote"):
        rt.run(
            ["t"],
            prompt="",
            cwd=tmp_path,
            timeout=5,
            env={"API_KEY": "x"},
            stdout_path=tmp_path / "o",
            stderr_path=tmp_path / "e",
            cancel_check=None,
            heartbeat=None,
            heartbeat_interval=30.0,
            sandbox_profile=None,
            perms=perms,
            secret_keys=["API_KEY"],  # resolved allow list — this is what triggers the guard
        )


def test_ssh_allows_secrets_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = _rt(allow_secrets=True)
    monkeypatch.setattr(
        "herder.runtimes.ssh.run_with_terminate",
        lambda *a, **k: __import__(
            "herder.models", fromlist=["Result"]
        ).Result(status="done", exit_code=0),
    )
    perms = Permissions.from_json('{"network": true}')
    res = rt.run(
        ["t"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env={"API_KEY": "x"},
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=perms,
        secret_keys=["API_KEY"],  # has secrets, but allow_remote_secrets=True
    )
    assert res.status == "done"


def test_ssh_no_secrets_base_env_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job with no secrets (secret_keys=[]) must NOT be refused.

    This is the critical regression test for the defect the Enforcer caught:
    build_env([]) always produces 8+ base keys (PATH/HOME/USER/…).  The
    SSHRuntime must accept a job whose env contains only base keys — the guard
    must key off secret_keys (the resolved allow list), NOT the full env dict.
    """
    rt = _rt(allow_secrets=False)
    monkeypatch.setattr(
        "herder.runtimes.ssh.run_with_terminate",
        lambda *a, **k: __import__(
            "herder.models", fromlist=["Result"]
        ).Result(status="done", exit_code=0),
    )
    from herder.env import build_env

    perms = Permissions.from_json('{"network": true}')
    # build_env([]) returns ~8 base keys — this MUST not trigger the secrets guard
    base_only_env = build_env([])
    assert len(base_only_env) >= 1, "build_env([]) must return at least PATH"

    res = rt.run(
        ["t"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env=base_only_env,  # full minimised env with base keys only
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=perms,
        secret_keys=[],  # no secrets — allow list is empty
    )
    assert res.status == "done"


def test_registry_builds_ssh() -> None:
    """build_runtime(SSHRuntimeSpec(...)) must return an SSHRuntime with correct fields."""
    from herder.runtimes.registry import build_runtime
    from herder.config import SSHRuntimeSpec

    rt = build_runtime(SSHRuntimeSpec(type="ssh", host="u@h"))
    assert isinstance(rt, SSHRuntime)
    assert rt.host == "u@h"
    assert rt.remote_root == "/tmp/herder"
    assert rt.allow_remote_secrets is False
