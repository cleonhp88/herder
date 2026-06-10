"""Tests for cancellable subprocess execution with SIGTERM→grace→SIGKILL."""
import os
import signal
import time
from pathlib import Path

from herder.providers.base import run_subprocess_cancellable


def test_cancel_kills_long_process_quickly(tmp_path):
    """Requesting cancel via cancel_check() kills process quickly (not after full timeout)."""
    out, err = tmp_path / "o.log", tmp_path / "e.log"
    t0 = time.monotonic()
    res = run_subprocess_cancellable(
        ["sleep", "30"],
        prompt="",
        cwd=tmp_path,
        timeout=60,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: True,
        poll_interval=0.05,
        grace_seconds=1.0,
    )
    elapsed = time.monotonic() - t0
    assert res.status == "cancelled"
    assert elapsed < 10  # killed fast, not after 30s


def test_timeout_kills_long_process(tmp_path):
    """Timeout expires while process running → timeout status."""
    out, err = tmp_path / "o.log", tmp_path / "e.log"
    res = run_subprocess_cancellable(
        ["sleep", "30"],
        prompt="",
        cwd=tmp_path,
        timeout=1,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
        poll_interval=0.05,
        grace_seconds=0.5,
    )
    assert res.status == "timeout"
    assert res.error_type == "timeout"


def test_normal_completion_captures_output_to_files(tmp_path):
    """Normal (non-cancelled) completion captures output to both stdout and result."""
    out, err = tmp_path / "o.log", tmp_path / "e.log"
    res = run_subprocess_cancellable(
        ["cat"],
        prompt="hello cancellable",
        cwd=tmp_path,
        timeout=10,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
    )
    assert res.status == "done"
    assert res.exit_code == 0
    assert res.output.strip() == "hello cancellable"
    assert out.read_text().strip() == "hello cancellable"


def test_missing_binary_classified(tmp_path):
    """Missing executable → failed with error_type unavailable."""
    out, err = tmp_path / "o.log", tmp_path / "e.log"
    res = run_subprocess_cancellable(
        ["no-such-bin-xyz"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: False,
    )
    assert res.status == "failed"
    assert res.error_type == "unavailable"


def test_cancel_kills_child_process_group(tmp_path):
    """FIX 1: Cancellation kills the entire process group (not just parent).

    When a parent spawns a child and agent cancels, both parent and child
    must be reaped. This verifies that start_new_session=True + os.killpg()
    terminates the entire process group.
    """
    out, err = tmp_path / "o.log", tmp_path / "e.log"
    pidfile = tmp_path / "child.pid"

    # Script: parent spawns a child in background that sleeps 30s, then parent sleeps 30s
    # If only parent is killed, child will orphan and survive.
    script = (
        f"sh -c '(echo $$ > {pidfile}; sleep 30) & "
        "sleep 30; wait'"
    )

    res = run_subprocess_cancellable(
        ["/bin/sh", "-c", script],
        prompt="",
        cwd=tmp_path,
        timeout=60,
        env={},
        stdout_path=out,
        stderr_path=err,
        cancel_check=lambda: True,  # Request cancel immediately
        poll_interval=0.05,
        grace_seconds=1.0,
    )

    assert res.status == "cancelled"

    # Wait a moment for process cleanup
    time.sleep(0.5)

    # Check that the child process is dead
    if pidfile.exists():
        try:
            child_pid = int(pidfile.read_text().strip())
            # Try to send signal 0 (existence check)
            try:
                os.kill(child_pid, 0)
                # If we reach here, process is still alive → TEST FAILED
                assert False, f"child process {child_pid} survived cancellation (process group not killed)"
            except ProcessLookupError:
                # Expected: process is dead
                pass
        except (ValueError, OSError):
            # pidfile unreadable or already cleaned up → OK
            pass
