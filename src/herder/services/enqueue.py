"""Enqueue service — validates and persists job requests."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from herder.config import Config, ConfigError
from herder.db.store import Store
from herder.errors import BudgetError
from herder.ids import new_job_id
from herder.registry import resolve
from herder.runspace import create_run_dir, snapshot_prompt


@dataclass
class EnqueueRequest:
    """Request to enqueue a job.

    Attributes:
        project: Project name.
        role: Role name.
        kind: Job kind (e.g. 'research', 'planner').
        prompt: Prompt text (full content, not file path). Mutually exclusive
            with ``prompt_file``; one must be provided.
        prompt_file: Path to a file whose contents become the prompt. If both
            ``prompt`` and ``prompt_file`` are supplied, ``prompt`` takes precedence.
        priority: Job priority (default 0).
        dry_run: If True, don't persist to database.
        idempotency_key: Optional idempotency key for deduplication.
        runtime: Name of a runtime declared in ``Config.runtimes``, or None to
            resolve via the layered fallback (provider → project → "local").
    """

    project: str
    role: str
    kind: str
    prompt: str = ""
    prompt_file: str | None = None
    priority: int = 0
    dry_run: bool = False
    idempotency_key: str | None = None
    runtime: str | None = None


@dataclass
class EnqueueResult:
    """Result from enqueuing a job.

    Attributes:
        dry_run: Whether this was a dry-run.
        provider: Provider name.
        argv: Resolved argv for the provider.
        cwd: Working directory (project root).
        workspace_mode: Workspace mode (readonly/worktree/inplace).
        permissions: JSON permissions string.
        timeout: Timeout in seconds.
        job_id: Enqueued job ID (None if dry-run).
        status: Job status (None if dry-run).
    """

    dry_run: bool
    provider: str
    argv: list[str]
    cwd: str
    workspace_mode: str
    permissions: str
    timeout: int
    job_id: str | None = None
    status: str | None = None


def enqueue_job(cfg: Config, store: Store, req: EnqueueRequest) -> EnqueueResult:
    """Enqueue a job (or dry-run and return info without persisting).

    Validates role/project, resolves provider config, creates run directory,
    and optionally persists to database.

    Dedup check, budget cap check, run-dir creation, and database INSERT
    are performed atomically within a single write transaction (nested
    under an outer transaction if the scheduler is calling).

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence.
        req: EnqueueRequest with project/role/kind/prompt.

    Returns:
        EnqueueResult with resolved config and job ID (if not dry-run).

    Raises:
        ConfigError: If role/project invalid, project root missing, etc.
        BudgetError: If budget caps are exceeded.
    """
    # Resolve prompt text: prefer explicit prompt, fall back to reading prompt_file.
    prompt_text = req.prompt
    if not prompt_text and req.prompt_file is not None:
        prompt_text = Path(req.prompt_file).read_text()

    # Fail-fast: neither prompt nor prompt_file produced usable text.
    if not prompt_text:
        raise ConfigError("enqueue requires non-empty prompt or prompt_file")

    # Validate runtime reference at the boundary before touching the DB.
    if (
        req.runtime is not None
        and req.runtime != "local"
        and req.runtime not in cfg.runtimes
    ):
        raise ConfigError(f"unknown runtime '{req.runtime}'")

    # Resolve role + project → provider config + permissions.
    # Pass store so that cooldown-aware routing is applied at enqueue time.
    r = resolve(cfg, role=req.role, project=req.project, store=store)
    prov = cfg.providers[r["provider"]]

    # Build base result (shared for dry-run and real enqueue)
    base = EnqueueResult(
        req.dry_run,
        r["provider"],
        [prov.executable, *prov.args],
        r["cwd"],
        r["workspace_mode"],
        r["permissions"],
        prov.timeout,
    )

    # Dry-run: return without persisting
    if req.dry_run:
        return base

    # Validate project root exists
    if not Path(r["cwd"]).is_dir():
        raise ConfigError(f"project root does not exist: {r['cwd']}")

    # Compute prompt hash for dedup and budget checks
    phash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    # Track whether we own the transaction (for nesting safety)
    own_txn = not store.conn.in_transaction
    rd = None

    if own_txn:
        store.conn.execute("BEGIN IMMEDIATE")

    try:
        # 1) Active dedup — collapse an identical still-running submission
        if cfg.budget.dedup_active and not req.idempotency_key:
            dup = store.find_active_duplicate(req.role, req.project, req.kind, phash)
            if dup is not None:
                if own_txn:
                    store.conn.execute("COMMIT")
                base.job_id, base.status = dup["id"], dup["status"]
                return base

        # 2) Budget caps — refuse runaway enqueue
        if store.count_active_jobs() >= cfg.budget.max_active_jobs:
            raise BudgetError(
                f"refused: {cfg.budget.max_active_jobs}+ active jobs already queued")
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if store.count_jobs_since(since) >= cfg.budget.max_jobs_per_day:
            raise BudgetError(
                f"refused: daily cap of {cfg.budget.max_jobs_per_day} jobs reached")

        # 3) Create run directory and snapshot prompt (inside transaction for atomicity)
        job_id = new_job_id()
        rd = create_run_dir(job_id)
        ppath, _ = snapshot_prompt(rd, prompt_text)

        # Determine status based on permissions (requires confirmation if inplace_write)
        perms = json.loads(r["permissions"])
        status = "waiting_approval" if perms.get("require_confirm") else "pending"

        # Persist to database (inside the same transaction)
        store.enqueue(
            id=job_id,
            kind=req.kind,
            role=req.role,
            provider=r["provider"],
            project=req.project,
            cwd=r["cwd"],
            workspace_mode=r["workspace_mode"],
            permissions=r["permissions"],
            status=status,
            priority=req.priority,
            prompt_path=str(ppath),
            prompt_hash=phash,
            run_dir=str(rd),
            idempotency_key=req.idempotency_key,
            runtime=req.runtime,
        )

        if own_txn:
            store.conn.execute("COMMIT")

        base.job_id = job_id
        base.status = status
        return base

    except BudgetError:
        if own_txn:
            store.conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError:
        if own_txn:
            store.conn.execute("ROLLBACK")
        # Duplicate idempotency_key: drop the fresh run_dir and return existing job
        if rd is not None:
            shutil.rmtree(rd, ignore_errors=True)
        existing = store.get_job_by_idempotency_key(req.idempotency_key) if req.idempotency_key else None
        if existing is None:
            raise
        base.job_id = existing["id"]
        base.status = existing["status"]
        return base
    except Exception:
        if own_txn:
            store.conn.execute("ROLLBACK")
        raise
