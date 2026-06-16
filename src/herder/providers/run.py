"""Unified provider execution dispatcher routing by provider type."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from herder.config import Provider
from herder.models import Result
from herder.providers import cli_generic, ollama_http

if TYPE_CHECKING:
    from herder.permissions import Permissions
    from herder.runtimes.base import Runtime


def execute(
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
    allow_tools: bool = False,
    runtime: "Runtime | None" = None,
    perms: "Permissions | None" = None,
    secret_keys: list[str] | None = None,
) -> Result:
    """Route execution by provider type. Single entry point for worker and doctor.

    Dispatches to the appropriate provider implementation based on provider.type:
    - "cli": delegates to cli_generic.run() via the injected runtime
    - "ollama": delegates to ollama_http.run() (HTTP-based; runtime is ignored)
    - "acp": delegates to acp_client.run() (manages own transport; runtime is ignored)
    - other: raises ValueError

    Args:
        provider: Provider configuration with type, executable, model, etc.
        prompt: Input prompt text.
        cwd: Working directory.
        run_dir: Directory for temporary files.
        env: Environment variables.
        timeout: Execution timeout in seconds.
        stdout_path: Optional path to save stdout.
        stderr_path: Optional path to save stderr.
        cancel_check: Optional callable returning True if cancellation requested.
        heartbeat: Optional callable to renew job lease periodically.
        heartbeat_interval: Seconds between heartbeat calls (default 30.0).
        sandbox_profile: Optional SBPL profile string for seatbelt sandbox confinement.
        allow_tools: Whether to allow ACP agent tool-use requests (ACP only).
        runtime: Runtime backend to use for CLI spawn; defaults to LocalRuntime().
        perms: Permissions instance forwarded to the runtime for fail-closed guards.
        secret_keys: Resolved secret allow list from effective_allow_env();
                     forwarded to SSHRuntime's secrets guard. Empty list (the
                     default) means no secrets — base env keys (PATH/HOME/…)
                     are NOT counted as secrets.

    Returns:
        Result with status, output, and metadata.

    Raises:
        ValueError: If provider type is not supported.
    """
    from herder.runtimes.local import LocalRuntime
    effective_runtime = runtime if runtime is not None else LocalRuntime()

    if provider.type == "cli":
        return cli_generic.run(
            provider,
            prompt,
            cwd=cwd,
            run_dir=run_dir,
            env=env,
            timeout=timeout,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel_check=cancel_check,
            heartbeat=heartbeat,
            heartbeat_interval=heartbeat_interval,
            sandbox_profile=sandbox_profile,
            runtime=effective_runtime,
            perms=perms,
            secret_keys=secret_keys,
        )

    if provider.type == "ollama":
        # YAGNI: HTTP-based cancellation and heartbeat for Ollama is v2+
        # Ollama runs over HTTP so sandbox is not relevant
        return ollama_http.run(provider, prompt, timeout=timeout)

    if provider.type == "acp":
        # Lazy import: acp_client imports the acp SDK, which is an optional dependency.
        # Importing here keeps the module importable without acp installed.
        from herder.providers import acp_client
        return acp_client.run(
            provider,
            prompt,
            cwd=cwd,
            run_dir=run_dir,
            env=env,
            timeout=timeout,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel_check=cancel_check,
            heartbeat=heartbeat,
            heartbeat_interval=heartbeat_interval,
            sandbox_profile=sandbox_profile,
            allow_tools=allow_tools,
        )

    raise ValueError(f"unsupported provider type: {provider.type}")
