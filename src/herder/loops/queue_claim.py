"""Worker queue processor — claims and executes all pending jobs."""

from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime, timedelta, timezone

from herder.backoff import compute_backoff_seconds
from herder.config import Config, Provider
from herder.db.store import Store
from herder.errors import is_retryable
from herder.loops.supervisor import execute_job
from herder.routing import select_provider
from herder.transitions import IllegalTransitionError

logger = logging.getLogger(__name__)


def _group_key(name: str, provider: Provider) -> str:
    """Return the concurrency-group key for a provider.

    A provider with an explicit ``concurrency_group`` shares that semaphore
    with all other providers in the same group.  A provider with no group is
    its own group, keyed by provider name — preserving prior per-provider
    behaviour exactly.

    Args:
        name: The provider's config-map key (its name).
        provider: The Provider configuration object.

    Returns:
        The string key used to look up the shared semaphore.
    """
    return provider.concurrency_group or name


def _process_claimed(
    cfg: Config, store: Store, job: sqlite3.Row, worker_id: str
) -> None:
    """Execute one claimed job with crash isolation + retry policy.

    Used by both the serial and parallel passes. Applies immediate requeue
    for transient failures (bounded by max_retries), marks dead after exhaustion,
    and isolates poison jobs so a single crash never kills the entire pass.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence.
        job: The claimed job row (from store.claim_job).
        worker_id: ID of the worker running the job.
    """
    try:
        outcome = execute_job(cfg, store, job, worker_id)
        if outcome == "failed":
            fresh = store.get_job(job["id"])
            # Apply retry policy: requeue if retryable and under max_retries,
            # otherwise mark dead.
            try:
                if (
                    is_retryable(fresh["error_type"])
                    and fresh["attempts"] < fresh["max_retries"]
                ):
                    # Compute optional backoff delay (0 when base_seconds == 0,
                    # which is the default — preserves immediate-requeue behaviour).
                    delay = compute_backoff_seconds(
                        fresh["attempts"],
                        cfg.worker.retry_backoff_base_seconds,
                        cfg.worker.retry_backoff_max_seconds,
                    )
                    nea = (
                        (
                            datetime.now(timezone.utc) + timedelta(seconds=delay)
                        ).isoformat()
                        if delay > 0
                        else None
                    )
                    # Advance-on-requeue: when the role has multiple
                    # providers, select the next provider respecting cooldown state.
                    # Guard: role may be None (ad-hoc jobs have no role).
                    role_name = fresh["role"]
                    role_obj = cfg.roles.get(role_name) if role_name else None
                    if role_obj is not None and len(role_obj.providers) > 1:
                        next_p = select_provider(
                            role_obj.providers,
                            fresh["provider"],
                            role_obj.cooldown,
                            store,
                        )
                        store.requeue(
                            job["id"], next_provider=next_p, next_eligible_at=nea
                        )
                    else:
                        # Legacy / single-provider: keep current provider.
                        store.requeue(job["id"], next_eligible_at=nea)
                elif fresh["attempts"] >= fresh["max_retries"]:
                    store.mark_dead(job["id"])
            except IllegalTransitionError as ite:
                # Another worker reclaimed this job after lease expiry and finalized it
                # before this retry decision ran. The decision is moot — log benignly and
                # do NOT fall through to the crash handler (which would log a misleading
                # "crashed" + write a spurious failed/internal attempt).
                logger.warning(
                    "job %s retry policy skipped (concurrent terminal): %s",
                    job["id"],
                    ite,
                )
    except Exception as e:  # isolate poison jobs — never let one kill the pass
        logger.error("job %s crashed in execute_job: %s", job["id"], e)
        try:
            store.record_attempt(
                job_id=job["id"],
                attempt_no=job["attempts"],
                worker_id=worker_id,
                exit_code=None,
                status="failed",
                error_type="internal",
            )
        except Exception:
            logger.exception("failed to record attempt for %s", job["id"])
        try:
            store.finish_job(job["id"], "failed", error_type="internal")
        except IllegalTransitionError as ite:
            logger.warning(
                "job %s crash-path finish_job skipped (already terminal): %s",
                job["id"],
                ite,
            )


