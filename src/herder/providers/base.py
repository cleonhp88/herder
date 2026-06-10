"""Safe subprocess execution with shell-injection protection.

CRITICAL: subprocess.run MUST use shell=False and pass prompt via stdin.
Never build shell strings or use shell=True.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from herder.models import Result


def _now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


def run_subprocess(
    argv: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    env: dict,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> Result:
    """Run a CLI command safely with prompt passed via stdin.

    CRITICAL: argv only; prompt via stdin. NEVER shell=True.

    Args:
        argv: Command-line arguments (program + flags).
        prompt: Prompt/input text (passed via stdin).
        cwd: Working directory.
        timeout: Timeout in seconds.
        env: Environment variables (None = inherit parent).
        stdout_path: Optional path to save stdout.
        stderr_path: Optional path to save stderr.

    Returns:
        Result with status, exit code, output, and classification.
    """
    started = _now()

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            env=env or None,
            shell=False,  # CRITICAL: never shell=True
        )
    except subprocess.TimeoutExpired:
        return Result(
            status="timeout",
            exit_code=-1,
            error_type="timeout",
            started_at=started,
            finished_at=_now(),
        )
    except FileNotFoundError:
        return Result(
            status="failed",
            exit_code=127,
            error_type="unavailable",
            started_at=started,
            finished_at=_now(),
        )

    # Import redact function for FIX 2
    from herder.redact import redact  # noqa: E402

    # Redact secrets from stdout/stderr before returning or saving
    out_text = redact(proc.stdout)
    err_text = redact(proc.stderr)

    # Save redacted stdout/stderr if paths provided
    if stdout_path:
        stdout_path.write_text(out_text)
    if stderr_path:
        stderr_path.write_text(err_text)

    # Classify result
    status = "done" if proc.returncode == 0 else "failed"
    error_type = None if status == "done" else "unknown"

    return Result(
        status=status,
        exit_code=proc.returncode,
        output=out_text,
        stderr=err_text,
        error_type=error_type,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=started,
        finished_at=_now(),
    )


def run_subprocess_cancellable(
    argv: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    env: dict,
    stdout_path: Path,
    stderr_path: Path,
    cancel_check: Callable[[], bool],
    poll_interval: float = 0.5,
    grace_seconds: float = 5.0,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval: float = 30.0,
) -> Result:
    """Run a CLI command with cancellation support. stdout/stderr go to FILES.

    Polls cancel_check() periodically. If it returns True, sends SIGTERM,
    waits grace_seconds, then SIGKILL. Timeout also kills with SIGTERM→grace→SIGKILL.

    Calls heartbeat() periodically to renew a job's lease (if provided).
    Heartbeat failures are silently caught and never kill the job.

    CRITICAL: argv only; prompt via stdin. stdout/stderr to FILES (not pipes)
    to prevent deadlock on chatty children.

    Args:
        argv: Command-line arguments (program + flags).
        prompt: Prompt/input text (passed via stdin).
        cwd: Working directory.
        timeout: Timeout in seconds.
        env: Environment variables (None = inherit parent).
        stdout_path: Path to save stdout (mandatory).
        stderr_path: Path to save stderr (mandatory).
        cancel_check: Callable that returns True if cancellation is requested.
        poll_interval: Time between checks in seconds (default 0.5).
        grace_seconds: Seconds to wait after SIGTERM before SIGKILL (default 5.0).
        heartbeat: Optional callable to renew job lease (called periodically).
        heartbeat_interval: Seconds between heartbeat calls (default 30.0).

    Returns:
        Result with status (done, failed, timeout, cancelled), exit code, and output.
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
                start_new_session=True,  # FIX 1: new session for process group kill
            )
        except FileNotFoundError:
            return Result(
                status="failed",
                exit_code=127,
                error_type="unavailable",
                started_at=started,
                finished_at=_now(),
            )

        # Send prompt via stdin and close pipe
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except BrokenPipeError:
            pass  # Child exited before reading stdin; harmless

        # Poll until process exits, timeout, or cancel requested
        deadline = time.monotonic() + timeout
        last_beat = time.monotonic()
        outcome = "done"

        while proc.poll() is None:
            # Call heartbeat at most every heartbeat_interval seconds
            if heartbeat is not None and time.monotonic() - last_beat >= heartbeat_interval:
                try:
                    heartbeat()
                except Exception:  # noqa: BLE001
                    pass  # heartbeat failure must not affect the job
                last_beat = time.monotonic()

            if cancel_check():
                outcome = "cancelled"
                break
            if time.monotonic() > deadline:
                outcome = "timeout"
                break
            time.sleep(poll_interval)

        # If not already done, kill the process and its entire process group (FIX 1)
        if outcome in ("cancelled", "timeout"):
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None

            if pgid is not None:
                # Send SIGTERM to entire process group
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

                # Wait for grace period
                try:
                    proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    # Send SIGKILL to entire process group
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            else:
                # Fallback if pgid unavailable: use old behavior
                proc.terminate()
                try:
                    proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    # Read captured output and redact secrets (FIX 2)
    from herder.redact import redact  # noqa: E402
    stdout_text = redact(stdout_path.read_text(encoding="utf-8", errors="replace"))
    stderr_text = redact(stderr_path.read_text(encoding="utf-8", errors="replace"))

    # Write redacted text back to files (FIX 2)
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")

    # Classify result
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

    # Normal completion
    status = "done" if proc.returncode == 0 else "failed"
    error_type = None if status == "done" else "unknown"

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
