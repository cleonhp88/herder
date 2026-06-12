"""Tests for herder.routing.select_provider."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from herder.config import Cooldown
from herder.routing import select_provider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cooldown(allowed_fails: int = 3, window_seconds: int = 300) -> Cooldown:
    return Cooldown(allowed_fails=allowed_fails, window_seconds=window_seconds)


def _store_with_failures(failures: dict[str, int]) -> MagicMock:
    """Return a mock Store whose count_recent_failures returns per-provider counts."""
    store = MagicMock()
    store.count_recent_failures.side_effect = lambda prov, win: failures.get(prov, 0)
    return store


# ---------------------------------------------------------------------------
# Basic routing — no failures
# ---------------------------------------------------------------------------

def test_first_by_default_no_failed_provider():
    """With no failed_provider and no failures, returns providers[0]."""
    providers = ["primary", "secondary"]
    store = _store_with_failures({})
    result = select_provider(providers, None, _cooldown(), store)
    assert result == "primary"


def test_starts_after_failed_provider():
    """After a failure, scanning starts at the slot AFTER failed_provider."""
    providers = ["primary", "secondary", "tertiary"]
    store = _store_with_failures({})
    # primary failed → next candidate is secondary
    result = select_provider(providers, "primary", _cooldown(), store)
    assert result == "secondary"


def test_wraps_around():
    """Wrap-around: if the last provider failed, scanning wraps to providers[0]."""
    providers = ["primary", "secondary"]
    store = _store_with_failures({})
    # secondary failed → wrap around to primary
    result = select_provider(providers, "secondary", _cooldown(), store)
    assert result == "primary"


def test_skips_cooling_candidate():
    """A provider over the failure threshold is skipped in favour of the next."""
    providers = ["hot", "cool"]
    # "hot" has 3 recent failures (= allowed_fails), so it is cooling
    store = _store_with_failures({"hot": 3})
    cd = _cooldown(allowed_fails=3)
    result = select_provider(providers, None, cd, store)
    assert result == "cool"


def test_skips_multiple_cooling_candidates():
    """Multiple cooling providers are all skipped; first non-cooling is chosen."""
    providers = ["a", "b", "c"]
    store = _store_with_failures({"a": 5, "b": 5})
    cd = _cooldown(allowed_fails=3)
    result = select_provider(providers, None, cd, store)
    assert result == "c"


# ---------------------------------------------------------------------------
# All-cooling fallback
# ---------------------------------------------------------------------------

def test_all_cooling_returns_primary_and_warns(caplog):
    """When every provider is cooling, fall back to providers[0] and log WARNING."""
    providers = ["a", "b"]
    store = _store_with_failures({"a": 10, "b": 10})
    cd = _cooldown(allowed_fails=3)
    with caplog.at_level(logging.WARNING, logger="herder.routing"):
        result = select_provider(providers, None, cd, store)
    assert result == "a"
    assert any("cooling" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# failed_provider not in list
# ---------------------------------------------------------------------------

def test_failed_provider_not_in_list_no_crash():
    """failed_provider absent from providers list must not crash; starts from 0."""
    providers = ["alpha", "beta"]
    store = _store_with_failures({})
    # "gamma" is not in the list — should not raise, should return "alpha"
    result = select_provider(providers, "gamma", _cooldown(), store)
    assert result == "alpha"


def test_failed_provider_not_in_list_respects_cooldown():
    """When failed_provider is missing and primary is cooling, skips to secondary."""
    providers = ["alpha", "beta"]
    store = _store_with_failures({"alpha": 5})
    cd = _cooldown(allowed_fails=3)
    result = select_provider(providers, "unknown_provider", cd, store)
    assert result == "beta"


# ---------------------------------------------------------------------------
# Single-provider fast path
# ---------------------------------------------------------------------------

def test_single_provider_fast_path_no_store_query():
    """Single-provider + no failed_provider: returns without querying the store."""
    store = MagicMock()
    store.count_recent_failures.side_effect = AssertionError("store must NOT be queried")
    result = select_provider(["only"], None, _cooldown(), store)
    assert result == "only"
    store.count_recent_failures.assert_not_called()


def test_single_provider_with_failed_provider_queries_store():
    """Single-provider with a failed_provider (retry scenario) still queries store."""
    store = _store_with_failures({"only": 0})
    # When there IS a failed_provider, we do NOT take the fast path
    # (failed_provider is not None) so the store IS queried.
    result = select_provider(["only"], "only", _cooldown(), store)
    assert result == "only"
    store.count_recent_failures.assert_called()


# ---------------------------------------------------------------------------
# Real store integration (uses actual failure data via isoformat timestamps)
# ---------------------------------------------------------------------------

def test_skips_cooling_with_real_store(herder_home):
    """select_provider with a real Store skips a provider that exceeds failure threshold."""
    from herder.db.store import Store

    store = Store.open()

    # Seed a job and failure attempts for "provA"
    store.enqueue(
        id="j1",
        kind="test",
        role="r",
        provider="provA",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for attempt_no in range(1, 4):  # 3 failures = allowed_fails
        store.record_attempt(
            job_id="j1",
            attempt_no=attempt_no,
            worker_id="w",
            exit_code=1,
            status="failed",
            provider="provA",
            finished_at=now_iso,
        )

    providers = ["provA", "provB"]
    cd = _cooldown(allowed_fails=3)
    result = select_provider(providers, None, cd, store)
    assert result == "provB"
