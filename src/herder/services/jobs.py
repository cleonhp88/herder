"""Jobs service — list and inspect enqueued jobs."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from herder.db.store import Store


def _latest_log(rd: Path, stream: str) -> Path | None:
    """Find the latest attempt log file, or fall back to legacy name.

    Args:
        rd: Run directory path.
        stream: Stream name ('stdout' or 'stderr').

    Returns:
        Path to the latest log file, or None if no file exists.
    """
    # Look for attempt-numbered logs (stdout.1.log, stdout.2.log, etc.)
    candidates = sorted(
        rd.glob(f"{stream}.*.log"),
        key=lambda p: (
            int(p.suffixes[0].lstrip("."))
            if p.suffixes and p.suffixes[0].lstrip(".").isdigit()
            else -1
        ),
    )
    if candidates:
        return candidates[-1]

    # Fall back to legacy stdout.log / stderr.log
    legacy = rd / f"{stream}.log"
    return legacy if legacy.exists() else None


def list_jobs(
    store: Store, status: str | None = None, kind: str | None = None
) -> list[sqlite3.Row]:
    """List jobs with optional filters.

    Args:
        store: SQLite store.
        status: Filter by status (e.g. 'pending', 'waiting_approval').
        kind: Filter by kind (e.g. 'research', 'planner').

    Returns:
        List of sqlite3.Row objects with job data.
    """
    return store.list_jobs(status=status, kind=kind)


def get_job(store: Store, job_id: str) -> sqlite3.Row | None:
    """Retrieve a job by ID.

    Args:
        store: SQLite store.
        job_id: Job ID to retrieve.

    Returns:
        sqlite3.Row with job data, or None if not found.
    """
    return store.get_job(job_id)


def read_result(store: Store, job_id: str) -> str | None:
    """Return result.md text for a job, or None if job/result missing.

    Args:
        store: SQLite store.
        job_id: Job ID to retrieve result for.

    Returns:
        Contents of result.md as string, or None if not found.
    """
    job = store.get_job(job_id)
    if not job or not job["output_path"]:
        return None
    p = Path(job["output_path"])
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def read_logs(store: Store, job_id: str, max_lines: int = 200) -> dict[str, str] | None:
    """Return {'stdout': ..., 'stderr': ...} (last max_lines each), or None if job missing.

    Args:
        store: SQLite store.
        job_id: Job ID to retrieve logs for.
        max_lines: Maximum number of lines to return per log (default 200).

    Returns:
        Dictionary with 'stdout' and 'stderr' keys containing log text,
        or None if job not found.
    """
    job = store.get_job(job_id)
    if not job or not job["run_dir"]:
        return None
    logs: dict[str, str] = {}
    rd = Path(job["run_dir"])
    for name in ("stdout", "stderr"):
        p = _latest_log(rd, name)
        if p and p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            logs[name] = "\n".join(lines[-max_lines:])
        else:
            logs[name] = ""
    return logs
