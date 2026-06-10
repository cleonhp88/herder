"""Cost estimation from usage metrics.

Maps provider cost_key to USD per-token rates and estimates total cost
from usage dict (tokens in/out). Supports ollama (eval_count, prompt_eval_count)
and API (input_tokens, output_tokens) formats.
"""
from __future__ import annotations

# USD per 1M tokens, keyed by cost_key set on the provider config.
# Conservative/illustrative defaults; operators override in config.
# Local/unknown/missing key → cost unknown (None).
_RATES: dict[str, tuple[float, float]] = {
    "free": (0.0, 0.0),
    "local": (0.0, 0.0),
}


def estimate_cost(usage: dict | None, cost_key: str | None) -> float | None:
    """Estimate USD cost from a usage dict (tokens in/out).

    Supports both ollama format (eval_count, prompt_eval_count) and
    API format (input_tokens, output_tokens).

    Args:
        usage: Dictionary with token counts. May be None.
        cost_key: Provider's cost_key (e.g. "local", "free", or custom key).
                 If None or unknown, returns None.

    Returns:
        Estimated cost in USD (float), or None if tokens unknown or cost_key unknown.
    """
    if not usage:
        return None
    rate = _RATES.get(cost_key or "", None)
    if rate is None:
        return None
    # Support both ollama and API token naming conventions
    inp = usage.get("prompt_eval_count") or usage.get("input_tokens") or 0
    out = usage.get("eval_count") or usage.get("output_tokens") or 0
    in_rate, out_rate = rate
    return round((inp / 1_000_000) * in_rate + (out / 1_000_000) * out_rate, 6)
