"""Integration tests for the ACP client provider adapter.

Requires the acp package.  The module-level importorskip ensures the test suite
passes without the optional dependency installed.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

acp = pytest.importorskip("acp")

from herder.config import Provider  # noqa: E402
from herder.providers.acp_client import run  # noqa: E402

# Absolute path to the stub agent (used as the provider executable)
_STUB = str(Path(__file__).parent / "acp_stub_agent.py")


def _provider(mode: str, timeout: int = 15) -> Provider:
    """Build a minimal ACP Provider pointing at the stub agent.

    Args:
        mode: Stub mode argument (echo, permission, slow, refuse).
        timeout: Execution timeout in seconds.

    Returns:
        Configured Provider instance.
    """
    return Provider(
        type="acp",
        executable=sys.executable,
        args=[_STUB, mode],
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_echo_happy_path(tmp_path: Path) -> None:
    """ACP echo stub should produce 'Hello world' and status 'done'."""
    p = _provider("echo")
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=15)
    assert res.status == "done", f"Unexpected status: {res.status!r}  stderr={res.stderr!r}"
    assert res.output == "Hello world"
    assert res.exit_code == 0


# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------

def test_permission_deny_policy(tmp_path: Path) -> None:
    """allow_tools=False should produce TOOL_DENIED."""
    p = _provider("permission")
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=15, allow_tools=False)
    assert res.status == "done"
    assert "TOOL_DENIED" in res.output


def test_permission_allow_policy(tmp_path: Path) -> None:
    """allow_tools=True should produce TOOL_ALLOWED."""
    p = _provider("permission")
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=15, allow_tools=True)
    assert res.status == "done"
    assert "TOOL_ALLOWED" in res.output


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_timeout_slow_stub(tmp_path: Path) -> None:
    """Slow stub should return status 'timeout' when provider timeout is exceeded."""
    p = _provider("slow", timeout=3)
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=3)
    assert res.status == "timeout"
    assert res.error_type == "timeout"
    assert res.exit_code == -1


# ---------------------------------------------------------------------------
# Refusal mapping
# ---------------------------------------------------------------------------

def test_refusal_mapping(tmp_path: Path) -> None:
    """Stub with stop_reason='refusal' should map to status='failed'."""
    p = _provider("refuse")
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=15)
    assert res.status == "failed"
    assert res.error_type == "bad_prompt"


# ---------------------------------------------------------------------------
# Missing executable
# ---------------------------------------------------------------------------

def test_missing_executable_returns_failed(tmp_path: Path) -> None:
    """Non-existent executable should return a failed Result, not crash."""
    p = Provider(
        type="acp",
        executable="/no/such/binary/xyz_acp_agent",
        timeout=5,
    )
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=5)
    assert res.status == "failed"
    assert res.error_type == "unavailable"


# ---------------------------------------------------------------------------
# Sandbox guard
# ---------------------------------------------------------------------------

def test_sandbox_profile_raises(tmp_path: Path) -> None:
    """Passing sandbox_profile should raise RuntimeError (ACP v1 cannot be sandboxed)."""
    p = _provider("echo")
    with pytest.raises(RuntimeError, match="sandbox"):
        run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=5, sandbox_profile="deny-all")


# ---------------------------------------------------------------------------
# stderr capture
# ---------------------------------------------------------------------------

def test_stderr_path_written(tmp_path: Path) -> None:
    """stderr_path file should be created (even if empty) when provided."""
    p = _provider("echo")
    stderr_path = tmp_path / "stderr.log"
    res = run(p, "hi", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=15, stderr_path=stderr_path)
    assert res.status == "done"
    assert stderr_path.exists()


# ---------------------------------------------------------------------------
# Cancel and heartbeat coverage
# ---------------------------------------------------------------------------

def test_cancel_check_fires(tmp_path: Path) -> None:
    """cancel_check returning True after ~1s should cancel the job promptly.

    Uses the 'slow' stub (30s sleep) so the job is still running when the
    cancel fires.  The run() call must return well within 10s.
    """
    # Flip to True after 1 second of wall-clock time.
    _start = time.monotonic()

    def _cancel_after_1s() -> bool:
        return (time.monotonic() - _start) >= 1.0

    p = _provider("slow", timeout=30)
    t0 = time.monotonic()
    res = run(
        p,
        "hi",
        cwd=tmp_path,
        run_dir=tmp_path,
        env={},
        timeout=30,
        cancel_check=_cancel_after_1s,
    )
    elapsed = time.monotonic() - t0

    assert res.status == "cancelled", f"Expected cancelled, got {res.status!r}"
    assert elapsed < 10.0, f"run() took {elapsed:.1f}s — cancel did not fire promptly"


def test_heartbeat_fires(tmp_path: Path) -> None:
    """heartbeat callable should be invoked at least once during a run.

    Uses the 'slow' stub with a short provider timeout (3s, timeout path)
    and a cancel_check that fires after 1.5s so we get cancellation before
    the provider timeout.  heartbeat_interval=0.5s ensures at least one
    heartbeat fires in 1.5s.  Assertions are ≥1 (not exact) to stay
    deterministic under load.
    """
    heartbeat_count = 0

    def _heartbeat() -> None:
        nonlocal heartbeat_count
        heartbeat_count += 1

    _start = time.monotonic()

    def _cancel_after_1_5s() -> bool:
        return (time.monotonic() - _start) >= 1.5

    p = _provider("slow", timeout=30)
    res = run(
        p,
        "hi",
        cwd=tmp_path,
        run_dir=tmp_path,
        env={},
        timeout=30,
        cancel_check=_cancel_after_1_5s,
        heartbeat=_heartbeat,
        heartbeat_interval=0.5,
    )

    assert res.status == "cancelled", f"Expected cancelled, got {res.status!r}"
    assert heartbeat_count >= 1, f"heartbeat never fired (count={heartbeat_count})"
