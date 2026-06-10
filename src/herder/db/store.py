"""SQLite store for jobs, schedules, and system state.

Opens/creates herder.db with schema migrations and WAL mode.
Provides methods to upsert and query provider health, jobs, and schedules.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from datetime import datetime, timezone, timedelta

from herder import paths
from herder.db.migrations import StoreError, MigrationError, migrate  # noqa: F401
from herder.doctor import ProviderHealth


class Store:
    """SQLite store for Herder state.

    Opens/creates the database, runs migrations, and provides query methods.
    """

    def __init__(self, conn: sqlite3.Connection):
        """Initialize a Store with an open SQLite connection.

        Args:
            conn: SQLite connection with row_factory and pragmas already set.
        """
        self.conn = conn

    @classmethod
    def open(cls) -> Store:
        """Open or create the herder database.

        Creates the HERDER_HOME directory if needed, opens herder.db,
        sets up row factory and pragmas, runs migrations, and returns a Store.

        Returns:
            An initialized Store instance.

        Raises:
            StoreError: If database version is incompatible.
        """
        home = paths.home()
        home.mkdir(parents=True, exist_ok=True)
        # FIX 4: Explicit mode (owner-only) on home directory
        try:
            os.chmod(home, 0o700)
        except OSError:
            pass

        conn = sqlite3.connect(str(paths.db_path()), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)

        # FIX 4: Explicit mode (owner-only) on database file after creation/connection
        db_path = paths.db_path()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

        return cls(conn)

    # ── provider_health ──

    def upsert_provider_health(self, h: ProviderHealth) -> None:
        """Upsert a provider health record.

        Args:
            h: ProviderHealth instance.
        """
        self.conn.execute(
            """INSERT INTO provider_health
               (provider, auth_status, noninteractive_status, latency_ms,
                error_sample, last_probe_at)
               VALUES (:p, :a, :n, :l, :e, :t)
               ON CONFLICT(provider) DO UPDATE SET
                 auth_status=:a,
                 noninteractive_status=:n,
                 latency_ms=:l,
                 error_sample=:e,
                 last_probe_at=:t""",
            {
                "p": h.provider,
                "a": h.auth_status,
                "n": h.noninteractive_status,
                "l": h.latency_ms,
                "e": h.error_sample,
                "t": h.last_probe_at,
            },
        )

    def list_provider_health(self) -> list[sqlite3.Row]:
        """List all provider health records ordered by provider name.

        Returns:
            List of sqlite3.Row objects with provider_health columns.
        """
        return self.conn.execute(
            "SELECT * FROM provider_health ORDER BY provider"
        ).fetchall()

    # ── jobs ──

    _JOB_FIELDS = {
        "id",
        "kind",
        "role",
        "provider",
        "project",
        "cwd",
        "workspace_mode",
        "permissions",
        "status",
        "priority",
        "attempts",
        "max_retries",
        "prompt_path",
        "prompt_hash",
        "source_prompt_file",
        "run_dir",
        "output_path",
        "cost",
        "error_type",
        "worker_id",
        "lease_until",
        "heartbeat_at",
        "idempotency_key",
        "workflow_id",
        "parent_job_id",
        "depends_on",
        "created_at",
        "started_at",
        "finished_at",
    }

    _TERMINAL = ("done", "failed", "dead", "cancelled", "rejected")

    def count_active_jobs(self) -> int:
        """Count non-terminal jobs.

        Returns:
            Count of non-terminal jobs (pending, approved, running, etc.).
        """
        ph = ",".join("?" * len(self._TERMINAL))
        return self.conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status NOT IN ({ph})", self._TERMINAL).fetchone()[0]

    def count_jobs_since(self, iso_ts: str) -> int:
        """Count jobs created since the given ISO timestamp.

        Args:
            iso_ts: ISO 8601 timestamp string.

        Returns:
            Count of jobs with created_at >= iso_ts.
        """
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE created_at >= ?", (iso_ts,)).fetchone()[0]

    def find_active_duplicate(self, role: str | None, project: str | None,
                              kind: str | None, prompt_hash: str) -> sqlite3.Row | None:
        """Find an active (non-terminal) job with matching role, project, kind, and prompt_hash.

        Dedup is keyed on (role, project, kind, prompt_hash). Intentionally ignores
        priority, allowing a higher-priority resubmission of an identical task to dedup
        against a lower-priority pending job.

        Args:
            role: Role name (or None).
            project: Project name (or None).
            kind: Job kind (or None).
            prompt_hash: SHA256 hash of prompt text.

        Returns:
            A sqlite3.Row with the earliest matching active job, or None if no match.
        """
        ph = ",".join("?" * len(self._TERMINAL))
        return self.conn.execute(
            f"""SELECT * FROM jobs
                WHERE prompt_hash=? AND role IS ? AND project IS ? AND kind IS ?
                  AND status NOT IN ({ph})
                ORDER BY created_at ASC LIMIT 1""",
            (prompt_hash, role, project, kind, *self._TERMINAL)).fetchone()

    def enqueue(self, **fields) -> str:
        """Enqueue a job.

        Validates that all provided fields are in the whitelist.
        Automatically sets created_at if not provided.

        Args:
            **fields: Job fields (must be subset of _JOB_FIELDS).

        Returns:
            The job id.

        Raises:
            StoreError: If unknown fields are provided.
        """
        unknown = set(fields) - self._JOB_FIELDS
        if unknown:
            raise StoreError(f"unknown job fields: {sorted(unknown)}")
        fields.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        cols = ",".join(fields.keys())
        placeholders = ",".join(":" + k for k in fields.keys())
        self.conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", fields
        )
        return fields["id"]

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        """Retrieve a job by id.

        Args:
            job_id: The job id.

        Returns:
            A sqlite3.Row with job data, or None if not found.
        """
        return self.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    def get_job_by_idempotency_key(self, key: str) -> sqlite3.Row | None:
        """Retrieve a job by idempotency key.

        Args:
            key: The idempotency key.

        Returns:
            A sqlite3.Row with job data, or None if not found.
        """
        return self.conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key=?", (key,)
        ).fetchone()

    def list_jobs(
        self, status: str | None = None, kind: str | None = None
    ) -> list[sqlite3.Row]:
        """List jobs with optional filters.

        Args:
            status: Filter by status (e.g. 'pending', 'done').
            kind: Filter by kind (e.g. 'research', 'planner').

        Returns:
            List of sqlite3.Row objects, ordered by created_at DESC.
        """
        query = "SELECT * FROM jobs"
        args: list = []
        clauses = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        return self.conn.execute(query, args).fetchall()

    def claim_job(self, worker_id: str, lease_seconds: int) -> sqlite3.Row | None:
        """Atomically claim the highest-priority claimable job.

        Claimable jobs are those with status='pending'/'approved',
        or status='running' with an expired lease (reclaim).

        Sets status='running', updates worker_id, lease_until, heartbeat_at,
        and increments attempts by 1. All in a single UPDATE...RETURNING.

        Args:
            worker_id: ID of the worker claiming the job.
            lease_seconds: Lease duration in seconds from now.

        Returns:
            The claimed job row, or None if no claimable job exists.
        """
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease_iso = (now + timedelta(seconds=lease_seconds)).isoformat()
        return self.conn.execute(
            """UPDATE jobs
               SET status='running', worker_id=?, started_at=COALESCE(started_at, ?),
                   lease_until=?, heartbeat_at=?, attempts=attempts+1
               WHERE id = (
                 SELECT id FROM jobs
                 WHERE status IN ('pending','approved')
                    OR (status='running' AND lease_until IS NOT NULL AND julianday(lease_until) < julianday(?))
                 ORDER BY priority DESC, created_at ASC
                 LIMIT 1
               )
               RETURNING *""",
            (worker_id, now_iso, lease_iso, now_iso, now_iso),
        ).fetchone()

    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        error_type: str | None = None,
        output_path: str | None = None,
    ) -> None:
        """Mark a job as finished (done, failed, or error state).

        Clears worker_id and lease_until, and sets finished_at to now.

        Args:
            job_id: The job id.
            status: Terminal status (e.g. 'done', 'failed', 'error').
            error_type: Optional error type (e.g. 'timeout', 'validation').
            output_path: Optional path to job output/result.
        """
        self.conn.execute(
            """UPDATE jobs
               SET status=?, error_type=?, output_path=?, finished_at=?,
                   worker_id=NULL, lease_until=NULL
               WHERE id=?""",
            (status, error_type, output_path, datetime.now(timezone.utc).isoformat(), job_id),
        )

    def record_attempt(
        self,
        *,
        job_id: str,
        attempt_no: int,
        worker_id: str | None,
        exit_code: int | None,
        status: str,
        error_type: str | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        usage: dict | None = None,
        duration_ms: int | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Record a job attempt in the attempts table.

        Args:
            job_id: The job id.
            attempt_no: Attempt number (1-indexed).
            worker_id: ID of the worker that ran the attempt.
            exit_code: Process exit code (or None if not applicable).
            status: Attempt status (e.g. 'done', 'failed', 'timeout').
            error_type: Optional error type (e.g. 'network', 'validation').
            stdout_path: Optional path to stdout log file.
            stderr_path: Optional path to stderr log file.
            usage: Optional dict with token/duration metrics (will be JSON-encoded).
            duration_ms: Optional wall-clock duration in milliseconds.
            started_at: Optional ISO 8601 timestamp when attempt began.
            finished_at: Optional ISO 8601 timestamp when attempt ended.
        """
        import json as _json

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO attempts
               (job_id, attempt_no, worker_id, exit_code, status, error_type,
                stdout_path, stderr_path, usage, duration_ms, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                attempt_no,
                worker_id,
                exit_code,
                status,
                error_type,
                stdout_path,
                stderr_path,
                _json.dumps(usage) if usage is not None else None,
                duration_ms,
                started_at or now,
                finished_at or now,
            ),
        )

    def request_cancel(self, job_id: str) -> str | None:
        """Request cancellation of a job.

        - pending/waiting_approval/approved → cancelled (terminal, immediate)
        - running → cancelling (worker will receive signal and finalize)
        - done/failed/cancelled → returns status unchanged (noop)
        - unknown job → returns None

        Args:
            job_id: The job id to cancel.

        Returns:
            The resulting status (cancelled, cancelling, done, failed, etc.),
            or None if the job doesn't exist.
        """
        job = self.get_job(job_id)
        if not job:
            return None

        status = job["status"]
        now = datetime.now(timezone.utc).isoformat()

        if status in ("pending", "waiting_approval", "approved"):
            # Job hasn't started: immediately transition to terminal state
            self.conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, worker_id=NULL, lease_until=NULL WHERE id=?",
                (now, job_id),
            )
            return "cancelled"

        if status == "running":
            # Job is running: flag for cancellation, worker will kill subprocess
            self.conn.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,))
            return "cancelling"

        # All other statuses (done, failed, cancelled, etc.): no-op
        return status

    def requeue(self, job_id: str) -> None:
        """Put a terminal/failed job back in the queue (manual retry or auto-retry).

        Keeps the attempts counter (history preserved in attempts table).

        Args:
            job_id: The job id to requeue.
        """
        self.conn.execute(
            "UPDATE jobs SET status='pending', error_type=NULL, finished_at=NULL,"
            " worker_id=NULL, lease_until=NULL WHERE id=?",
            (job_id,),
        )

    def mark_dead(self, job_id: str) -> None:
        """Mark a job as dead (exhausted all retries).

        Args:
            job_id: The job id to mark as dead.
        """
        self.conn.execute("UPDATE jobs SET status='dead' WHERE id=?", (job_id,))

    def approve_job(self, job_id: str) -> str | None:
        """Approve a waiting_approval job (transition to approved, now claimable).

        Atomic: uses UPDATE...RETURNING to guard the transition.

        - waiting_approval → approved
        - other statuses → unchanged (returns current status as no-op)
        - unknown job → returns None

        Args:
            job_id: The job id to approve.

        Returns:
            The resulting status (approved, or unchanged status if not waiting_approval),
            or None if the job doesn't exist.
        """
        row = self.conn.execute(
            "UPDATE jobs SET status='approved' WHERE id=? AND status='waiting_approval' RETURNING status",
            (job_id,),
        ).fetchone()
        if row:
            return "approved"
        cur = self.get_job(job_id)
        return cur["status"] if cur else None

    def reject_job(self, job_id: str) -> str | None:
        """Reject a waiting_approval job (transition to rejected, terminal state).

        Atomic: uses UPDATE...RETURNING to guard the transition.

        - waiting_approval → rejected (terminal, sets finished_at)
        - other statuses → unchanged (returns current status as no-op)
        - unknown job → returns None

        Args:
            job_id: The job id to reject.

        Returns:
            The resulting status (rejected, or unchanged status if not waiting_approval),
            or None if the job doesn't exist.
        """
        row = self.conn.execute(
            "UPDATE jobs SET status='rejected', finished_at=? WHERE id=? AND status='waiting_approval' RETURNING status",
            (datetime.now(timezone.utc).isoformat(), job_id),
        ).fetchone()
        if row:
            return "rejected"
        cur = self.get_job(job_id)
        return cur["status"] if cur else None

    # ── schedules ──

    def upsert_schedule(
        self,
        *,
        id: str,
        cron: str,
        project: str | None,
        role: str | None,
        kind: str | None,
        prompt_file: str | None,
        enabled: bool,
    ) -> None:
        """Upsert a schedule record.

        Args:
            id: Schedule ID.
            cron: Cron expression.
            project: Project name.
            role: Role name.
            kind: Job kind.
            prompt_file: Path to prompt file.
            enabled: Whether schedule is enabled.
        """
        self.conn.execute(
            """INSERT INTO schedules (id, cron, project, role, kind, prompt_file, enabled)
               VALUES (:id,:cron,:project,:role,:kind,:pf,:en)
               ON CONFLICT(id) DO UPDATE SET cron=:cron, project=:project, role=:role,
                 kind=:kind, prompt_file=:pf, enabled=:en""",
            {
                "id": id,
                "cron": cron,
                "project": project,
                "role": role,
                "kind": kind,
                "pf": prompt_file,
                "en": int(enabled),
            },
        )

    def record_schedule_run(
        self,
        schedule_id: str,
        scheduled_for: str,
        status: str,
        enqueued_job_id: str | None = None,
    ) -> bool:
        """Insert a schedule_runs row. Returns False if (schedule_id, scheduled_for)
        already exists (duplicate tick — idempotency via UNIQUE index).

        Args:
            schedule_id: The schedule ID.
            scheduled_for: ISO datetime when the job was scheduled.
            status: Run status (e.g. 'enqueued', 'missed', 'failed').
            enqueued_job_id: Optional job ID that was enqueued.

        Returns:
            True if inserted, False if duplicate (constraint violation).
        """
        try:
            self.conn.execute(
                """INSERT INTO schedule_runs (schedule_id, scheduled_for, enqueued_job_id, status, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    schedule_id,
                    scheduled_for,
                    enqueued_job_id,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def last_scheduled_for(self, schedule_id: str) -> str | None:
        """Get the latest scheduled_for timestamp for a schedule.

        Args:
            schedule_id: The schedule ID.

        Returns:
            ISO datetime string of the last scheduled run, or None if no runs exist.
        """
        row = self.conn.execute(
            "SELECT MAX(scheduled_for) AS m FROM schedule_runs WHERE schedule_id=?",
            (schedule_id,),
        ).fetchone()
        return row["m"] if row else None

    def list_schedule_runs(self, schedule_id: str) -> list[sqlite3.Row]:
        """List all runs for a schedule.

        Args:
            schedule_id: The schedule ID.

        Returns:
            List of schedule_runs rows, ordered by scheduled_for DESC.
        """
        return self.conn.execute(
            "SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY scheduled_for DESC",
            (schedule_id,),
        ).fetchall()

    def set_schedule_run_job_id(self, schedule_id: str, scheduled_for: str, job_id: str) -> None:
        """Set the enqueued_job_id for a schedule run.

        Args:
            schedule_id: The schedule ID.
            scheduled_for: The scheduled_for timestamp.
            job_id: The job ID to set.
        """
        self.conn.execute(
            "UPDATE schedule_runs SET enqueued_job_id=? WHERE schedule_id=? AND scheduled_for=?",
            (job_id, schedule_id, scheduled_for),
        )

    # ── lease renewal & worker heartbeat ──

    def renew_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> None:
        """Extend a running job's lease (heartbeat). Only the owning worker may renew.

        Updates lease_until to now + lease_seconds and heartbeat_at to now.
        Only applies if the job is in running or cancelling status and owned by worker_id.

        Args:
            job_id: The job ID.
            worker_id: The worker ID claiming ownership.
            lease_seconds: Lease duration in seconds from now.
        """
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        heartbeat_at = now.isoformat()
        self.conn.execute(
            """UPDATE jobs SET lease_until=?, heartbeat_at=?
               WHERE id=? AND worker_id=? AND status IN ('running','cancelling')""",
            (lease_until, heartbeat_at, job_id, worker_id),
        )

    def register_worker(self, worker_id: str, version: str = "") -> None:
        """Register or update a worker in the workers table.

        Records the worker's hostname, pid, version, and marks status as running.
        On conflict, updates with the same values (effectively refreshing registration).

        Args:
            worker_id: The worker ID.
            version: Optional version string (e.g., "0.1.0").
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO workers (worker_id, hostname, pid, version, status, started_at, last_heartbeat_at)
               VALUES (?,?,?,?,'running',?,?)
               ON CONFLICT(worker_id) DO UPDATE SET
                 hostname=excluded.hostname, pid=excluded.pid, version=excluded.version,
                 status='running', started_at=excluded.started_at, last_heartbeat_at=excluded.last_heartbeat_at""",
            (worker_id, socket.gethostname(), os.getpid(), version, now, now),
        )

    def mark_worker_stopped(self, worker_id: str) -> None:
        """Mark a worker as stopped and update its last_heartbeat_at timestamp.

        Called when a worker process exits (cleanly or via Ctrl-C).
        Updates status to 'stopped' and records the time.

        Args:
            worker_id: The worker ID.
        """
        self.conn.execute(
            "UPDATE workers SET status='stopped', last_heartbeat_at=? WHERE worker_id=?",
            (datetime.now(timezone.utc).isoformat(), worker_id),
        )

    def worker_heartbeat(self, worker_id: str) -> None:
        """Update the last_heartbeat_at timestamp for a worker.

        Args:
            worker_id: The worker ID.
        """
        self.conn.execute(
            "UPDATE workers SET last_heartbeat_at=? WHERE worker_id=?",
            (datetime.now(timezone.utc).isoformat(), worker_id),
        )

    def attempts_for_job(self, job_id: str) -> list[sqlite3.Row]:
        """List all attempts for a job.

        Args:
            job_id: The job id.

        Returns:
            List of attempt rows, ordered by attempt_no.
        """
        return self.conn.execute(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_no", (job_id,)
        ).fetchall()

    def all_attempts(self) -> list[sqlite3.Row]:
        """List all attempts across all jobs.

        Returns:
            List of attempt rows, ordered by finished_at.
        """
        return self.conn.execute(
            "SELECT * FROM attempts ORDER BY finished_at"
        ).fetchall()
