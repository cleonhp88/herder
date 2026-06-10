"""Scheduler tick — cron-driven job enqueue with missed-slot marking and idempotency.

Pure logic: takes (cfg, store, now) and processes each enabled schedule.
- Marks missed slots during downtime (bounded by MISSED_LOOKBACK).
- Enqueues jobs exactly once per matching minute (via UNIQUE schedule_runs constraint).
- Guards against daemon restart within the same minute.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from herder.config import Config, ConfigError
from herder.db.store import Store
from herder.services.enqueue import enqueue_job, EnqueueRequest

logger = logging.getLogger(__name__)

# How far back we mark missed slots after downtime (bounded so a month-long
# downtime doesn't insert thousands of rows).
MISSED_LOOKBACK = timedelta(hours=24)


def _floor_minute(dt: datetime) -> datetime:
    """Floor datetime to the minute (zero seconds and microseconds)."""
    return dt.replace(second=0, microsecond=0)


def tick(cfg: Config, store: Store, now: datetime) -> int:
    """One scheduler pass at wall-time `now` (timezone-aware).

    For each enabled schedule:
    1. Mark missed slots (downtime filling) back to MISSED_LOOKBACK or last recorded run.
    2. If the current minute matches the cron expression, enqueue exactly once
       (guarded by UNIQUE schedule_runs constraint + idempotency_key).

    Args:
        cfg: Loaded configuration.
        store: SQLite store.
        now: Current time (timezone-aware UTC), injected for determinism.

    Returns:
        Number of jobs enqueued.
    """
    tz = ZoneInfo(cfg.worker.timezone)
    local_now = _floor_minute(now.astimezone(tz))
    enqueued = 0

    for sch in cfg.schedules:
        # Persist schedule config (upsert).
        store.upsert_schedule(
            id=sch.id,
            cron=sch.cron,
            project=sch.project,
            role=sch.role,
            kind=sch.kind,
            prompt_file=sch.prompt_file,
            enabled=sch.enabled,
        )

        if not sch.enabled:
            continue

        # 1) Mark missed slots between last recorded run and now (exclusive).
        last = store.last_scheduled_for(sch.id)
        if last:
            start = max(
                datetime.fromisoformat(last).astimezone(tz),
                local_now - MISSED_LOOKBACK,
            )
            it = croniter(sch.cron, start)
            while True:
                nxt = it.get_next(datetime)
                if nxt >= local_now:
                    break
                # Normalize scheduled_for to UTC
                nxt_utc = nxt.astimezone(timezone.utc)
                store.record_schedule_run(sch.id, nxt_utc.isoformat(), "missed")

        # 2) Due now?
        if not croniter.match(sch.cron, local_now):
            continue

        # Normalize scheduled_for to UTC
        scheduled_for = local_now.astimezone(timezone.utc).isoformat()

        # Atomic transaction: record schedule_run then enqueue job
        store.conn.execute("BEGIN IMMEDIATE")
        try:
            # Attempt to record this run. If it already exists (duplicate tick
            # from restart), rollback and skip.
            if not store.record_schedule_run(sch.id, scheduled_for, "enqueued"):
                store.conn.execute("ROLLBACK")
                continue  # duplicate tick in the same minute (restart) → skip

            # Load and enqueue the job.
            prompt = Path(sch.prompt_file).read_text(encoding="utf-8")
            req = EnqueueRequest(
                project=sch.project,
                role=sch.role,
                kind=sch.kind,
                prompt=prompt,
                idempotency_key=f"{sch.id}:{scheduled_for}",
            )
            res = enqueue_job(cfg, store, req)
            store.set_schedule_run_job_id(sch.id, scheduled_for, res.job_id)
            store.conn.execute("COMMIT")
            enqueued += 1
        except Exception as e:  # any failure → rollback, slot unrecorded, retried next tick
            store.conn.execute("ROLLBACK")
            logger.error("schedule %s failed to enqueue: %s", sch.id, e)

    return enqueued
