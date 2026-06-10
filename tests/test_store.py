"""Tests for the Store class and database operations."""
import os
import stat

import pytest

from herder import paths
from herder.db.store import Store
from herder.doctor import ProviderHealth


EXPECTED = {"jobs", "attempts", "schedules", "schedule_runs", "provider_health", "workers"}


def test_six_tables(herder_home):
    """Database has all six required tables after opening."""
    store = Store.open()
    names = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert EXPECTED.issubset(names)


def test_wal(herder_home):
    """Database uses WAL journaling mode."""
    store = Store.open()
    mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_set(herder_home):
    """Database has busy_timeout set to 5000ms."""
    store = Store.open()
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_upsert_provider_health(herder_home):
    """Provider health can be upserted and retrieved."""
    store = Store.open()
    health = ProviderHealth(
        provider="claude",
        noninteractive_status="ok",
        auth_status="ok",
        latency_ms=100,
        error_sample=None,
        last_probe_at="2026-06-09T00:00:00+00:00",
    )
    store.upsert_provider_health(health)
    results = store.list_provider_health()
    assert len(results) == 1
    assert results[0]["provider"] == "claude"
    assert results[0]["auth_status"] == "ok"


def test_upsert_provider_health_updates(herder_home):
    """Provider health upsert updates existing row."""
    store = Store.open()
    health1 = ProviderHealth(
        provider="claude",
        noninteractive_status="ok",
        auth_status="ok",
        latency_ms=100,
        error_sample=None,
        last_probe_at="2026-06-09T00:00:00+00:00",
    )
    store.upsert_provider_health(health1)

    health2 = ProviderHealth(
        provider="claude",
        noninteractive_status="fail",
        auth_status="missing",
        latency_ms=200,
        error_sample="auth failed",
        last_probe_at="2026-06-09T01:00:00+00:00",
    )
    store.upsert_provider_health(health2)

    results = store.list_provider_health()
    assert len(results) == 1
    assert results[0]["auth_status"] == "missing"
    assert results[0]["latency_ms"] == 200


def test_db_and_home_are_owner_only(herder_home):
    """FIX 4: Database and home directory are owner-only (0o700, 0o600).

    Verifies that no group or world bits are set on sensitive directories/files.
    """
    Store.open()

    # Check home directory permissions (0o700 = owner rwx, group/other none)
    home_mode = stat.S_IMODE(os.stat(paths.home()).st_mode)
    assert home_mode & 0o077 == 0, f"home dir has group/world bits: {oct(home_mode)}"

    # Check database file permissions (0o600 = owner rw, group/other none)
    db_mode = stat.S_IMODE(os.stat(paths.db_path()).st_mode)
    assert db_mode & 0o077 == 0, f"db file has group/world bits: {oct(db_mode)}"
