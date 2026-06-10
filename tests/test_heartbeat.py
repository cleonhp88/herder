"""Tests for heartbeat lease renewal during job execution."""
import time
from pathlib import Path

from herder.providers.base import run_subprocess_cancellable


def test_heartbeat_called_periodically(tmp_path):
    """Heartbeat is called periodically during job execution."""
    beats = []
    out = tmp_path / "o.log"
    err = tmp_path / "e.log"

    res = run_subprocess_cancellable(
        ["sleep", "2"],
        prompt="",
        cwd=tmp_path,
        timeout=30,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        poll_interval=0.05,
        heartbeat=lambda: beats.append(time.monotonic()),
        heartbeat_interval=0.5,
    )

    assert res.status == "done"
    assert len(beats) >= 2, f"Expected >=2 beats, got {len(beats)}"


def test_heartbeat_failure_does_not_kill_job(tmp_path):
    """A failing heartbeat callback does not terminate the job."""
    def bad_beat() -> None:
        raise RuntimeError("db hiccup")

    out = tmp_path / "o.log"
    err = tmp_path / "e.log"

    res = run_subprocess_cancellable(
        ["sleep", "1"],
        prompt="",
        cwd=tmp_path,
        timeout=30,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        poll_interval=0.05,
        heartbeat=bad_beat,
        heartbeat_interval=0.2,
    )

    assert res.status == "done", "Job should complete despite heartbeat failure"
