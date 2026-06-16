"""Generic CLI provider execution: build invocation, run, parse output."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from herder.config import Provider
from herder.models import Result
from herder.providers.invocation import build_invocation
from herder.providers.parsers import parse

if TYPE_CHECKING:
    from herder.permissions import Permissions
    from herder.runtimes.base import Runtime


def run(
    provider: Provider,
    prompt: str,
    *,
    cwd: Path,
    run_dir: Path,
    env: dict,  # type: ignore[type-arg]
    timeout: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval: float = 30.0,
    sandbox_profile: str | None = None,
    runtime: "Runtime | None" = None,
    perms: "Permissions | None" = None,
    secret_keys: list[str] | None = None,
) -> Result:
    """Execute a CLI provider: build argv, delegate to runtime, parse output.

    Builds the invocation from the provider config (handling input mode),
    delegates subprocess execution to the injected runtime, and applies
    the output parser only if execution succeeded (status == "done").

    The sandbox_profile and perms are forwarded to the runtime so each
    backend can apply appropriate confinement or enforce fail-closed rules.
    LocalRuntime applies the seatbelt wrap; Docker/SSH runtimes self-confine.

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
        sandbox_profile: Optional SBPL profile string forwarded to the runtime.
        runtime: Runtime backend; defaults to LocalRuntime() when None.
        perms: Permissions forwarded to the runtime for fail-closed guards.
        secret_keys: Resolved secret allow list from effective_allow_env();
                     forwarded to SSHRuntime's secrets guard. Empty list (the
                     default) means no secrets — base env keys (PATH/HOME/…)
                     are NOT counted as secrets.

    Returns:
        Result with status, output (after parsing), and metadata.
    """
    from herder.runtimes.local import LocalRuntime
    effective_runtime: Runtime = runtime if runtime is not None else LocalRuntime()

    # Build argv from provider config, respecting input mode
    inv = build_invocation(provider, prompt, run_dir)
    argv = inv.argv

    res = effective_runtime.run(
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
        sandbox_profile=sandbox_profile,
        perms=perms,
        secret_keys=secret_keys,
    )

    # Apply parser only if execution succeeded
    if res.status == "done":
        res.output = parse(provider.parser, res.output)

    return res
