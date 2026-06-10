"""Unified provider execution dispatcher routing by provider type."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from herder.config import Provider
from herder.models import Result
from herder.providers import cli_generic, ollama_http


def execute(
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
    """Route execution by provider type. Single entry point for worker and doctor.

    Dispatches to the appropriate provider implementation based on provider.type:
    - "cli": delegates to cli_generic.run()
    - "ollama": delegates to ollama_http.run() (ignores cancel_check and heartbeat in v1)
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
        cancel_check: Optional callable returning True if cancellation requested (CLI only in v1).
        heartbeat: Optional callable to renew job lease periodically (CLI only in v1).
        heartbeat_interval: Seconds between heartbeat calls (default 30.0).
        sandbox_profile: Optional SBPL profile string for seatbelt sandbox confinement (CLI only).

    Returns:
        Result with status, output, and metadata.

    Raises:
        ValueError: If provider type is not supported.
    """
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
        )

    if provider.type == "ollama":
        # YAGNI: HTTP-based cancellation and heartbeat for Ollama is v2+
        # Ollama runs over HTTP so sandbox is not relevant
        return ollama_http.run(provider, prompt, timeout=timeout)

    raise ValueError(f"unsupported provider type: {provider.type}")
