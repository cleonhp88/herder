"""Tests for Tier 2 store methods: provider column, count_recent_failures, requeue."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from herder.db.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enqueue(store: Store, jid: str) -> None:
    """Enqueue a minimal job for use in attempt tests."""
    store.enqueue(
        id=jid,
        kind="research",
        role="r",
        provider="prov",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
    )


# ---------------------------------------------------------------------------
# count_recent_failures
# ---------------------------------------------------------------------------

def test_count_recent_failures_empty_db(herder_home):
    """count_recent_failures returns 0 on an empty database."""
    store = Store.open()
    assert store.count_recent_failures("myprov", 300) == 0


def test_count_recent_failures_counts_failed_and_timeout(herder_home):
    """count_recent_failures counts 'failed' AND 'timeout', never 'done'.

    Timeouts must accumulate cooldown weight: a hanging backend is the most
    common sickness signal — counting only loud failures would let routing
    keep selecting a hung provider forever.
    """
    store = Store.open()
    _enqueue(store, "j1")
    _enqueue(store, "j2")
    _enqueue(store, "j3")

    now_iso = datetime.now(timezone.utc).isoformat()
    store.record_attempt(job_id="j1", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider="myprov",
                         finished_at=now_iso)
    store.record_attempt(job_id="j2", attempt_no=1, worker_id="w",
                         exit_code=0, status="done", provider="myprov",
                         finished_at=now_iso)
    store.record_attempt(job_id="j3", attempt_no=1, worker_id="w",
                         exit_code=None, status="timeout", provider="myprov",
                         finished_at=now_iso)

    assert store.count_recent_failures("myprov", 300) == 2


def test_select_provider_skips_timing_out_provider(herder_home):
    """select_provider skips a provider whose recent attempts are timeouts."""
    from herder.config import Cooldown
    from herder.routing import select_provider

    store = Store.open()
    now = datetime.now(timezone.utc)
    for i in range(3):
        _enqueue(store, f"t{i}")
        store.record_attempt(job_id=f"t{i}", attempt_no=1, worker_id="w",
                             exit_code=None, status="timeout", provider="hung",
                             finished_at=now.isoformat())

    chosen = select_provider(["hung", "healthy"], None, Cooldown(), store)
    assert chosen == "healthy"


def test_count_recent_failures_respects_window(herder_home):
    """count_recent_failures excludes failures older than window_seconds.

    This test is the canary for the datetime comparison bug: Python isoformat
    strings ('T' separator) must compare correctly against stored isoformat
    strings without falling back to SQLite datetime() which uses a space separator.
    """
    store = Store.open()
    _enqueue(store, "old_job")
    _enqueue(store, "new_job")

    # Attempt finished 10 minutes ago — outside a 5-minute window
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    # Attempt finished 1 minute ago — inside a 5-minute window
    new_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    store.record_attempt(job_id="old_job", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider="myprov",
                         finished_at=old_ts)
    store.record_attempt(job_id="new_job", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider="myprov",
                         finished_at=new_ts)

    window_5min = 300
    result = store.count_recent_failures("myprov", window_5min)
    # Only the recent failure (1 min ago) should be counted
    assert result == 1, (
        f"Expected 1 (only recent failure), got {result}. "
        "This likely indicates a datetime comparison bug (isoformat 'T' vs SQLite space)."
    )


def test_count_recent_failures_null_provider_not_counted(herder_home):
    """Attempts with NULL provider are never counted (pre-Tier2 history)."""
    store = Store.open()
    _enqueue(store, "j1")

    now_iso = datetime.now(timezone.utc).isoformat()
    store.record_attempt(job_id="j1", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider=None,
                         finished_at=now_iso)

    assert store.count_recent_failures("myprov", 300) == 0


def test_count_recent_failures_only_matching_provider(herder_home):
    """count_recent_failures is scoped to the specified provider only."""
    store = Store.open()
    _enqueue(store, "j1")
    _enqueue(store, "j2")

    now_iso = datetime.now(timezone.utc).isoformat()
    store.record_attempt(job_id="j1", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider="provA",
                         finished_at=now_iso)
    store.record_attempt(job_id="j2", attempt_no=1, worker_id="w",
                         exit_code=1, status="failed", provider="provB",
                         finished_at=now_iso)

    assert store.count_recent_failures("provA", 300) == 1
    assert store.count_recent_failures("provB", 300) == 1
    assert store.count_recent_failures("provC", 300) == 0


# ---------------------------------------------------------------------------
# record_attempt with/without provider
# ---------------------------------------------------------------------------

def test_record_attempt_with_provider(herder_home):
    """record_attempt persists provider column when given."""
    store = Store.open()
    _enqueue(store, "j1")
    store.record_attempt(job_id="j1", attempt_no=1, worker_id="w",
                         exit_code=0, status="done", provider="myprov")
    row = store.conn.execute(
        "SELECT provider FROM attempts WHERE job_id='j1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "myprov"


def test_record_attempt_without_provider(herder_home):
    """record_attempt stores NULL for provider when not given (default)."""
    store = Store.open()
    _enqueue(store, "j1")
    store.record_attempt(job_id="j1", attempt_no=1, worker_id="w",
                         exit_code=0, status="done")
    row = store.conn.execute(
        "SELECT provider FROM attempts WHERE job_id='j1'"
    ).fetchone()
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# requeue with/without next_provider
# ---------------------------------------------------------------------------

def test_requeue_without_next_provider_preserves_provider(herder_home):
    """requeue() without next_provider leaves jobs.provider unchanged."""
    store = Store.open()
    store.enqueue(
        id="j1",
        kind="research",
        role="r",
        provider="original_prov",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="failed",
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
    )
    store.requeue("j1")
    job = store.get_job("j1")
    assert job["status"] == "pending"
    assert job["provider"] == "original_prov"


def test_requeue_with_next_provider_updates_provider(herder_home):
    """requeue(next_provider=...) atomically updates provider in one UPDATE."""
    store = Store.open()
    store.enqueue(
        id="j1",
        kind="research",
        role="r",
        provider="original_prov",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="failed",
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
    )
    store.requeue("j1", next_provider="new_prov")
    job = store.get_job("j1")
    assert job["status"] == "pending"
    assert job["provider"] == "new_prov"
