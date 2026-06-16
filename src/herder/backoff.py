"""Exponential backoff computation for auto-retry delays."""

from __future__ import annotations


def compute_backoff_seconds(
    attempts: int,
    base_seconds: int,
    max_seconds: int,
) -> int:
    """Compute exponential backoff delay for the Nth retry.

    Returns 0 when disabled (``base_seconds == 0``), preserving the
    default immediate-requeue behaviour.

    Formula: ``min(max_seconds, base_seconds * 2 ** (attempts - 1))``,
    floored at 0. ``attempts`` is the job's attempt count *after* the
    failed attempt (1-indexed), so the first retry waits ``base_seconds``,
    the second ``2 * base_seconds``, etc.

    Args:
        attempts: Attempt count after the failed attempt (1-indexed).
            Values ≤ 0 are treated as 0 (no prior attempts → no delay).
        base_seconds: Base delay in seconds. 0 disables backoff entirely.
        max_seconds: Upper cap on the computed delay (must be > 0).

    Returns:
        Delay in seconds (0 when backoff is disabled or attempts ≤ 0).

    Example:
        >>> compute_backoff_seconds(1, base_seconds=5, max_seconds=300)
        5
        >>> compute_backoff_seconds(2, base_seconds=5, max_seconds=300)
        10
        >>> compute_backoff_seconds(0, base_seconds=5, max_seconds=300)
        0
        >>> compute_backoff_seconds(1, base_seconds=0, max_seconds=300)
        0
    """
    if base_seconds <= 0:
        return 0
    exp = max(0, attempts - 1)
    return min(max_seconds, base_seconds * (2**exp))
