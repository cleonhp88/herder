"""End-to-end test for sandbox enforcement of untrusted jobs.

Verifies that an untrusted job (network: false) cannot write outside its cwd
due to seatbelt confinement.
"""
from pathlib import Path

import pytest

from herder import env as env_mod
from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.providers.sandbox import is_supported
from herder.services.enqueue import EnqueueRequest, enqueue_job

pytestmark = pytest.mark.skipif(
    not is_supported(), reason="sandbox-exec only on macOS"
)


def test_untrusted_job_cannot_write_outside_cwd(
    herder_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Verify an untrusted job running under sandbox cannot escape its cwd.

    Enqueues a job with role "untrusted" (permissions: network: false),
    attempts to write outside its cwd, and verifies the seatbelt blocked it.
    """
    monkeypatch.setattr(env_mod, "_login_shell_env", lambda: {"PATH": "/usr/bin:/bin"})

    # Set up directories
    proj = tmp_path / "proj"
    proj.mkdir()

    # Escape target: outside the project cwd
    escape_target = tmp_path / "escape_attempt.txt"

    # Create config with untrusted role that tries to write outside cwd
    c = tmp_path / "config.yaml"
    c.write_text(
        f"""providers:
  evil:
    type: cli
    executable: sh
    args: ["-c", "echo ESCAPED > {escape_target}"]
    input: stdin
    timeout: 10

roles:
  untrusted:
    provider: evil
    permissions: untrusted

projects:
  p:
    root: {proj}
    default_workspace_mode: readonly
    allowed_roles: [untrusted]

worker:
  global_concurrency: 1
  lease_seconds: 3600
"""
    )

    cfg = load_config(str(c))
    store = Store.open()

    # Enqueue the job
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p",
            role="untrusted",
            kind="research",
            prompt="try to escape",
        ),
    )
    job_id = r.job_id

    # Run the pending job once
    run_pending_once(cfg, store, "w1", 3600)

    # Verify the escape write was blocked by seatbelt (PRIMARY ASSERTION)
    assert not escape_target.exists(), (
        f"untrusted job should not have written to {escape_target} "
        "(seatbelt should have blocked it)"
    )

    # Verify the job failed as expected (sandboxed write attempt failed)
    final_job = store.get_job(job_id)
    assert final_job is not None
    # Job should not be "done" since the write failed
    assert final_job["status"] != "done", (
        f"job should fail (write blocked by sandbox); got status {final_job['status']}"
    )


def test_trusted_job_still_runs_unwrapped(
    herder_home: Path, tmp_path: Path, monkeypatch
) -> None:
    """Verify a trusted job (network: true) runs without sandbox wrapping.

    This test ensures that changing supervisor to add sandbox logic doesn't
    break trusted jobs.
    """
    monkeypatch.setattr(env_mod, "_login_shell_env", lambda: {"PATH": "/usr/bin:/bin"})

    proj = tmp_path / "proj"
    proj.mkdir()

    # Create config with trusted role
    c = tmp_path / "config.yaml"
    c.write_text(
        f"""providers:
  simple:
    type: cli
    executable: sh
    args: ["-c", "echo TRUSTED_OUTPUT"]
    input: stdin
    timeout: 10

roles:
  trusted:
    provider: simple
    permissions: worktree_write

projects:
  p:
    root: {proj}
    default_workspace_mode: readonly
    allowed_roles: [trusted]

worker:
  global_concurrency: 1
  lease_seconds: 3600
"""
    )

    cfg = load_config(str(c))
    store = Store.open()

    # Enqueue the job
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p",
            role="trusted",
            kind="research",
            prompt="trusted prompt",
        ),
    )
    job_id = r.job_id

    # Run the pending job once
    run_pending_once(cfg, store, "w1", 3600)

    # Verify the job succeeded (no sandbox blocking)
    final_job = store.get_job(job_id)
    assert final_job is not None
    assert final_job["status"] == "done", (
        f"trusted job should succeed without sandbox; got {final_job['status']}"
    )
