"""Integration test for job cancellation end-to-end."""
import threading
import time

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _test_config(tmp_path) -> str:
    """Generate a minimal test config with sleeper provider."""
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  sleeper: {type: cli, executable: sh, args: ['-c', 'sleep 20'], input: stdin, parser: text, timeout: 60}\n"
        "roles:\n  napper: {provider: sleeper, permissions: read_only}\n"
        f"projects:\n  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [napper]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def test_cancel_running_job_end_to_end(herder_home, tmp_path):
    """Request cancel on a running job; worker detects and kills it quickly."""
    cfg = load_config(_test_config(tmp_path))
    store = Store.open()

    # Enqueue a job
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p", role="napper", kind="automation", prompt="zzz"
        ),
    )
    jid = r.job_id

    # Background thread that waits for job to start, then requests cancel
    def canceller():
        deadline = time.monotonic() + 15
        s2 = Store.open()
        while time.monotonic() < deadline:
            row = s2.get_job(jid)
            if row and row["status"] == "running":
                s2.request_cancel(jid)
                return
            time.sleep(0.1)

    t = threading.Thread(target=canceller)
    t.start()

    t0 = time.monotonic()
    # Worker loop: claim and execute the job
    run_pending_once(cfg, store, "w1", 3600)
    elapsed = time.monotonic() - t0
    t.join()

    # Verify job was cancelled and execution was fast
    j = store.get_job(jid)
    assert j["status"] == "cancelled", f"Expected cancelled, got {j['status']}"
    assert elapsed < 15, f"Took {elapsed}s; should complete within grace period, not full 20s"

    # Verify attempt was recorded with cancelled status
    rows = store.conn.execute(
        "SELECT status FROM attempts WHERE job_id=?", (jid,)
    ).fetchall()
    assert rows and rows[0]["status"] == "cancelled", "Attempt should be recorded as cancelled"
