"""Runtime Protocol and shared poll/kill core for Herder.

The Runtime Protocol defines the interface every runtime backend must
implement.  run_with_terminate() is the shared execution engine extracted
from providers/base.run_subprocess_cancellable, parameterised by an
injectable terminate callable so each backend can supply its own
cancellation strategy (docker stop, ssh ControlMaster exit, etc.).

CRITICAL: Never use shell=True. argv-only. Prompt via stdin.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from herder.models import Result

# Type alias for the injectable terminate strategy.
TerminateFn = Callable[[subprocess.Popen], None]  # type: ignore[type-arg]


def _now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


def _default_terminate(proc: subprocess.Popen, grace_seconds: float) -> None:  # type: ignore[type-arg]
    """Default terminate: SIGTERM → grace period → SIGKILL on process group.

    Reproduces the existing killpg behaviour from providers/base.py exactly
    so LocalRuntime has full parity with the original implementation.

    Args:
        proc: The running subprocess to terminate.
        grace_seconds: Seconds to wait after SIGTERM before SIGKILL.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    else:
        # Fallback: no process group available
        proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_with_terminate(
    argv: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    env: dict,  # type: ignore[type-arg]
    stdout_path: Path,
    stderr_path: Path,
    cancel_check: Callable[[], bool],
    heartbeat: Callable[[], None] | None,
    heartbeat_interval: float,
    terminate: TerminateFn | None,
    poll_interval: float = 0.5,
    grace_seconds: float = 5.0,
) -> Result:
    """Shared poll/kill execution core with injectable terminate strategy.

    Runs argv as a subprocess, polls cancel_check() and the timeout
    deadline, then delegates cancellation to the terminate callable.
    When terminate is None the default SIGTERM→grace→SIGKILL process-group
    kill is used (identical to the original run_subprocess_cancellable).

    CRITICAL: argv only; prompt via stdin. stdout/stderr to FILES (not
    pipes) to prevent deadlock on chatty children.

    Args:
        argv: Command and arguments — no shell string building.
        prompt: Text passed to the process via stdin.
        cwd: Working directory for the subprocess.
        timeout: Wall-clock seconds before timeout outcome.
        env: Environment mapping (empty dict → inherit parent env via None).
        stdout_path: File path for captured stdout.
        stderr_path: File path for captured stderr.
        cancel_check: Returns True when the job is cancelled externally.
        heartbeat: Optional callable to renew the job lease periodically.
        heartbeat_interval: Seconds between heartbeat calls.
        terminate: Custom teardown callable; None → default killpg strategy.
        poll_interval: Seconds between poll iterations (default 0.5).
        grace_seconds: Grace period after SIGTERM before SIGKILL (default 5.0).

    Returns:
        Result classified as done/failed/timeout/cancelled/unavailable.
    """
    started = _now()

    with open(stdout_path, "w") as out_fh, open(stderr_path, "w") as err_fh:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=out_fh,
                stderr=err_fh,
                text=True,
                cwd=str(cwd),
                env=env or None,
                shell=False,  # CRITICAL: never shell=True
                start_new_session=True,  # new session for process-group kill
            )
        except FileNotFoundError:
            return Result(
                status="failed",
                exit_code=127,
                error_type="unavailable",
                started_at=started,
                finished_at=_now(),
            )

        # Send prompt via stdin then close the pipe
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except BrokenPipeError:
            pass  # Child exited before reading stdin; harmless

        deadline = time.monotonic() + timeout
        last_beat = time.monotonic()
        outcome = "done"

        while proc.poll() is None:
            if heartbeat is not None and time.monotonic() - last_beat >= heartbeat_interval:
                try:
                    heartbeat()
                except Exception:  # noqa: BLE001
                    pass  # heartbeat failure must never affect the job
                last_beat = time.monotonic()

            if cancel_check():
                outcome = "cancelled"
                break
            if time.monotonic() > deadline:
                outcome = "timeout"
                break
            time.sleep(poll_interval)

        if outcome in ("cancelled", "timeout"):
            if terminate is not None:
                terminate(proc)
            else:
                _default_terminate(proc, grace_seconds)

    # Read captured output and redact secrets
    from herder.redact import redact  # noqa: E402

    stdout_text = redact(stdout_path.read_text(encoding="utf-8", errors="replace"))
    stderr_text = redact(stderr_path.read_text(encoding="utf-8", errors="replace"))

    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")

    if outcome == "cancelled":
        return Result(
            status="cancelled",
            exit_code=proc.returncode if proc.returncode is not None else -1,
            output=stdout_text,
            stderr=stderr_text,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started,
            finished_at=_now(),
        )

    if outcome == "timeout":
        return Result(
            status="timeout",
            exit_code=-1,
            error_type="timeout",
            output=stdout_text,
            stderr=stderr_text,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started,
            finished_at=_now(),
        )

    status = "done" if proc.returncode == 0 else "failed"
    error_type: str | None = None if status == "done" else "unknown"

    return Result(
        status=status,
        exit_code=proc.returncode,
        output=stdout_text,
        stderr=stderr_text,
        error_type=error_type,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=started,
        finished_at=_now(),
    )


class Runtime(Protocol):
    """Protocol that every runtime backend must satisfy.

    Each runtime receives the full execution context and produces a Result.
    The perms and sandbox_profile arguments allow each backend to apply
    appropriate confinement or enforce fail-closed security guards.
    """

    name: str

    def run(
        self,
        argv: list[str],
        *,
        prompt: str,
        cwd: Path,
        timeout: int,
        env: dict,  # type: ignore[type-arg]
        stdout_path: Path | None,
        stderr_path: Path | None,
        cancel_check: Callable[[], bool] | None,
        heartbeat: Callable[[], None] | None,
        heartbeat_interval: float,
        sandbox_profile: str | None,
        perms: object,
    ) -> Result:
        """Execute argv and return a classified Result.

        Args:
            argv: Command and arguments.
            prompt: Text passed via stdin.
            cwd: Working directory.
            timeout: Wall-clock seconds.
            env: Environment mapping.
            stdout_path: Optional file path for stdout capture.
            stderr_path: Optional file path for stderr capture.
            cancel_check: Returns True when job is cancelled externally.
            heartbeat: Optional lease-renewal callable.
            heartbeat_interval: Seconds between heartbeat calls.
            sandbox_profile: macOS sandbox profile string (local only).
            perms: Permissions instance (used by ssh fail-closed guards).

        Returns:
            Result classified as done/failed/timeout/cancelled.
        """
        ...
