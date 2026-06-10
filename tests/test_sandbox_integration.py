"""Integration tests for macOS seatbelt sandbox enforcement (real sandbox-exec).

These tests use real subprocess execution with sandbox-exec to verify that:
1. Writes inside allow_write paths succeed
2. Writes outside allow_write paths are blocked by seatbelt
3. Network access is denied when deny_network=True
"""
import subprocess
import sys
from pathlib import Path

import pytest

from herder.providers.sandbox import build_profile, is_supported, wrap

pytestmark = pytest.mark.skipif(
    not is_supported(), reason="sandbox-exec only on macOS"
)


def _run(
    profile: str, script: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Helper: run a shell script under seatbelt with the given profile.

    Args:
        profile: SBPL profile string.
        script: Shell script to execute.
        cwd: Working directory for the subprocess.

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    argv = wrap(["/bin/sh", "-c", script], profile)
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_write_inside_cwd_allowed(tmp_path: Path) -> None:
    """Verify writes inside the allow_write cwd succeed."""
    prof = build_profile(allow_write=[tmp_path], deny_network=True)
    r = _run(prof, "echo ok > inside.txt", tmp_path)
    assert r.returncode == 0, f"write should succeed; stderr: {r.stderr}"
    assert (tmp_path / "inside.txt").exists()
    assert (tmp_path / "inside.txt").read_text().strip() == "ok"


def test_write_outside_cwd_denied(tmp_path: Path) -> None:
    """Verify writes outside allow_write are blocked by seatbelt."""
    outside = tmp_path.parent / "escape_attempt.txt"
    prof = build_profile(allow_write=[tmp_path], deny_network=True)
    r = _run(prof, f"echo x > '{outside}' 2>&1", tmp_path)
    # seatbelt will block the write; exit code != 0 and file does not exist
    assert r.returncode != 0 or not outside.exists(), (
        "write outside cwd should be blocked by seatbelt"
    )
    assert not outside.exists()


def test_network_denied(tmp_path: Path) -> None:
    """Verify network access is denied when deny_network=True.

    Tests by attempting a TCP connect to 1.1.1.1:80 via Python.
    Under (deny network*), the socket creation or connect will fail.
    """
    prof = build_profile(allow_write=[tmp_path], deny_network=True)
    script = (
        "python3 -c \"import socket,sys; "
        "socket.setdefaulttimeout(2); "
        "socket.create_connection(('1.1.1.1',80)); print('CONNECTED')\" 2>&1"
    )
    r = _run(prof, script, tmp_path)
    # Connection should be blocked by seatbelt; "CONNECTED" not in output
    assert "CONNECTED" not in r.stdout, (
        "network should be denied; connection should not succeed"
    )


def test_read_allowed(tmp_path: Path) -> None:
    """Verify reads work normally under sandbox (not denied)."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("readable content")
    prof = build_profile(allow_write=[tmp_path], deny_network=False)
    r = _run(prof, f"cat '{test_file}'", tmp_path)
    assert r.returncode == 0
    assert "readable content" in r.stdout


def test_execution_allowed(tmp_path: Path) -> None:
    """Verify subprocess execution works normally under sandbox."""
    prof = build_profile(allow_write=[tmp_path], deny_network=False)
    r = _run(prof, "echo hello && ls", tmp_path)
    assert r.returncode == 0
    assert "hello" in r.stdout
