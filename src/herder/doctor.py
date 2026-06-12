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


def _probe_acp(
    name: str,
    p: Provider,
    *,
    env: dict,
    cwd: Path,
    timestamp: str,
    probe_timeout: float = 10.0,
) -> ProviderHealth:
    """Probe an ACP provider via the initialize handshake.

    Spawns the agent process, sends initialize, and closes immediately.
    A successful initialize response means the provider is reachable and
    speaks the ACP protocol.

    Args:
        name: Provider name (for reporting).
        p: Provider configuration.
        env: Environment variables.
        cwd: Working directory.
        timestamp: ISO8601 probe timestamp.
        probe_timeout: Timeout for the entire probe in seconds (default 10).

    Returns:
        ProviderHealth with noninteractive_status "ok" on success, "fail" otherwise.
    """
    try:
        import acp
    except ImportError:
        return ProviderHealth(
            name,
            "fail",
            "unknown",
            None,
            "acp package not installed; run: uv pip install 'herder[acp]'",
            timestamp,
        )

    if not p.executable:
        return ProviderHealth(name, "fail", "missing", None, "no executable configured", timestamp)

    import asyncio

    async def _do_probe() -> tuple[str, str, int | None, str | None]:
        """Run the minimal ACP handshake.

        Returns:
            Tuple of (noninteractive_status, auth_status, latency_ms, error_sample).
        """
        from herder.providers.acp_client import _HeadlessClient

        start = datetime.now(timezone.utc)
        client = _HeadlessClient(allow_tools=False)
        try:
            args = list(p.args) if p.args else []
            async with acp.spawn_agent_process(
                client,
                p.executable,
                *args,
                env=env,
                cwd=cwd,
            ) as (conn, _process):
                await conn.initialize(acp.PROTOCOL_VERSION)
                latency_ms = int(
                    (datetime.now(timezone.utc) - start).total_seconds() * 1000
                )
                return "ok", "unknown", latency_ms, None
        except FileNotFoundError:
            return "fail", "missing", None, "binary not found"
        except Exception as exc:  # noqa: BLE001
            return "fail", "unknown", None, str(exc)[:200]

    start_wall = datetime.now(timezone.utc)
    try:
        status, auth, latency_ms, error_sample = asyncio.run(
            asyncio.wait_for(_do_probe(), timeout=probe_timeout)
        )
    except asyncio.TimeoutError:
        latency_ms = int((datetime.now(timezone.utc) - start_wall).total_seconds() * 1000)
        return ProviderHealth(
            name,
            "tty_required",
            "unknown",
            latency_ms,
            "timed out during initialize",
            timestamp,
        )
    except Exception as exc:  # noqa: BLE001
        return ProviderHealth(
            name,
            "fail",
            "unknown",
            None,
            str(exc)[:200],
            timestamp,
        )

    return ProviderHealth(name, status, auth, latency_ms, error_sample, timestamp)


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

    # Probe ACP providers via the real protocol (initialize handshake under a short timeout)
    if p.type == "acp":
        return _probe_acp(name, p, env=env, cwd=cwd, timestamp=timestamp)

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
