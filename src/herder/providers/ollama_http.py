"""Ollama HTTP provider for remote LLM inference."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone

from herder.config import Provider
from herder.models import Result


def run(provider: Provider, prompt: str, *, timeout: int) -> Result:
    """Call a remote Ollama server's /api/generate endpoint (non-streaming).

    Args:
        provider: Provider configuration with base_url and model.
        prompt: Input prompt to send to the model.
        timeout: Request timeout in seconds.

    Returns:
        Result object with status, output, usage metrics, and timestamps.
    """
    started = datetime.now(timezone.utc)

    # Build request
    url = (provider.base_url or "").rstrip("/") + "/api/generate"
    body: dict = {
        "model": provider.model,
        "prompt": prompt,
        "stream": False,
    }
    if provider.think is not None:
        body["think"] = provider.think
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # Execute request with error handling
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (TimeoutError, socket.timeout):
        return Result(
            "timeout",
            -1,
            error_type="timeout",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            return Result(
                "timeout",
                -1,
                error_type="timeout",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
            )
        return Result(
            "failed",
            -1,
            error_type="unavailable",
            output=str(e.reason),
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as e:  # noqa: BLE001
        return Result(
            "failed",
            -1,
            error_type="unknown",
            output=str(e),
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )

    # Extract usage metrics if present
    usage = {
        k: data[k]
        for k in ("eval_count", "prompt_eval_count")
        if k in data
    } or None

    return Result(
        "done",
        0,
        output=data.get("response", ""),
        usage=usage,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
