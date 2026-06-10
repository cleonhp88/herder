"""Generic CLI provider execution: build invocation, run, parse output."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from herder.config import Provider
from herder.models import Result
from herder.providers.base import run_subprocess, run_subprocess_cancellable
from herder.providers.invocation import build_invocation
from herder.providers.parsers import parse


def run(
    provider: Provider,
    prompt: str,
    *,
    cwd: Path,
    run_dir: Path,
    env: dict,
    timeout: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval: float = 30.0,
    sandbox_profile: str | None = None,
) -> Result:
    """Execute a CLI provider: build argv, run subprocess, parse output.

    Builds the invocation from the provider config (handling input mode),
    runs the subprocess with the prompt, and applies the output parser
    only if execution succeeded (status == "done").

    If cancel_check is provided and stdout_path/stderr_path are set,
    uses the cancellable runner; otherwise uses the standard runner.

    If sandbox_profile is provided, wraps argv with sandbox-exec before execution.

    Args:
        provider: Provider configuration (executable, args, input mode, parser).
        prompt: Input prompt text.
        cwd: Working directory for subprocess.
        run_dir: Directory for temporary files (e.g., prompt files).
        env: Environment variables.
        timeout: Execution timeout in seconds.
        stdout_path: Optional path to save stdout.
        stderr_path: Optional path to save stderr.
        cancel_check: Optional callable returning True if cancellation requested.
        heartbeat: Optional callable to renew job lease periodically.
        heartbeat_interval: Seconds between heartbeat calls (default 30.0).
        sandbox_profile: Optional SBPL profile string for seatbelt sandbox confinement.

    Returns:
        Result with status, output (after parsing), and metadata.
    """
    # Build argv from provider config, respecting input mode
    inv = build_invocation(provider, prompt, run_dir)
    argv = inv.argv

    # If sandbox_profile is provided, wrap argv with sandbox-exec
    if sandbox_profile is not None:
        from herder.providers.sandbox import wrap
        argv = wrap(argv, sandbox_profile)

    # Run subprocess with prompt (via stdin or as arg depending on input mode)
    if cancel_check is not None and stdout_path is not None and stderr_path is not None:
        res = run_subprocess_cancellable(
            argv,
            prompt=inv.stdin or "",
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel_check=cancel_check,
            heartbeat=heartbeat,
            heartbeat_interval=heartbeat_interval,
        )
    else:
        res = run_subprocess(
            argv,
            prompt=inv.stdin or "",
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    # Apply parser only if execution succeeded
    if res.status == "done":
        res.output = parse(provider.parser, res.output)

    return res
