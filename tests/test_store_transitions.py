"""Store-level FSM transition tests.

Verifies that the guarded mutators (finish_job, requeue, mark_dead) enforce
LEGAL_TRANSITIONS, write job_events rows, are idempotent where specified, and
that the v4 migration is applied correctly.
"""

from __future__ import annotations

import pytest

from herder.db.store import Store
from herder.transitions import IllegalTransitionError

# ── helpers ──────────────────────────────────────────────────────────────────

_BASE = dict(
    kind="research",
    role=None,
    provider="echo",
    project=None,
    cwd="/tmp/x",
    workspace_mode="readonly",
    permissions="{}",
    prompt_path="/tmp/x/p.md",
    prompt_hash="abc123",
    run_dir="/tmp/x",
)


def _add(store: Store, jid: str, status: str = "pending") -> str:
    """Enqueue a job with the given status and return its id.

    Args:
        store: Open Store instance.
        jid: Job id.
        status: Initial status (direct INSERT — no FSM guard on enqueue).

    Returns:
        The job id.
    """
    store.enqueue(id=jid, status=status, **_BASE)
    return jid


def _running(store: Store, jid: str) -> str:
    """Enqueue a job, claim it, and return its id (status = running).

    Args:
        store: Open Store instance.
        jid: Job id.

    Returns:
        The job id.
    """
    _add(store, jid, status="pending")
    store.claim_job("w1", 3600)
    return jid


# ── migration v4 ─────────────────────────────────────────────────────────────


def test_v4_user_version(herder_home) -> None:
    """A fresh database reports PRAGMA user_version == 6 after migration."""
    store = Store.open()
    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 6


def test_v4_job_events_table_exists(herder_home) -> None:
    """Schema v4 creates the job_events table."""
    store = Store.open()
    tables = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "job_events" in tables


