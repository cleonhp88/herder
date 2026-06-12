"""Tests for database migrations and schema versioning."""
import pytest
from herder.db.store import Store, StoreError


def test_user_version_set(herder_home):
    """User version pragma is set to 3 after opening database."""
    store = Store.open()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_open_is_idempotent(herder_home):
    """Opening database multiple times is safe and idempotent."""
    Store.open()
    Store.open()
    assert Store.open().conn.execute("PRAGMA user_version").fetchone()[0] == 3


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
        row[1]
        for row in store.conn.execute("PRAGMA table_info(attempts)").fetchall()
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
    """Opening the database twice (v2→v3 migration) is idempotent."""
    store1 = Store.open()
    version1 = store1.conn.execute("PRAGMA user_version").fetchone()[0]
    store2 = Store.open()
    version2 = store2.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version1 == version2 == 3
