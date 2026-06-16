"""Benchmark service — compare latency, tokens, and output across providers.

Runs the same prompt through multiple providers sequentially, collecting
metrics about execution time, token usage, and output size. Each provider
runs in an isolated temporary directory to prevent side effects.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from herder.config import Config, ConfigError
from herder.env import build_env
from herder.providers.run import execute


@dataclass
class BenchResult:
    """Result metrics for a single provider benchmark run.

    Attributes:
        provider: Name of the provider that was benchmarked.
        status: Execution status (done, failed, timeout, cancelled).
        duration_ms: Total execution time in milliseconds.
        output_len: Length of the output in characters.
        tokens: Total tokens (eval_count + prompt_eval_count), or None if unavailable.
        error_type: Classification of failure (if status != done), or None.
    """

    provider: str
    status: str
    duration_ms: int
    output_len: int
    tokens: int | None = None
    error_type: str | None = None


@dataclass
class BenchReport:
    """Complete benchmark report for one prompt across multiple providers.

    Attributes:
        prompt_chars: Length of the input prompt in characters.
        results: List of BenchResult objects, one per provider.
    """

    prompt_chars: int
    results: list[BenchResult] = field(default_factory=list)


def run_bench(cfg: Config, prompt: str, provider_names: list[str]) -> BenchReport:
    """Run a prompt through multiple providers and collect metrics.

    Each provider executes in its own temporary directory to isolate side effects.
    Providers are run sequentially. Unknown provider names raise ConfigError
    before any execution begins.

    Args:
        cfg: Loaded configuration with provider definitions.
        prompt: Input prompt to send to each provider.
        provider_names: List of provider names (must all exist in cfg.providers).

    Returns:
        BenchReport with prompt_chars and results list.

    Raises:
        ConfigError: If any provider name is not in cfg.providers.
    """
    # Validate all provider names exist before running anything
    for name in provider_names:
        if name not in cfg.providers:
            raise ConfigError(f"unknown provider: {name}")

    rep = BenchReport(prompt_chars=len(prompt))

    # Run each provider sequentially
    for name in provider_names:
        prov = cfg.providers[name]

        # Resolve env profile and allowlist
        allow = []
        if prov.env_profile:
            prof = cfg.env_profiles.get(prov.env_profile)
            if prof:
                allow = prof.allow_env

        # Create isolated temp directory for this provider run
        with tempfile.TemporaryDirectory(prefix="herder-bench-") as td:
            tdp = Path(td)
            stdout_log = tdp / "out.log"
            stderr_log = tdp / "err.log"

            # Measure execution time
            t0 = datetime.now(timezone.utc)
            res = execute(
                prov,
                prompt,
                cwd=tdp,
                run_dir=tdp,
                env=build_env(allow),
                timeout=prov.timeout,
                stdout_path=stdout_log,
                stderr_path=stderr_log,
            )
            duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)

        # Extract token usage from result
        tokens = None
        if res.usage:
            eval_count = int(res.usage.get("eval_count") or 0)
            prompt_eval_count = int(res.usage.get("prompt_eval_count") or 0)
            tokens = eval_count + prompt_eval_count

        # Record result
        rep.results.append(
            BenchResult(
                provider=name,
                status=res.status,
                duration_ms=duration_ms,
                output_len=len(res.output or ""),
                tokens=tokens,
                error_type=res.error_type,
            )
        )

    return rep
