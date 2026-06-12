"""Job supervisor — executes a claimed job end-to-end."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from herder.config import Config
from herder.db.store import Store
from herder.env import build_env
from herder.errors import classify_error
from herder.permissions import Permissions, effective_allow_env
from herder.providers.run import execute
from herder.redact import redact
from herder.runspace import write_result_md, make_worktree, capture_worktree_diff

logger = logging.getLogger(__name__)

# Heartbeat interval in seconds (call renew_lease every N seconds during job execution)
HEARTBEAT_INTERVAL = 30.0


def execute_job(cfg: Config, store: Store, job, worker_id: str) -> str:
    """Run one already-claimed job row end-to-end. Returns final status ('done'/'failed'/'cancelled').

    Builds a DB-backed cancel check that polls the job status for 'cancelling'.
    If the job is marked cancelling, the subprocess runner will terminate the process
    with SIGTERM→grace→SIGKILL.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence.
        job: A claimed job row from the database.
        worker_id: ID of the worker running this job.

    Returns:
        Final status string ('done', 'failed', or 'cancelled').
    """
    provider = cfg.providers[job["provider"]]
    prompt = Path(job["prompt_path"]).read_text(encoding="utf-8")
    run_dir = Path(job["run_dir"])
    attempt_no = job["attempts"]

    # Resolve working directory by workspace mode
    cwd = Path(job["cwd"])
    worktree: Path | None = None
    if job["workspace_mode"] == "worktree":
        worktree = make_worktree(cwd, job["id"])
        cwd = worktree

    # Derive attempt-scoped log file paths
    stdout_path = run_dir / f"stdout.{attempt_no}.log"
    stderr_path = run_dir / f"stderr.{attempt_no}.log"

    # Build a cancel check that polls the DB
    def _should_cancel() -> bool:
        row = store.get_job(job["id"])
        return bool(row) and row["status"] == "cancelling"

    # Build a heartbeat renewal closure
    lease_seconds = cfg.worker.lease_seconds
    def _renew() -> None:
        store.renew_lease(job["id"], worker_id, lease_seconds)

    # Parse job permissions and resolve allowlisted env vars
    perms = Permissions.from_json(job["permissions"])
    prof = cfg.env_profiles.get(provider.env_profile) if provider.env_profile else None
    provider_allow = prof.allow_env if prof else []
    # Enforce secret_access: a job gets secrets ONLY if its permissions grant it
    allow = effective_allow_env(perms, provider_allow)

    # Log any confinement modes (network/filesystem enforcement is in 7.3 via sandbox)
    if perms.network is not True or perms.filesystem != "inplace_write":
        logger.debug(
            "job %s perms network=%s fs=%s shell_tools=%s (sandbox enforces in 7.3)",
            job["id"],
            perms.network,
            perms.filesystem,
            perms.shell_tools,
        )

    # Decide if sandbox confinement is needed (7.3: untrusted jobs)
    sandbox_profile = None
    if perms.network is False:  # untrusted-style job → confine
        from herder.providers.sandbox import is_supported, build_profile
        if not is_supported():
            raise RuntimeError(
                f"job {job['id']} requires sandbox (network denied) but "
                "sandbox-exec is unavailable on this platform"
            )
        sandbox_profile = build_profile(allow_write=[cwd], deny_network=True)

    res = execute(
        provider,
        prompt,
        cwd=cwd,
        run_dir=run_dir,
        env=build_env(allow),
        timeout=provider.timeout,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        cancel_check=_should_cancel,
        heartbeat=_renew,
        heartbeat_interval=HEARTBEAT_INTERVAL,
        sandbox_profile=sandbox_profile,
    )

    # Compute duration from result timestamps
    duration_ms = None
    if res.started_at and res.finished_at:
        duration_ms = int((res.finished_at - res.started_at).total_seconds() * 1000)

    store.record_attempt(
        job_id=job["id"],
        attempt_no=attempt_no,
        worker_id=worker_id,
        exit_code=res.exit_code,
        status=res.status,
        error_type=res.error_type,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        usage=res.usage,
        duration_ms=duration_ms,
        started_at=res.started_at.isoformat() if res.started_at else None,
        finished_at=res.finished_at.isoformat() if res.finished_at else None,
        provider=job["provider"],
    )

    # Capture worktree diff if applicable
    if worktree is not None:
        try:
            capture_worktree_diff(worktree, run_dir / "artifacts" / "changes.diff")
        except Exception:  # noqa: BLE001
            logger.warning("diff capture failed for %s", job["id"])

    # Handle result based on outcome
    if res.status == "done":
        frontmatter = {
            "job_id": job["id"],
            "kind": job["kind"],
            "role": job["role"] or "",
            "provider": job["provider"],
            "project": job["project"] or "",
            "status": "done",
            "attempts": job["attempts"],
        }
        out = write_result_md(run_dir, frontmatter, redact(res.output))
        store.finish_job(job["id"], "done", output_path=str(out))
        return "done"

    if res.status == "cancelled":
        store.finish_job(job["id"], "cancelled")
        return "cancelled"

    # failed, timeout, or other error outcomes
    error_type = res.error_type
    if error_type in (None, "unknown"):
        error_type = classify_error(res.stderr or "", res.exit_code)
    store.finish_job(job["id"], "failed", error_type=error_type)
    return "failed"
