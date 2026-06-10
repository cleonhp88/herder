"""Worker queue processor — claims and executes all pending jobs."""
from __future__ import annotations

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

from herder.config import Config
from herder.db.store import Store
from herder.errors import is_retryable
from herder.loops.supervisor import execute_job

logger = logging.getLogger(__name__)


def _process_claimed(cfg: Config, store: Store, job: sqlite3.Row, worker_id: str) -> None:
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
            # otherwise mark dead. Note: immediate requeue (no backoff timer yet —
            # would need a next_eligible_at column; schema v2 candidate).
            if (
                is_retryable(fresh["error_type"])
                and fresh["attempts"] < fresh["max_retries"]
            ):
                store.requeue(job["id"])
            elif fresh["attempts"] >= fresh["max_retries"]:
                store.mark_dead(job["id"])
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
        store.finish_job(job["id"], "failed", error_type="internal")


def run_pending_once(cfg: Config, store: Store, worker_id: str, lease_seconds: int) -> int:
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


def run_pending_parallel(cfg: Config, store: Store, worker_id: str, lease_seconds: int) -> int:
    """Parallel pass: claim jobs and execute them on a thread pool.

    Jobs on different providers run concurrently (bounded by cfg.worker.global_concurrency).
    Jobs on the same provider respect the provider's max_concurrency via per-provider
    semaphores. Each worker thread opens its OWN Store (SQLite connections are not
    thread-safe).

    Note: A thread saturated on a provider semaphore holds a pool slot until that
    provider frees up. Since semaphore holders are always running jobs, this is
    bounded and deadlock-free.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence (used only in the main thread to claim jobs).
        worker_id: ID of the worker running jobs.
        lease_seconds: Lease duration in seconds for claimed jobs.

    Returns:
        Count of jobs successfully executed (or attempted).
    """
    # Create a semaphore for each provider to enforce per-provider concurrency limits.
    semaphores = {name: threading.Semaphore(p.max_concurrency) for name, p in cfg.providers.items()}
    count = 0
    futures = set()

    def _worker(job_row: sqlite3.Row) -> None:
        """Worker thread function: acquire provider semaphore, execute, release."""
        provider_name = job_row["provider"]
        sema = semaphores.get(provider_name)
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
