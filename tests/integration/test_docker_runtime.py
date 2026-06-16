"""Gated integration test for DockerRuntime.

Skipped automatically when ``docker`` is not installed or the daemon is not
reachable.  When present and running, verifies that a real container can be
started and produces expected output.
"""
import shutil
import subprocess

import pytest


def _docker_available() -> bool:
    """Return True only when docker binary exists AND daemon responds."""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="docker not installed or daemon not running"
)


def test_docker_runs_echo(tmp_path):
    """DockerRuntime executes a real container and returns done status."""
    from herder.permissions import Permissions
    from herder.runtimes.docker import DockerRuntime

    rt = DockerRuntime(name="d", image="alpine", network="none", extra_args=[])
    res = rt.run(
        ["echo", "hi"],
        prompt="",
        cwd=tmp_path,
        timeout=60,
        env={},
        stdout_path=tmp_path / "o",
        stderr_path=tmp_path / "e",
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=Permissions.from_json("{}"),
    )
    assert res.status == "done"
    assert "hi" in res.output
