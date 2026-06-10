"""Tests for Store worker claim/finish/attempt operations."""
from herder.db.store import Store


BASE = dict(
    kind="research",
    role="planner",
    provider="echo_cli",
    project="p",
    cwd="/tmp/x",
    workspace_mode="readonly",
    permissions="{}",
    status="pending",
    prompt_path="/tmp/x/prompt.md",
    prompt_hash="abc",
    run_dir="/tmp/x",
)


def _add(s, jid, **over):
    """Helper to enqueue a job with overrides."""
    f = dict(BASE, id=jid)
    f.update(over)
    s.enqueue(**f)
    return jid


def test_claim_marks_running_with_lease(herder_home):
    """claim_job atomically claims a pending job, sets running + lease + worker_id."""
    s = Store.open()
    _add(s, "job_A")
    row = s.claim_job("w1", 3600)
    assert row is not None
    assert row["id"] == "job_A"
    assert row["status"] == "running"
    assert row["worker_id"] == "w1"
    assert row["lease_until"] is not None
    assert row["attempts"] == 1


def test_claim_returns_none_when_no_pending(herder_home):
    """claim_job returns None when no pending jobs."""
    s = Store.open()
    assert s.claim_job("w1", 3600) is None


def test_claim_respects_priority_then_fifo(herder_home):
    """claim_job orders by priority DESC, then created_at ASC."""
    s = Store.open()
    _add(s, "job_low", priority=0)
    _add(s, "job_high", priority=5)
    row = s.claim_job("w1", 3600)
    assert row["id"] == "job_high"


def test_claim_reclaims_expired_lease(herder_home):
    """claim_job reclaims a job with expired lease, increments attempts."""
    s = Store.open()
    _add(s, "job_A")
    row1 = s.claim_job("w1", 3600)
    assert row1["worker_id"] == "w1"
    assert row1["attempts"] == 1

    # Try to claim again (should fail due to active lease)
    assert s.claim_job("w2", 3600) is None

    # Expire the lease
    s.conn.execute(
        "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id='job_A'"
    )
    row2 = s.claim_job("w2", 3600)
    assert row2 is not None
    assert row2["worker_id"] == "w2"
    assert row2["attempts"] == 2


def test_finish_job_sets_terminal_state(herder_home):
    """finish_job clears worker_id/lease_until, sets output_path/finished_at/status."""
    s = Store.open()
    _add(s, "job_A")
    s.claim_job("w1", 3600)
    s.finish_job("job_A", "done", output_path="/tmp/x/result.md")
    j = s.get_job("job_A")
    assert j["status"] == "done"
    assert j["finished_at"] is not None
    assert j["output_path"] == "/tmp/x/result.md"
    assert j["worker_id"] is None
    assert j["lease_until"] is None


