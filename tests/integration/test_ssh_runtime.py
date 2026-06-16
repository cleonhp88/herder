"""Gated integration test for SSHRuntime.

Skipped unless the environment variable HERDER_SSH_TEST_HOST is set.
When set, connects to that host and verifies a simple command executes
successfully end-to-end via SSHRuntime.

Usage:
    HERDER_SSH_TEST_HOST=user@hostname pytest tests/integration/test_ssh_runtime.py -v
"""
import os
from pathlib import Path

import pytest

from herder.permissions import Permissions
from herder.runtimes.ssh import SSHRuntime

pytestmark = pytest.mark.skipif(
    os.environ.get("HERDER_SSH_TEST_HOST") is None,
    reason="HERDER_SSH_TEST_HOST not set — skipping SSH integration test",
)


def test_ssh_runtime_runs_echo(tmp_path: Path) -> None:
    """Verify SSHRuntime can run a simple command on the configured host."""

    host: str = os.environ["HERDER_SSH_TEST_HOST"]
    rt = SSHRuntime(
        name="integration-test",
        host=host,
        remote_root="/tmp/herder-integration",
        ssh_opts=[],
        allow_remote_secrets=False,
    )
    # network=True so the untrusted guard passes
    perms = Permissions.from_json('{"network": true}')
    res = rt.run(
        ["echo", "hi"],
        prompt="",
        cwd=tmp_path,
        timeout=30,
        env={},
        stdout_path=tmp_path / "o.log",
        stderr_path=tmp_path / "e.log",
        cancel_check=lambda: False,
        heartbeat=None,
        heartbeat_interval=30.0,
        sandbox_profile=None,
        perms=perms,
    )
    assert res.status == "done"
    assert "hi" in (res.output or "")
