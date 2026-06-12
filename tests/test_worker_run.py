"""Tests for worker job execution end-to-end (via cat CLI provider)."""
from pathlib import Path

import herder.loops.supervisor as sup
from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import EnqueueRequest, enqueue_job


def _cfg(tmp_path) -> str:
    """Create a minimal test config with cat CLI provider."""
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    return str(c)


def test_run_pending_once_completes_cat_job(herder_home, tmp_path):
    """run_pending_once claims and executes one cat job, writes result.md, returns count=1."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p",
            role="planner",
            kind="research",
            prompt="hello from cat",
        ),
    )
    n = run_pending_once(cfg, store, "w1", 3600)
    assert n == 1

    done = store.list_jobs(status="done")
    assert len(done) == 1
    rd = Path(done[0]["run_dir"])
    result = (rd / "result.md").read_text(encoding="utf-8")
    assert result.startswith("---\n")
    assert "job_id:" in result
    assert "hello from cat" in result  # cat echoed the prompt into the body
    assert done[0]["output_path"] == str(rd / "result.md")

    # exactly one attempt recorded, marked done, with provider populated
    rows = store.conn.execute("SELECT * FROM attempts").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "done"
    assert rows[0]["provider"] == "echo_cli", (
        f"Expected attempt provider='echo_cli', got {rows[0]['provider']!r}"
    )


def test_run_pending_once_empty_queue_returns_zero(herder_home, tmp_path):
    """run_pending_once with no pending jobs returns 0."""
    cfg = load_config(_cfg(tmp_path))
    assert run_pending_once(cfg, Store.open(), "w1", 3600) == 0


def test_run_pending_once_isolates_poison_job(herder_home, tmp_path):
    """A job with missing provider crashes execute_job; the loop must
    finalize it as failed and still process a following good job."""
    from herder.runspace import create_run_dir, snapshot_prompt
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # poison job: provider not in cfg
    rd = create_run_dir("job_poison")
    pp, ph = snapshot_prompt(rd, "x")
    store.enqueue(
        id="job_poison",
        kind="research",
        role="planner",
        provider="ghost",
        project="p",
        cwd=str(tmp_path),
        workspace_mode="readonly",
        permissions="{}",
        status="pending",
        prompt_path=str(pp),
        prompt_hash=ph,
        run_dir=str(rd),
    )

    # good job via the normal service
    enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p",
            role="planner",
            kind="research",
            prompt="good one",
        ),
    )

    # run_pending_once should process both and not crash
    n = run_pending_once(cfg, store, "w1", 3600)
    assert n == 2

    # poison job finalized as failed, not a zombie
    poison = store.get_job("job_poison")
    assert poison["status"] == "failed" and poison["error_type"] == "internal"
    assert poison["worker_id"] is None and poison["lease_until"] is None

    # good job was still processed and completed
    done_jobs = store.list_jobs(status="done")
    assert len(done_jobs) == 1

    # attempt row exists for poison job
    attempt_count = store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE job_id='job_poison'"
    ).fetchone()[0]
    assert attempt_count == 1


def test_long_job_lease_renewed_beyond_initial(herder_home, tmp_path, monkeypatch):
    """A long-running job's lease is renewed during execution via heartbeat.

    With lease_seconds=1 and job sleep 2s, the job would normally be reclaimable
    mid-run by another worker. But with heartbeat renewal every 0.3s, the lease
    is continuously extended, so the job remains locked and completes successfully.
    """
    # Create config with short lease
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  sleeper: {type: cli, executable: sh, args: ['-c', 'sleep 2'], input: stdin, parser: text, timeout: 30}\n"
        "roles:\n"
        "  r: {provider: sleeper, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 1}\n"
    )

    cfg = load_config(str(c))

    # Speed up heartbeat so 2s job renews several times
    monkeypatch.setattr(sup, "HEARTBEAT_INTERVAL", 0.3, raising=False)

    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="r", kind="automation", prompt="x"),
    )
    run_pending_once(cfg, store, "w1", 1)
    j = store.get_job(r.job_id)

    # Job should complete despite initial 1s lease, because heartbeat renews it
    assert j["status"] == "done"
    assert j["heartbeat_at"] is not None, "Renewal happened during the run"
