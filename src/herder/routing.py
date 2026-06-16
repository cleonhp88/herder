"""Provider routing — selects the best available provider for a job.

Implements cooldown-aware routing: scans the providers list starting after the
previously-failed provider (wrap-around), returning the first candidate whose
recent failure count is below the cooldown threshold.

Failure counts are GLOBAL per provider (intentional): a provider failing for
one role is considered failing for all roles — a broken backend is broken for
everyone. Cooldown only has effect for roles with ≥2 providers.

Single-provider roles get no cooldown protection (there is no fallback to
select, so the store is never queried for the fast path).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herder.config import Cooldown
    from herder.db.store import Store

logger = logging.getLogger(__name__)


def select_provider(
    providers: list[str],
    failed_provider: str | None,
    cooldown: "Cooldown",
    store: "Store",
) -> str:
    """Select the best available provider for a job, honouring cooldown state.

    Scanning starts at the position AFTER ``failed_provider`` in the list
    (wrapping around), so the previously-failed provider is tried last.
    When ``failed_provider`` is ``None`` (first enqueue), scanning begins at
    index 0 — the primary provider is preferred.

    If all providers are currently cooling (recent failure count ≥
    ``cooldown.allowed_fails``), the first provider in the list is returned
    and a WARNING is logged.  Cooldown is a routing *hint*, not a hard gate —
    jobs must never be stranded.

    Failure counts are global per provider (intentional): a provider failing
    for one role is considered failing for all roles.  Single-provider roles
    get no cooldown protection.

    Args:
        providers: Ordered list of provider names for the role (≥1 element).
        failed_provider: Provider name that failed on the previous attempt, or
            ``None`` for the initial enqueue.
        cooldown: Cooldown policy (allowed_fails, window_seconds).
        store: SQLite store for querying recent failure counts.

    Returns:
        Name of the selected provider.
    """
    # Fast path: single provider, no store query needed.
    if len(providers) == 1 and failed_provider is None:
        return providers[0]

    # Determine starting index: scan from the slot AFTER failed_provider.
    # Guard: failed_provider may not be in the list (config
    # changed between enqueue and retry) — fall back to index -1 so that
    # the first candidate considered is providers[0].
    if failed_provider is None:
        start_idx = -1  # next index = 0, i.e. primary provider
    else:
        try:
            start_idx = providers.index(failed_provider)
        except ValueError:
            # Provider disappeared from config — start from the beginning.
            start_idx = -1

    n = len(providers)
    for offset in range(n):
        candidate_idx = (start_idx + 1 + offset) % n
        candidate = providers[candidate_idx]
        failures = store.count_recent_failures(candidate, cooldown.window_seconds)
        if failures < cooldown.allowed_fails:
            return candidate

    # All providers are cooling — return primary and warn; never strand a job.
    logger.warning(
        "All %d providers for role are cooling (%d/%d failures within %ds); "
        "routing to primary provider '%s' anyway.",
        n,
        cooldown.allowed_fails,
        cooldown.allowed_fails,
        cooldown.window_seconds,
        providers[0],
    )
    return providers[0]