def test_v4_job_events_index_exists(herder_home) -> None:
    """Schema v4 creates idx_job_events_job index."""
    store = Store.open()
    idx_names = {
        row[1]
        for row in store.conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_job_events_job" in idx_names


def test_v4_open_idempotent(herder_home) -> None:
    """Opening the database twice still yields user_version == 6."""
    Store.open()
    store = Store.open()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 6


# ── THE critical resurrection guard ──────────────────────────────────────────


def test_requeue_on_done_job_raises(herder_home) -> None:
    """requeue() on a terminal done job raises IllegalTransitionError (resurrection guard).

    This is the primary correctness guarantee of the FSM: a finished job can
    never be silently re-queued and re-run, which was previously possible via
    a bare UPDATE.
    """
    store = Store.open()
    _running(store, "job_done")
    store.finish_job("job_done", "done")
    with pytest.raises(IllegalTransitionError, match=r"done.*pending"):
        store.requeue("job_done")


def test_requeue_on_rejected_job_raises(herder_home) -> None:
    """requeue() on a rejected (terminal) job raises IllegalTransitionError."""
    store = Store.open()
    _add(store, "job_rej", status="rejected")
    with pytest.raises(IllegalTransitionError):
        store.requeue("job_rej")


# ── finish_job guard ─────────────────────────────────────────────────────────


def test_finish_job_legal_running_to_done(herder_home) -> None:
    """finish_job(running → done) is legal and persists the status change."""
    store = Store.open()
    _running(store, "job_A")
    store.finish_job("job_A", "done")
    assert store.get_job("job_A")["status"] == "done"


def test_finish_job_legal_running_to_failed(herder_home) -> None:
    """finish_job(running → failed) is legal."""
    store = Store.open()
    _running(store, "job_A")
    store.finish_job("job_A", "failed")
    assert store.get_job("job_A")["status"] == "failed"


def test_finish_job_illegal_pending_to_done_raises(herder_home) -> None:
    """finish_job(pending → done) raises IllegalTransitionError."""
    store = Store.open()
    _add(store, "job_A", status="pending")
    with pytest.raises(IllegalTransitionError):
        store.finish_job("job_A", "done")


def test_finish_job_nonexistent_raises(herder_home) -> None:
    """finish_job on unknown job_id raises IllegalTransitionError."""
    store = Store.open()
    with pytest.raises(IllegalTransitionError, match=r"no such job"):
        store.finish_job("ghost", "done")


def test_finish_job_idempotent(herder_home) -> None:
    """A second finish_job call with the same status is a silent no-op.

    No exception, no duplicate job_events row.
    """
    store = Store.open()
    _running(store, "job_A")
    store.finish_job("job_A", "done")
    # Second call with same status — must not raise and must not add a second event
    store.finish_job("job_A", "done")
    events = store.job_events("job_A")
    assert len(events) == 1  # only the first transition recorded


# ── requeue guard ─────────────────────────────────────────────────────────────


def test_requeue_legal_failed_to_pending(herder_home) -> None:
    """requeue(failed → pending) is legal."""
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.requeue("job_A")
    assert store.get_job("job_A")["status"] == "pending"


def test_requeue_legal_dead_to_pending(herder_home) -> None:
    """requeue(dead → pending) is legal (manual CLI retry)."""
    store = Store.open()
    _add(store, "job_A", status="dead")
    store.requeue("job_A")
    assert store.get_job("job_A")["status"] == "pending"


def test_requeue_legal_cancelled_to_pending(herder_home) -> None:
    """requeue(cancelled → pending) is legal (manual CLI retry)."""
    store = Store.open()
    _add(store, "job_A", status="cancelled")
    store.requeue("job_A")
    assert store.get_job("job_A")["status"] == "pending"


def test_requeue_illegal_running_to_pending_raises(herder_home) -> None:
    """requeue(running → pending) raises IllegalTransitionError."""
    store = Store.open()
    _running(store, "job_A")
    with pytest.raises(IllegalTransitionError):
        store.requeue("job_A")


def test_requeue_idempotent_already_pending(herder_home) -> None:
    """requeue() on a pending job is a silent no-op (already at target)."""
    store = Store.open()
    _add(store, "job_A", status="pending")
    store.requeue("job_A")  # must not raise
    assert store.get_job("job_A")["status"] == "pending"
    assert store.job_events("job_A") == []  # no event written for no-op


def test_requeue_nonexistent_raises(herder_home) -> None:
    """requeue on unknown job_id raises IllegalTransitionError."""
    store = Store.open()
    with pytest.raises(IllegalTransitionError, match=r"no such job"):
        store.requeue("ghost")


# ── mark_dead guard ───────────────────────────────────────────────────────────


def test_mark_dead_legal_failed_to_dead(herder_home) -> None:
    """mark_dead(failed → dead) is legal."""
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.mark_dead("job_A")
    assert store.get_job("job_A")["status"] == "dead"


def test_mark_dead_illegal_pending_to_dead_raises(herder_home) -> None:
    """mark_dead(pending → dead) raises IllegalTransitionError."""
    store = Store.open()
    _add(store, "job_A", status="pending")
    with pytest.raises(IllegalTransitionError):
        store.mark_dead("job_A")


def test_mark_dead_idempotent(herder_home) -> None:
    """A second mark_dead on an already-dead job is a silent no-op."""
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.mark_dead("job_A")
    store.mark_dead("job_A")  # must not raise
    events = store.job_events("job_A")
    assert len(events) == 1  # only one event written


def test_mark_dead_nonexistent_raises(herder_home) -> None:
    """mark_dead on unknown job_id raises IllegalTransitionError."""
    store = Store.open()
    with pytest.raises(IllegalTransitionError, match=r"no such job"):
        store.mark_dead("ghost")


# ── job_events content ────────────────────────────────────────────────────────


def test_finish_job_writes_job_event(herder_home) -> None:
    """finish_job writes exactly one job_events row with correct from/to/reason."""
    store = Store.open()
    _running(store, "job_A")
    store.finish_job("job_A", "done")
    events = store.job_events("job_A")
    assert len(events) == 1
    ev = events[0]
    assert ev["from_status"] == "running"
    assert ev["to_status"] == "done"
    assert ev["reason"] == "finish"
    assert ev["at"] is not None


def test_requeue_writes_job_event(herder_home) -> None:
    """requeue writes exactly one job_events row with correct from/to/reason."""
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.requeue("job_A")
    events = store.job_events("job_A")
    assert len(events) == 1
    ev = events[0]
    assert ev["from_status"] == "failed"
    assert ev["to_status"] == "pending"
    assert ev["reason"] == "requeue"
    assert ev["at"] is not None


def test_mark_dead_writes_job_event(herder_home) -> None:
    """mark_dead writes exactly one job_events row with correct from/to/reason."""
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.mark_dead("job_A")
    events = store.job_events("job_A")
    assert len(events) == 1
    ev = events[0]
    assert ev["from_status"] == "failed"
    assert ev["to_status"] == "dead"
    assert ev["reason"] == "mark_dead"
    assert ev["at"] is not None


def test_job_events_empty_for_new_job(herder_home) -> None:
    """job_events returns an empty list for a newly-enqueued job with no transitions."""
    store = Store.open()
    _add(store, "job_A", status="pending")
    assert store.job_events("job_A") == []


def test_job_events_ordered_by_id(herder_home) -> None:
    """job_events returns rows ordered by id (chronological insertion order).

    Verifies multi-event sequences: failed → pending (requeue) → running →
    failed (finish) produces two events in insertion order.
    """
    store = Store.open()
    _add(store, "job_A", status="failed")
    store.requeue("job_A")  # event 1: failed → pending
    # Claim (pending → running via claim_job, not guarded here) then finish
    store.claim_job("w1", 3600)
    store.finish_job("job_A", "failed")  # event 2: running → failed
    events = store.job_events("job_A")
    assert len(events) == 2
    assert events[0]["from_status"] == "failed"
    assert events[0]["to_status"] == "pending"
    assert events[1]["from_status"] == "running"
    assert events[1]["to_status"] == "failed"
    assert events[0]["id"] < events[1]["id"]


# ── concurrency: 0-row UPDATE must not record a phantom event ──────────────────


def test_finish_job_no_phantom_event_on_lost_race(herder_home, monkeypatch) -> None:
    """A concurrent status change that makes the guarded UPDATE match 0 rows must NOT
    record a phantom job_events row, and must not mutate the committed row.

    Simulates the read→UPDATE window: the in-memory read sees a stale legal source
    ('running') while the committed row is already terminal ('done'), so the
    ``WHERE status IN ('running','cancelling')`` guard matches nothing.
    """
    store = Store.open()
    jid = _add(store, "job_race", status="done")  # committed status is terminal
    # Force finish_job's read to see a stale legal source; the real UPDATE runs against
    # the committed 'done' row and matches 0 rows.
    monkeypatch.setattr(store, "get_job", lambda _jid: {"status": "running"})
    store.finish_job(jid, "failed")  # running->failed legal per the stale read
    assert store.job_events(jid) == []  # no phantom audit event
    real = store.conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
    assert real["status"] == "done"  # committed row untouched
