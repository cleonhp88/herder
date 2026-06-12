"""Tests for token usage and duration persistence on attempts (schema v2+)."""
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from herder.db.store import Store
from herder.db.migrations import SCHEMA_V1, SCHEMA_V2_UPGRADE, CURRENT_SCHEMA_VERSION


def test_fresh_db_is_current_version(herder_home):
    """A fresh database should be created at the current schema version."""
    store = Store.open()
    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION


def test_attempts_has_duration_ms_and_usage(herder_home):
    """The attempts table must have both duration_ms and usage columns."""
    store = Store.open()
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(attempts)")}
    assert "duration_ms" in cols, "attempts table missing duration_ms column"
    assert "usage" in cols, "attempts table missing usage column"


def test_v1_db_upgrades_to_current(herder_home):
    """An existing v1 database should upgrade cleanly to current schema without data loss."""
    # 1. Build a v1 database manually
    db_path = Path(herder_home) / "herder.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    # 2. Insert some test data into v1
    conn.execute(
        """INSERT INTO jobs
           (id, kind, role, provider, cwd, workspace_mode, permissions, status, prompt_path, prompt_hash, run_dir, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("job_v1", "test", "r1", "echo", "/tmp", "readonly", "{}", "done",
         "/tmp/p.md", "hash", "/tmp/run", datetime.now(timezone.utc).isoformat()),
    )
    conn.execute(
        """INSERT INTO attempts
           (job_id, attempt_no, worker_id, exit_code, status, usage, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("job_v1", 1, "w1", 0, "done", '{"eval_count": 42}', datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    # 3. Open via Store → should migrate to current schema version
    store = Store.open()
    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION, (
        f"Expected schema v{CURRENT_SCHEMA_VERSION} after migration, got v{version}"
    )

    # 4. Verify old data is still there
    job = store.get_job("job_v1")
    assert job is not None, "Job data lost during migration"
    assert job["id"] == "job_v1"

    # 5. Verify new columns exist
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(attempts)")}
    assert "duration_ms" in cols
    assert "usage" in cols

    # 6. Verify the old usage record is readable
    attempt = store.attempts_for_job("job_v1")[0]
    assert attempt["usage"] is not None
    usage_dict = json.loads(attempt["usage"])
    assert usage_dict["eval_count"] == 42


def test_record_attempt_persists_usage_dict(herder_home):
    """record_attempt should accept a dict and persist it as JSON."""
    store = Store.open()

    # Seed a job
    store.enqueue(
        id="job_usage",
        kind="test",
        role="r",
        provider="echo",
        cwd="/tmp",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/p.md",
        prompt_hash="h",
        run_dir="/tmp/run",
    )

    # Record an attempt with usage dict
    usage_dict = {"eval_count": 42, "prompt_eval_count": 8}
    store.record_attempt(
        job_id="job_usage",
        attempt_no=1,
        worker_id="w1",
        exit_code=0,
        status="done",
        usage=usage_dict,
    )

    # Fetch and verify
    row = store.conn.execute(
        "SELECT usage FROM attempts WHERE job_id=?", ("job_usage",)
    ).fetchone()
    assert row["usage"] is not None
    stored = json.loads(row["usage"])
    assert stored["eval_count"] == 42
    assert stored["prompt_eval_count"] == 8


def test_record_attempt_persists_duration_ms(herder_home):
    """record_attempt should persist duration_ms."""
    store = Store.open()

    # Seed a job
    store.enqueue(
        id="job_duration",
        kind="test",
        role="r",
        provider="echo",
        cwd="/tmp",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/p.md",
        prompt_hash="h",
        run_dir="/tmp/run",
    )

    # Record an attempt with duration
    store.record_attempt(
        job_id="job_duration",
        attempt_no=1,
        worker_id="w1",
        exit_code=0,
        status="done",
        duration_ms=1234,
    )

    # Fetch and verify
    row = store.conn.execute(
        "SELECT duration_ms FROM attempts WHERE job_id=?", ("job_duration",)
    ).fetchone()
    assert row["duration_ms"] == 1234


def test_record_attempt_accepts_none_usage_and_duration(herder_home):
    """record_attempt should gracefully handle None for usage and duration_ms."""
    store = Store.open()

    # Seed a job
    store.enqueue(
        id="job_none",
        kind="test",
        role="r",
        provider="echo",
        cwd="/tmp",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/p.md",
        prompt_hash="h",
        run_dir="/tmp/run",
    )

    # Record with None values
    store.record_attempt(
        job_id="job_none",
        attempt_no=1,
        worker_id="w1",
        exit_code=0,
        status="done",
        usage=None,
        duration_ms=None,
    )

    # Fetch and verify
    row = store.conn.execute(
        "SELECT usage, duration_ms FROM attempts WHERE job_id=?", ("job_none",)
    ).fetchone()
    assert row["usage"] is None
    assert row["duration_ms"] is None


def test_record_attempt_with_both_usage_and_duration(herder_home):
    """record_attempt should persist both usage and duration_ms together."""
    store = Store.open()

    # Seed a job
    store.enqueue(
        id="job_complete",
        kind="test",
        role="r",
        provider="echo",
        cwd="/tmp",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/p.md",
        prompt_hash="h",
        run_dir="/tmp/run",
    )

    # Record with both
    usage = {"eval_count": 100, "prompt_eval_count": 20}
    store.record_attempt(
        job_id="job_complete",
        attempt_no=1,
        worker_id="w1",
        exit_code=0,
        status="done",
        error_type=None,
        stdout_path=None,
        stderr_path=None,
        usage=usage,
        duration_ms=5000,
    )

    # Fetch and verify both
    row = store.conn.execute(
        "SELECT usage, duration_ms FROM attempts WHERE job_id=?", ("job_complete",)
    ).fetchone()
    stored_usage = json.loads(row["usage"])
    assert stored_usage == usage
    assert row["duration_ms"] == 5000


def test_jobs_table_has_total_cost_column(herder_home):
    """The jobs table should have a total_cost column (v2 addition)."""
    store = Store.open()
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(jobs)")}
    assert "total_cost" in cols, "jobs table missing total_cost column"
