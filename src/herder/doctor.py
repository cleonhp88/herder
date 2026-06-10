"""Provider health probing — detect auth issues, TTY requirements, timeouts.

Performs non-intrusive health checks on providers by running them with a
generic probe prompt and classifying the result (ok, tty_required, prompted, fail).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from herder.config import Provider
from herder.providers import ollama_http
from herder.providers.base import run_subprocess
from herder.providers.invocation import build_invocation


PROBE_PROMPT = "Reply with the single word: OK"

PROMPT_PATTERNS = (
    "login",
    "sign in",
    "press enter",
    "continue?",
    "api key",
    "authorize",
    "permission",
)


@dataclass
class ProviderHealth:
    """Health status of a provider after probing.

    Attributes:
        provider: Provider name.
        noninteractive_status: ok | tty_required | prompted | fail.
        auth_status: ok | missing | expired | unknown.
        latency_ms: Probe latency in milliseconds (None if not measured).
        error_sample: Sample error message (first 200 chars).
        last_probe_at: ISO8601 timestamp of probe time.
    """

    provider: str
    noninteractive_status: str
    auth_status: str
    latency_ms: int | None
    error_sample: str | None
    last_probe_at: str


def _looks_prompted(text: str) -> bool:
    """Check if output contains login/auth prompts."""
    low = text.lower()
    return any(pat in low for pat in PROMPT_PATTERNS)


def probe_provider(
    name: str, p: Provider, *, env: dict, cwd: Path
) -> ProviderHealth:
    """Probe a provider to detect auth issues, TTY requirements, or availability.

    Runs the provider with a generic probe prompt and classifies the result.

    Args:
        name: Provider name (for reporting).
        p: Provider configuration.
        env: Environment variables to pass to subprocess.
        cwd: Working directory.

    Returns:
        ProviderHealth with status, latency, and error sample.
    """
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat()

    # Probe ollama providers via HTTP
    if p.type == "ollama":
        start = now
        res = ollama_http.run(p, PROBE_PROMPT, timeout=min(p.timeout, 30))
        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        if res.status == "done":
            return ProviderHealth(
                name, "ok", "ok", latency, (res.output or "")[:200] or None, timestamp
            )
        if res.error_type == "timeout":
            return ProviderHealth(
                name,
                "tty_required",
                "unknown",
                latency,
                "timed out",
                timestamp,
            )
        if res.error_type == "unavailable":
            return ProviderHealth(
                name,
                "fail",
                "missing",
                latency,
                (res.output or "server unreachable")[:200],
                timestamp,
            )
        return ProviderHealth(
            name,
            "fail",
            "unknown",
            latency,
            (res.output or "")[:200] or None,
            timestamp,
        )

    # Non-CLI providers always report unknown
    if p.type != "cli" or not p.executable:
        return ProviderHealth(name, "ok", "unknown", None, None, timestamp)

    # Build invocation from Provider config in a temporary directory (for file-mode probes)
    with tempfile.TemporaryDirectory() as td:
        inv = build_invocation(p, PROBE_PROMPT, Path(td))

        # Run subprocess with probe prompt
        start = datetime.now(timezone.utc)
        res = run_subprocess(
            inv.argv,
            prompt=inv.stdin or "",
            cwd=cwd,
            timeout=min(p.timeout, 30),  # Cap probe timeout at 30s
            env=env,
        )
        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        # Combine stdout and stderr for analysis
        combined = "\n".join(part for part in (res.output, res.stderr) if part)

        # Classify: binary unavailable
        if res.error_type == "unavailable":
            return ProviderHealth(
                name,
                "fail",
                "missing",
                latency,
                "binary not found",
                timestamp,
            )

        # Classify: timeout → likely TTY or auth required
        if res.status == "timeout":
            return ProviderHealth(
                name,
                "tty_required",
                "unknown",
                latency,
                "timed out (TTY/login?)",
                timestamp,
            )

        # Classify: prompted with auth keywords
        if _looks_prompted(combined):
            return ProviderHealth(
                name,
                "prompted",
                "missing",
                latency,
                combined[:200],
                timestamp,
            )

        # Classify: successful execution
        if res.status == "done":
            return ProviderHealth(
                name,
                "ok",
                "ok",
                latency,
                combined[:200] or None,
                timestamp,
            )

        # Classify: other failures
        return ProviderHealth(
            name,
            "fail",
            "unknown",
            latency,
            combined[:200] or None,
            timestamp,
        )
