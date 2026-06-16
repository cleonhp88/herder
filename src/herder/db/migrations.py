"""Database schema migrations and version management.

Tracks schema version using PRAGMA user_version and applies migrations
in sequence. Fail-closed on version mismatch.
"""

from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 6

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  role TEXT,
  provider TEXT,
  project TEXT,
  cwd TEXT NOT NULL,
  workspace_mode TEXT NOT NULL,
  permissions TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 3,
  prompt_path TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  source_prompt_file TEXT,
  run_dir TEXT NOT NULL,
  output_path TEXT,
  cost REAL,
  error_type TEXT,
  worker_id TEXT,
  lease_until TEXT,
  heartbeat_at TEXT,
  idempotency_key TEXT,
  workflow_id TEXT,
  parent_job_id TEXT,
  depends_on TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
  ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_claimable
  ON jobs(status, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  worker_id TEXT,
  exit_code INTEGER,
  status TEXT NOT NULL,
  error_type TEXT,
  stdout_path TEXT,
  stderr_path TEXT,
  usage TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id);

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,
  cron TEXT NOT NULL,
  project TEXT,
  role TEXT,
  kind TEXT,
  prompt_file TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_enqueued_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  enqueued_job_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(schedule_id, scheduled_for)
);

CREATE TABLE IF NOT EXISTS provider_health (
  provider TEXT PRIMARY KEY,
  version TEXT,
  auth_status TEXT,
  noninteractive_status TEXT,
  latency_ms INTEGER,
  error_sample TEXT,
  last_probe_at TEXT
);

CREATE TABLE IF NOT EXISTS workers (
  worker_id TEXT PRIMARY KEY,
  hostname TEXT,
  pid INTEGER,
  version TEXT,
  status TEXT,
  started_at TEXT,
  last_heartbeat_at TEXT
);
"""

SCHEMA_V2_UPGRADE = """
ALTER TABLE attempts ADD COLUMN duration_ms INTEGER;
ALTER TABLE jobs ADD COLUMN total_cost REAL;
"""

SCHEMA_V3_UPGRADE = """
ALTER TABLE attempts ADD COLUMN provider TEXT;
CREATE INDEX IF NOT EXISTS idx_attempts_provider_finished ON attempts(provider, finished_at, status);
"""

SCHEMA_V4_UPGRADE = """
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);
"""

SCHEMA_V5_UPGRADE = "ALTER TABLE jobs ADD COLUMN next_eligible_at TEXT;"

SCHEMA_V6_UPGRADE = "ALTER TABLE jobs ADD COLUMN runtime TEXT;"


class StoreError(Exception):
    """Base exception for store and migration errors."""

    pass


class MigrationError(StoreError):
    """Raised when a migration fails or version mismatch occurs."""

    pass


def migrate(conn: sqlite3.Connection) -> None:
    """Apply migrations to the database.

    Checks PRAGMA user_version and applies migrations up to CURRENT_SCHEMA_VERSION.
    Fails closed if database version is newer than this binary's version.

    Args:
        conn: SQLite connection.

    Raises:
        MigrationError: If database version is newer than CURRENT_SCHEMA_VERSION
                       or if migration fails.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    if version == 0:
        # Fresh database — apply V1 and V2
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
        version = 1

    if version == 1:
        # Upgrade v1 → v2
        conn.executescript(SCHEMA_V2_UPGRADE)
        conn.execute("PRAGMA user_version = 2")
        version = 2

    if version == 2:
        # Upgrade v2 → v3: add provider column to attempts for Tier 2 routing
        conn.executescript(SCHEMA_V3_UPGRADE)
        conn.execute("PRAGMA user_version = 3")
        version = 3

    if version == 3:
        # Upgrade v3 → v4: add job_events audit table for FSM transition history
        conn.executescript(SCHEMA_V4_UPGRADE)
        conn.execute("PRAGMA user_version = 4")
        version = 4

    if version == 4:
        # Upgrade v4 → v5: add next_eligible_at for exponential retry backoff
        conn.executescript(SCHEMA_V5_UPGRADE)
        conn.execute("PRAGMA user_version = 5")
        version = 5

    if version == 5:
        # Upgrade v5 → v6: add runtime column for the Runtime seam
        conn.executescript(SCHEMA_V6_UPGRADE)
        conn.execute("PRAGMA user_version = 6")
        version = 6

    if version > CURRENT_SCHEMA_VERSION:
        # Database is newer than this binary
        raise MigrationError(
            f"Database schema v{version} is newer than this binary (v{CURRENT_SCHEMA_VERSION})"
        )
