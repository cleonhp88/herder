"""Tests for database migrations and schema versioning."""
import pytest
from herder.db.store import Store, StoreError


def test_user_version_set(herder_home):
    """User version pragma is set to 2 after opening database."""
    store = Store.open()
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_open_is_idempotent(herder_home):
    """Opening database multiple times is safe and idempotent."""
    Store.open()
    Store.open()
    assert Store.open().conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_newer_db_rejected(herder_home):
    """Opening a database with newer schema version raises StoreError."""
    store = Store.open()
    store.conn.execute("PRAGMA user_version = 999")
    with pytest.raises(StoreError):
        Store.open()
