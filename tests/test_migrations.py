"""Tests for database migrations and schema versioning."""

import pytest
from herder.db.store import Store, StoreError


def test_user_version_set(herder_home):
    """User version pragma is set to 6 after opening database."""
    store = Store.open()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_open_is_idempotent(herder_home):
    """Opening database multiple times is safe and idempotent."""
    Store.open()
    Store.open()
    assert Store.open().conn.execute("PRAGMA user_version").fetchone()[0] == 6


def test_newer_db_rejected(herder_home):
    """Opening a database with newer schema version raises StoreError."""
    store = Store.open()
    store.conn.execute("PRAGMA user_version = 999")
    with pytest.raises(StoreError):
        Store.open()


def test_v3_attempts_provider_column_exists(herder_home):
    """Schema v3 adds provider column to attempts table."""
    store = Store.open()
    cols = {
        row[1] for row in store.conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    assert "provider" in cols


def test_v3_provider_index_exists(herder_home):
    """Schema v3 creates idx_attempts_provider_finished index."""
    store = Store.open()
    idx_names = {
        row[1]
        for row in store.conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_attempts_provider_finished" in idx_names


def test_v2_to_v3_migration_idempotent(herder_home):
    """Opening the database twice is idempotent — ends at current version 6."""
    store1 = Store.open()
    version1 = store1.conn.execute("PRAGMA user_version").fetchone()[0]
    store2 = Store.open()
    version2 = store2.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version1 == version2 == 6


def test_migration_v6_adds_runtime_column(tmp_path):
    """Migration v6 adds the runtime column to the jobs table."""
    import sqlite3
    from herder.db.migrations import migrate, CURRENT_SCHEMA_VERSION
    conn = sqlite3.connect(tmp_path / "t.db")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "runtime" in cols


def test_migration_v5_to_v6_upgrade(tmp_path):
    """Migration correctly upgrades a real v5 DB to v6 by adding jobs.runtime column."""
    import sqlite3
    from herder.db.migrations import (
        migrate,
        SCHEMA_V1,
        SCHEMA_V2_UPGRADE,
        SCHEMA_V3_UPGRADE,
        SCHEMA_V4_UPGRADE,
        SCHEMA_V5_UPGRADE,
    )

    conn = sqlite3.connect(tmp_path / "v5.db")
    # Build an authentic v5 schema: apply each upgrade in order up to v5.
    conn.executescript(SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.executescript(SCHEMA_V2_UPGRADE)
    conn.execute("PRAGMA user_version = 2")
    conn.executescript(SCHEMA_V3_UPGRADE)
    conn.execute("PRAGMA user_version = 3")
    conn.executescript(SCHEMA_V4_UPGRADE)
    conn.execute("PRAGMA user_version = 4")
    conn.executescript(SCHEMA_V5_UPGRADE)
    conn.execute("PRAGMA user_version = 5")

    # Confirm v5 schema does NOT yet have the runtime column.
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "runtime" not in cols_before

    # Now run the migration — it must upgrade v5 → v6.
    migrate(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "runtime" in cols_after
