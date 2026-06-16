"""Integration tests for worktree job isolation."""
import subprocess
from pathlib import Path


from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _git_repo(tmp_path):
    """Helper: create a minimal git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    g("init", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "f.txt").write_text("original\n")
    g("add", "-A")
    g("commit", "-m", "init")
    return repo


def test_worktree_job_isolates_changes_and_emits_diff(herder_home, tmp_path):
    """Worktree jobs isolate changes and emit diff artifacts."""
    repo = _git_repo(tmp_path)
    c = tmp_path / "c.yaml"
    # 'agent' = sh script that modifies a file in its cwd (simulates a coding agent)
    c.write_text(
        "providers:\n"
        "  scribbler: {type: cli, executable: sh, args: ['-c', 'echo hacked >> f.txt'], input: stdin, timeout: 10}\n"
        "roles:\n  coder: {provider: scribbler, permissions: worktree_write}\n"
        f"projects:\n  p: {{root: '{repo}', default_workspace_mode: worktree, allowed_roles: [coder]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    cfg = load_config(str(c))
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="coder", kind="coding", prompt="x"),
    )
    run_pending_once(cfg, store, "w1", 3600)

    j = store.get_job(r.job_id)
    assert j["status"] == "done"
    # real repo NOT modified
    assert (repo / "f.txt").read_text() == "original\n"
    # diff artifact captured
    diff_file = Path(j["run_dir"]) / "artifacts" / "changes.diff"
    assert diff_file.exists()
    assert "hacked" in diff_file.read_text()


def test_readonly_job_runs_in_repo_root(herder_home, tmp_path):
    """Read-only jobs run in the real repo root (no worktree)."""
    repo = _git_repo(tmp_path)
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n  pwd_echo: {type: cli, executable: pwd, args: [], input: stdin, timeout: 10}\n"
        "roles:\n  reader: {provider: pwd_echo, permissions: read_only}\n"
        f"projects:\n  p: {{root: '{repo}', default_workspace_mode: readonly, allowed_roles: [reader]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    cfg = load_config(str(c))
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="reader", kind="research", prompt="x"),
    )
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(r.job_id)
    assert j["status"] == "done"
    result = (Path(j["run_dir"]) / "result.md").read_text()
    # pwd printed the real repo root (no worktree)
    assert repo.name in result


def test_worktree_job_survives_retry(herder_home, tmp_path):
    """Worktree jobs can be retried in the same worktree (idempotency gate)."""
    repo = _git_repo(tmp_path)
    c = tmp_path / "c.yaml"
    # first run: marker absent → create marker, exit 1 (generic retryable failure)
    # second run: marker present → succeed
    script = "if [ -f .tried ]; then echo recovered; else touch .tried; echo boom >&2; exit 1; fi"
    c.write_text(
        "providers:\n"
        f"  flaky: {{type: cli, executable: sh, args: ['-c', '{script}'], input: stdin, timeout: 10}}\n"
        "roles:\n  coder: {provider: flaky, permissions: worktree_write}\n"
        f"projects:\n  p: {{root: '{repo}', default_workspace_mode: worktree, allowed_roles: [coder]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    cfg = load_config(str(c))
    store = Store.open()
    r = enqueue_job(
        cfg, store, EnqueueRequest(project="p", role="coder", kind="coding", prompt="x")
    )
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(r.job_id)
    assert j["status"] == "done"  # retried in the SAME worktree and succeeded
    assert j["attempts"] == 2
    assert j["error_type"] is None or j["error_type"] != "internal"
