"""Error classification for job failures.

Provides best-effort classification of stderr output to determine error type
and whether a job should be retried.
"""
from __future__ import annotations

import re

# Pattern groups for error classification.
# Format: (tuple of needles, error_type)
# First match wins, so order matters: auth before permission because
# "unauthorized" must not fall through to a generic bucket.
_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (
        (
            "401",
            "unauthorized",
            "not authenticated",
            "sign in",
            "login required",
            "refresh token",
            "token expired",
            "api key",
            "authentication",
        ),
        "auth",
    ),
    (
        (
            "429",
            "rate limit",
            "too many requests",
            "quota exceeded",
            "overloaded",
        ),
        "rate_limit",
    ),
    (("permission denied", "403", "forbidden"), "permission"),
    (
        (
            "prompt is too long",
            "context length",
            "too many tokens",
            "maximum context",
        ),
        "bad_prompt",
    ),
]

RETRYABLE: frozenset[str] = frozenset(
    {"timeout", "rate_limit", "unavailable", "unknown"}
)


def _matches(needle: str, low: str) -> bool:
    """Match a needle in haystack with boundary checking for numeric needles.

    Numeric needles (e.g., "401") require word boundaries to avoid matching
    inside larger numbers (e.g., "4030 tokens"). Word needles use substring matching.

    Args:
        needle: The pattern to search for.
        low: The lowercase haystack to search in.

    Returns:
        True if the needle matches, False otherwise.
    """
    if needle.isdigit():
        return re.search(rf"(?<!\d){needle}(?!\d)", low) is not None
    return needle in low


def classify_error(stderr: str, exit_code: int | None) -> str:
    """Classify a failed run from its stderr text.

    Performs a best-effort pattern match against stderr. If no pattern matches,
    returns "unknown".

    Args:
        stderr: Stderr output from the failed process.
        exit_code: Process exit code (unused, kept for future expansion).

    Returns:
        Error type string (e.g., "auth", "rate_limit", "unknown").
    """
    low = (stderr or "").lower()
    for needles, etype in _PATTERNS:
        if any(_matches(n, low) for n in needles):
            return etype
    return "unknown"


def is_retryable(error_type: str | None) -> bool:
    """Check if an error type is retryable.

    Args:
        error_type: Error type string or None.

    Returns:
        True if the error should be retried, False otherwise.
    """
    return error_type in RETRYABLE


class BudgetError(Exception):
    """Enqueue refused because a budget/runaway cap was hit."""
