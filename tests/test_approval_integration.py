"""Integration tests for approval gate workflow."""
from pathlib import Path

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path: Path) -> str:
    """Create a minimal config with inplace_write permission (requires approval)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  ops: {provider: echo_cli, permissions: inplace_write}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [ops]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def test_require_confirm_job_waits_then_runs_after_approve(herder_home, tmp_path):
    """Job with require_confirm waits for approval before worker claims it."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # Enqueue a job with inplace_write (requires approval)
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="ops", kind="automation", prompt="x"),
    )
    assert r.status == "waiting_approval"

    # Worker pass must NOT touch waiting_approval jobs
    claimed = run_pending_once(cfg, store, "w1", 3600)
    assert claimed == 0
    assert store.get_job(r.job_id)["status"] == "waiting_approval"

    # Approve the job
    store.approve_job(r.job_id)
    assert store.get_job(r.job_id)["status"] == "approved"

    # Now worker can claim and run it
    claimed = run_pending_once(cfg, store, "w1", 3600)
    assert claimed == 1
    assert store.get_job(r.job_id)["status"] == "done"


def test_rejected_job_never_runs(herder_home, tmp_path):
    """Rejected job remains terminal and never runs."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="ops", kind="automation", prompt="x"),
    )
    assert r.status == "waiting_approval"

    # Reject the job
    store.reject_job(r.job_id)
    assert store.get_job(r.job_id)["status"] == "rejected"

    # Worker pass should find nothing to run
    claimed = run_pending_once(cfg, store, "w1", 3600)
    assert claimed == 0
    assert store.get_job(r.job_id)["status"] == "rejected"


def test_pending_job_runs_immediately(herder_home, tmp_path):
    """Job without require_confirm runs immediately without approval gate."""
    # Create config with readonly permissions (no require_confirm)
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  viewer: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [viewer]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )

    cfg = load_config(str(c))
    store = Store.open()

    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="viewer", kind="automation", prompt="x"),
    )
    assert r.status == "pending"

    # Worker claims and runs immediately (no approval gate)
    claimed = run_pending_once(cfg, store, "w1", 3600)
    assert claimed == 1
    assert store.get_job(r.job_id)["status"] == "done"
