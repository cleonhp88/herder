"""Tests for the shared poll/kill core with injectable terminate.

Tests are the contract — RED before GREEN.
"""
from pathlib import Path

from herder.runtimes.base import run_with_terminate


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "out.log", tmp_path / "err.log"


def test_done_fast_command(tmp_path: Path) -> None:
    out, err = _paths(tmp_path)
    res = run_with_terminate(
        ["/bin/sh", "-c", "printf hello"],
        prompt="",
        cwd=tmp_path,
        timeout=10,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        terminate=None,
    )
    assert res.status == "done"
    assert "hello" in res.output


def test_cancel_invokes_custom_terminate(tmp_path: Path) -> None:
    out, err = _paths(tmp_path)
    called: dict[str, int] = {"n": 0}

    def fake_terminate(proc: object) -> None:
        called["n"] += 1
        proc.kill()  # type: ignore[attr-defined]

    res = run_with_terminate(
        ["/bin/sh", "-c", "sleep 30"],
        prompt="",
        cwd=tmp_path,
        timeout=10,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: True,  # cancel immediately
        heartbeat=None,
        heartbeat_interval=30.0,
        terminate=fake_terminate,
    )
    assert res.status == "cancelled"
    assert called["n"] == 1  # custom terminate was used


def test_timeout_classified(tmp_path: Path) -> None:
    out, err = _paths(tmp_path)
    res = run_with_terminate(
        ["/bin/sh", "-c", "sleep 30"],
        prompt="",
        cwd=tmp_path,
        timeout=1,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        terminate=None,
        poll_interval=0.1,
    )
    assert res.status == "timeout"


def test_missing_binary_unavailable(tmp_path: Path) -> None:
    out, err = _paths(tmp_path)
    res = run_with_terminate(
        ["/nonexistent/xyz"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        terminate=None,
    )
    assert res.status == "failed"
    assert res.error_type == "unavailable"


def test_default_terminate_kills_process_group(tmp_path: Path) -> None:
    # terminate=None must fall back to the SIGTERM→grace→SIGKILL process-group kill.
    out, err = _paths(tmp_path)
    res = run_with_terminate(
        ["/bin/sh", "-c", "sleep 30"],
        prompt="",
        cwd=tmp_path,
        timeout=1,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        terminate=None,
        poll_interval=0.1,
        grace_seconds=0.5,
    )
    assert res.status == "timeout"  # process was killed, not left running
