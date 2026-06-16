"""Integration tests for auto-retry policy."""
from pathlib import Path

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path, script: str) -> str:
    """Generate a minimal test config with a custom provider."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        f"  flaky: {{type: cli, executable: sh, args: ['-c', '{script}'], input: stdin, parser: text, timeout: 10}}\n"
        "roles:\n  r: {provider: flaky, permissions: read_only}\n"
        f"projects:\n  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def test_retryable_error_retries_until_dead(herder_home, tmp_path):
    """Generic failure (unknown) is retryable → re-claimed until max_retries, then dead."""
    cfg = load_config(_cfg(tmp_path, "echo boom >&2; exit 1"))
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="r", kind="automation", prompt="x"),
    )
    job_id = r.job_id
    job = store.get_job(job_id)
    max_retries = job["max_retries"]

    # Run worker once to claim and execute all retries.
    # The worker loop will keep claiming the requeued job until max_retries exhausted,
    # then mark it dead — all in one run_pending_once call.
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(job_id)
    # After all retries exhausted, should be marked dead
    assert j["status"] == "dead"
    assert j["attempts"] == max_retries
    assert j["error_type"] == "unknown"

    # Verify all attempts were recorded
    n_attempts = store.conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    assert n_attempts == max_retries


def test_auth_error_not_retried(herder_home, tmp_path):
    """Auth error is non-retryable → stays failed after one attempt."""
    cfg = load_config(_cfg(tmp_path, "echo 401 Unauthorized >&2; exit 1"))
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="r", kind="automation", prompt="x"),
    )
    job_id = r.job_id

    # Run worker once
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(job_id)
    # After execution, should be failed (not requeued)
    assert j["status"] == "failed"
    assert j["error_type"] == "auth"
    assert j["attempts"] == 1


def test_rate_limit_error_retried(herder_home, tmp_path):
    """Rate limit error is retryable → re-queued and retried until dead."""
    cfg = load_config(_cfg(tmp_path, "echo 429error >&2; exit 1"))
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="r", kind="automation", prompt="x"),
    )
    job_id = r.job_id
    job = store.get_job(job_id)
    max_retries = job["max_retries"]

    # Run worker once — should exhaust all retries and mark dead
    # because "429error" matches the "429" pattern in classify_error
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(job_id)
    # Should retry until dead (rate_limit is retryable)
    assert j["status"] == "dead"
    assert j["error_type"] == "rate_limit"
    assert j["attempts"] == max_retries


def test_retry_attempts_have_separate_logs(herder_home, tmp_path):
    """Each retry attempt has its own log file."""
    cfg = load_config(_cfg(tmp_path, "echo boom >&2; exit 1"))
    store = Store.open()
    r = enqueue_job(cfg, store, EnqueueRequest(project="p", role="r", kind="automation", prompt="x"))
    job_id = r.job_id
    job = store.get_job(job_id)
    max_retries = job["max_retries"]

    # Run worker once to exhaust all retries
    run_pending_once(cfg, store, "w1", 3600)

    # Verify each attempt has a separate log file
    j = store.get_job(job_id)
    rd = Path(j["run_dir"])
    stderr_logs = sorted(rd.glob("stderr.*.log"))
    assert len(stderr_logs) == max_retries, f"Expected {max_retries} stderr logs, got {len(stderr_logs)}"

    # Verify each attempt row points to a distinct file
    paths = [
        row["stderr_path"]
        for row in store.conn.execute(
            "SELECT stderr_path FROM attempts WHERE job_id=? ORDER BY attempt_no", (job_id,)
        ).fetchall()
    ]
    assert len(set(paths)) == max_retries, f"Expected {max_retries} distinct paths, got {len(set(paths))}"
