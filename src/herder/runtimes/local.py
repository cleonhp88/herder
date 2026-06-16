"""LocalRuntime — local subprocess backend for Herder.

Wraps the existing providers/base.py execution functions behind the Runtime
Protocol.  Owns the seatbelt (sandbox-exec) wrap so docker/ssh runtimes do
not inherit macOS-specific logic.

CRITICAL: Never use shell=True. argv-only. Prompt via stdin.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from herder.models import Result
from herder.runtimes.base import run_with_terminate


@dataclass(frozen=True)
class LocalRuntime:
    """Runtime that executes jobs as local subprocesses.

    Delegates to the shared poll/kill core (run_with_terminate) when
    cancel_check and file paths are provided, and to the simple
    run_subprocess otherwise — identical behaviour to pre-Phase-3 code.

    The seatbelt (sandbox-exec) wrap is applied here so that Docker/SSH
    runtimes that manage their own confinement do not inherit it.

    Attributes:
        name: Runtime identifier — always "local".
    """

    name: str = "local"

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
        secret_keys: list[str] | None = None,  # noqa: ARG002 — guards live in SSHRuntime
    ) -> Result:
        """Execute argv locally and return a classified Result.

        Applies the seatbelt wrap when sandbox_profile is set, then routes
        to the cancellable or simple runner based on whether cancel_check
        and file capture paths are supplied.

        Args:
            argv: Command and arguments — no shell string building.
            prompt: Text passed to the process via stdin.
            cwd: Working directory for the subprocess.
            timeout: Wall-clock seconds before timeout outcome.
            env: Environment mapping (empty dict → inherit parent env).
            stdout_path: File path for stdout capture; None → captured in memory.
            stderr_path: File path for stderr capture; None → captured in memory.
            cancel_check: Returns True when the job is cancelled externally.
            heartbeat: Optional callable to renew the job lease periodically.
            heartbeat_interval: Seconds between heartbeat calls.
            sandbox_profile: macOS SBPL profile string; triggers sandbox-exec wrap.
            perms: Permissions instance (unused by local runtime; kept for Protocol).

        Returns:
            Result classified as done/failed/timeout/cancelled/unavailable.
        """
        if sandbox_profile is not None:
            from herder.providers.sandbox import wrap
            argv = wrap(argv, sandbox_profile)

        if (
            cancel_check is not None
            and stdout_path is not None
            and stderr_path is not None
        ):
            return run_with_terminate(
                argv,
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cancel_check=cancel_check,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
                terminate=None,
            )

        # Non-cancellable path: delegate to the simple subprocess runner.
        from herder.providers.base import run_subprocess
        return run_subprocess(
            argv,
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            env=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