def run_pending_once(
    cfg: Config, store: Store, worker_id: str, lease_seconds: int
) -> int:
    """Claim and execute every currently-claimable job, serially. Returns count processed.

    Each job is crash-isolated: an exception in one job is recorded and finalized as
    'failed' so the loop continues and the job never strands as a leased zombie.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence.
        worker_id: ID of the worker running jobs.
        lease_seconds: Lease duration in seconds for claimed jobs.

    Returns:
        Count of jobs successfully executed (or attempted).
    """
    count = 0
    while True:
        job = store.claim_job(worker_id, lease_seconds)
        if job is None:
            break
        _process_claimed(cfg, store, job, worker_id)
        count += 1
    return count


def _build_group_limits(providers: dict[str, Provider]) -> dict[str, int]:
    """Compute the semaphore permit count for each concurrency group.

    Providers that share a ``concurrency_group`` are serialised through ONE
    semaphore whose size equals the *minimum* ``max_concurrency`` declared among
    the group's members (tightest constraint wins).  A provider with no
    ``concurrency_group`` forms its own isolated group (keyed by provider name),
    preserving prior per-provider behaviour exactly.

    Args:
        providers: Mapping of provider name to Provider config object.

    Returns:
        Mapping of group key → semaphore permit count.  Suitable for direct
        construction of ``threading.Semaphore`` objects.

    Example:
        >>> limits = _build_group_limits(cfg.providers)
        >>> semaphores = {g: threading.Semaphore(n) for g, n in limits.items()}

    Complexity:
        O(n) where n = len(providers).
    """
    group_limit: dict[str, int] = {}
    for name, provider in providers.items():
        group_key = _group_key(name, provider)
        group_limit[group_key] = min(
            group_limit.get(group_key, provider.max_concurrency),
            provider.max_concurrency,
        )
    return group_limit


def run_pending_parallel(
    cfg: Config, store: Store, worker_id: str, lease_seconds: int
) -> int:
    """Parallel pass: claim jobs and execute them on a thread pool.

    Jobs on different providers run concurrently (bounded by cfg.worker.global_concurrency).
    Jobs on the same provider — or on providers sharing a ``concurrency_group`` — respect
    the group's max_concurrency via a shared semaphore.  Providers in the same
    concurrency_group share ONE permit and are serialised; providers with no
    concurrency_group each get their own semaphore (prior behaviour unchanged).
    Each worker thread opens its OWN Store (SQLite connections are not thread-safe).

    Note: A thread blocked on a group semaphore holds a pool slot until a permit
    frees. Because providers sharing a concurrency_group share ONE permit, up to
    (jobs claimed for that group) pool slots may park on it at once. Still bounded
    and deadlock-free: every permit holder is actively running a job, so at least
    one thread always advances.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence (used only in the main thread to claim jobs).
        worker_id: ID of the worker running jobs.
        lease_seconds: Lease duration in seconds for claimed jobs.

    Returns:
        Count of jobs successfully executed (or attempted).
    """
    # Semaphores keyed by concurrency group: providers on the same host (same group)
    # share ONE permit and are serialised. A provider with no concurrency_group is its
    # own group, preserving prior per-provider behaviour exactly. Group permit count =
    # the tightest (min) max_concurrency among the group's providers.
    group_limit = _build_group_limits(cfg.providers)
    semaphores = {g: threading.Semaphore(limit) for g, limit in group_limit.items()}

    count = 0
    futures = set()

    def _worker(job_row: sqlite3.Row) -> None:
        """Worker thread: acquire group semaphore, execute job, release."""
        provider_name = job_row["provider"]
        provider = cfg.providers.get(provider_name)
        sema = None
        if provider is not None:
            sema = semaphores.get(_group_key(provider_name, provider))
        if sema is not None:
            sema.acquire()
        try:
            # Open a new Store in this thread (SQLite connections are not thread-safe).
            thread_store = Store.open()
            _process_claimed(cfg, thread_store, job_row, worker_id)
        finally:
            if sema is not None:
                sema.release()

    with ThreadPoolExecutor(max_workers=cfg.worker.global_concurrency) as pool:
        while True:
            job = store.claim_job(worker_id, lease_seconds)
            if job is None:
                # No more jobs to claim; if no futures are running, we're done.
                if not futures:
                    break
                # Wait for at least one running job to finish; it may have requeued work.
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                continue
            futures.add(pool.submit(_worker, job))
            count += 1
        # Wait for any remaining running jobs.
        if futures:
            wait(futures)
    return count
