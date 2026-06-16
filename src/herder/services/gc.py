"""Garbage collection service for run directories.

Removes run directories for terminal jobs older than the configured retention policy.
Safe: terminal-only, age-gated, and only paths inside paths.runs_dir().
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from herder import paths
from herder.config import Config
from herder.db.store import Store

# Terminal statuses mapped to retention bucket
_DONE_LIKE = {"done", "cancelled", "rejected"}
_FAIL_LIKE = {"failed", "dead"}


@dataclass
class GcReport:
    """Result of a garbage collection run."""

    deleted: list[str] = field(default_factory=list)  # job ids whose run_dir was removed
    freed_bytes: int = 0  # total bytes freed
    skipped_nonterminal: int = 0  # jobs in non-terminal state
    skipped_too_recent: int = 0  # jobs within retention window
    dry_run: bool = False  # whether this was a dry run


def _dir_size(p: Path) -> int:
    """Calculate total size of a directory tree.

    Args:
        p: Path to the directory.

    Returns:
        Total size in bytes.
    """
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _is_inside(child: Path, parent: Path) -> bool:
    """Check if child is a descendant of parent.

    Args:
        child: Potential child path.
        parent: Potential parent path.

    Returns:
        True if child is strictly inside parent.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def run_gc(
    store: Store, cfg: Config, now: datetime, *, dry_run: bool = False
) -> GcReport:
    """Remove run dirs of terminal jobs older than retention.

    Safe: only terminal jobs, age-gated by retention policy, and only paths
    strictly inside paths.runs_dir(). Never deletes non-terminal jobs or
    anything outside the runs directory.

    Args:
        store: Database store with job list.
        cfg: Config with retention policy.
        now: Current time (injected for testability).
        dry_run: If True, report deletions without actually deleting.

    Returns:
        GcReport with counts of deleted/skipped jobs and bytes freed.
    """
    rep = GcReport(dry_run=dry_run)
    runs_root = paths.runs_dir()
    keep_done = timedelta(days=cfg.retention.keep_done_days)
    keep_fail = timedelta(days=cfg.retention.keep_failed_days)

    for job in store.list_jobs():
        status = job["status"]

        # Determine retention bucket
        if status in _DONE_LIKE:
            cutoff = keep_done
        elif status in _FAIL_LIKE:
            cutoff = keep_fail
        else:
            # Non-terminal (pending/running/waiting/approved/cancelling) → never GC
            rep.skipped_nonterminal += 1
            continue

        # Check if job has finished
        fin = job["finished_at"]
        if not fin:
            rep.skipped_nonterminal += 1
            continue

        # Check age
        age = now - datetime.fromisoformat(fin)
        if age < cutoff:
            rep.skipped_too_recent += 1
            continue

        # Safety: verify path is inside runs_dir
        rd = Path(job["run_dir"])
        if not rd.exists() or not _is_inside(rd, runs_root):
            continue

        # Calculate freed space
        rep.freed_bytes += _dir_size(rd)
        rep.deleted.append(job["id"])

        # Delete (or report in dry-run)
        if not dry_run:
            shutil.rmtree(rd, ignore_errors=True)

    return rep