def test_record_attempt(herder_home):
    """record_attempt inserts a row into attempts table."""
    s = Store.open()
    _add(s, "job_A")
    s.record_attempt(
        job_id="job_A",
        attempt_no=1,
        worker_id="w1",
        exit_code=0,
        status="done",
        stdout_path="/tmp/x/stdout.log",
    )
    rows = s.conn.execute(
        "SELECT * FROM attempts WHERE job_id='job_A'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["attempt_no"] == 1


def test_claim_reclaims_expired_lease_non_utc_offset(herder_home):
    """claim_job must reclaim expired leases with non-UTC timezone offsets.

    Uses julianday() for robust timestamp comparison, immune to timezone offset differences.
    """
    s = Store.open()

    # Expired lease in the past with +07:00 offset (way before now)
    _add(s, "job_A")
    s.claim_job("w1", 3600)
    s.conn.execute(
        "UPDATE jobs SET lease_until='2000-01-01T00:00:00+07:00' WHERE id='job_A'"
    )
    row = s.claim_job("w2", 3600)
    assert row is not None and row["worker_id"] == "w2", "Should reclaim expired lease with +07:00"


def test_claim_preserves_started_at_on_reclaim(herder_home):
    """claim_job should preserve the original started_at on reclaim, not update it."""
    s = Store.open()
    _add(s, "job_A")
    first_claim = s.claim_job("w1", 3600)
    first_started = first_claim["started_at"]

    # Expire the lease and reclaim
    s.conn.execute(
        "UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id='job_A'"
    )
    second_claim = s.claim_job("w2", 3600)
    second_started = second_claim["started_at"]

    assert second_started == first_started, "started_at should be preserved on reclaim"


def test_request_cancel_pending_goes_terminal(herder_home):
    """request_cancel on pending job: status → cancelled, finished_at set."""
    s = Store.open()
    _add(s, "job_A")
    result = s.request_cancel("job_A")
    assert result == "cancelled"
    j = s.get_job("job_A")
    assert j["status"] == "cancelled"
    assert j["finished_at"] is not None


def test_request_cancel_running_flags_cancelling(herder_home):
    """request_cancel on running job: status → cancelling (worker will finish)."""
    s = Store.open()
    _add(s, "job_A")
    s.claim_job("w1", 3600)
    result = s.request_cancel("job_A")
    assert result == "cancelling"
    j = s.get_job("job_A")
    assert j["status"] == "cancelling"


def test_request_cancel_done_is_noop(herder_home):
    """request_cancel on done job: returns status unchanged."""
    s = Store.open()
    _add(s, "job_A")
    s.claim_job("w1", 3600)
    s.finish_job("job_A", "done")
    result = s.request_cancel("job_A")
    assert result == "done"


def test_request_cancel_unknown_returns_none(herder_home):
    """request_cancel on unknown job: returns None."""
    s = Store.open()
    result = s.request_cancel("job_nope")
    assert result is None


def test_approve_waiting_job(herder_home):
    """approve_job: waiting_approval → approved (now claimable)."""
    s = Store.open()
    _add(s, "job_A", status="waiting_approval")
    assert s.approve_job("job_A") == "approved"
    assert s.get_job("job_A")["status"] == "approved"
    # now claimable
    assert s.claim_job("w1", 3600)["id"] == "job_A"


def test_reject_waiting_job_terminal(herder_home):
    """reject_job: waiting_approval → rejected (terminal, never claimable)."""
    s = Store.open()
    _add(s, "job_A", status="waiting_approval")
    assert s.reject_job("job_A") == "rejected"
    j = s.get_job("job_A")
    assert j["status"] == "rejected" and j["finished_at"] is not None
    assert s.claim_job("w1", 3600) is None


def test_approve_non_waiting_is_noop(herder_home):
    """approve_job on non-waiting job: status unchanged."""
    s = Store.open()
    _add(s, "job_A")  # pending
    assert s.approve_job("job_A") == "pending"  # unchanged
    assert s.reject_job("job_A") == "pending"


def test_approve_unknown_returns_none(herder_home):
    """approve_job/reject_job on unknown job: returns None."""
    s = Store.open()
    assert s.approve_job("job_nope") is None
    assert s.reject_job("job_nope") is None


def test_waiting_approval_never_claimed(herder_home):
    """waiting_approval jobs are never claimed by workers."""
    s = Store.open()
    _add(s, "job_A", status="waiting_approval")
    assert s.claim_job("w1", 3600) is None


def test_renew_lease_extends_running_job(herder_home):
    """renew_lease extends a running job's lease and updates heartbeat_at."""
    s = Store.open()
    _add(s, "job_A")
    row = s.claim_job("w1", 10)
    old_lease = row["lease_until"]
    s.renew_lease("job_A", "w1", 3600)
    j = s.get_job("job_A")
    assert j["lease_until"] > old_lease
    assert j["heartbeat_at"] is not None


def test_renew_lease_ignores_other_worker(herder_home):
    """renew_lease is only effective if the calling worker owns the job."""
    s = Store.open()
    _add(s, "job_A")
    row = s.claim_job("w1", 10)
    s.renew_lease("job_A", "w2", 3600)  # not the owner → no-op
    assert s.get_job("job_A")["lease_until"] == row["lease_until"]


def test_register_and_heartbeat_worker(herder_home):
    """register_worker inserts a worker row; worker_heartbeat updates last_heartbeat_at."""
    s = Store.open()
    s.register_worker("w1", version="0.1.0")
    s.worker_heartbeat("w1")
    row = s.conn.execute("SELECT * FROM workers WHERE worker_id='w1'").fetchone()
    assert row is not None
    assert row["status"] == "running"
    assert row["last_heartbeat_at"] is not None
    assert row["pid"] > 0
    assert row["hostname"] is not None


def test_active_lease_blocks_second_claim_renewal_keeps_blocking(herder_home):
    """Renewing a lease keeps it locked against competing claims.
    
    Demonstrates that a heartbeat (renewal) extends the lease,
    preventing another worker from reclaiming the job even after
    the original lease would have naturally expired.
    """
    import time
    s = Store.open()
    _add(s, "job_A")
    s.claim_job("w1", 1)                       # 1s lease
    s.renew_lease("job_A", "w1", 3600)         # heartbeat extends to 1h
    time.sleep(1.1)               # original lease would have expired
    assert s.claim_job("w2", 3600) is None     # renewal keeps it locked


def test_without_renewal_expired_lease_is_stolen(herder_home):
    """Without renewal, an expired lease becomes reclaimable by another worker.
    
    Demonstrates the inverse: if the owning worker fails to heartbeat,
    the lease expires and the job can be claimed by another worker.
    """
    import time
    s = Store.open()
    _add(s, "job_A")
    s.claim_job("w1", 1)
    time.sleep(1.1)
    row = s.claim_job("w2", 3600)              # no heartbeat → reclaimable
    assert row is not None and row["worker_id"] == "w2"
