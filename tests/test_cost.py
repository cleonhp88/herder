"""Tests for cost estimation and per-job cost accrual."""

from __future__ import annotations

import pytest

from herder.cost import _RATES, estimate_cost
from herder.db.store import Store

# ---------------------------------------------------------------------------
# Minimal job fixture for Store-level tests
# ---------------------------------------------------------------------------

_BASE_JOB: dict = dict(
    kind="test",
    role="r",
    provider="p",
    project="proj",
    cwd="/tmp/x",
    workspace_mode="readonly",
    permissions="{}",
    status="pending",
    prompt_path="/tmp/x/p.md",
    prompt_hash="abc",
    run_dir="/tmp/x",
)


def _enqueue(store: Store, jid: str) -> str:
    """Enqueue a minimal test job and return its id.

    Args:
        store: Open Store instance.
        jid: Job id to use.

    Returns:
        The job id.
    """
    store.enqueue(**dict(_BASE_JOB, id=jid))
    return jid


class TestEstimateCost:
    """Cost estimation from usage metrics."""

    def test_local_rate_is_zero(self) -> None:
        """Local/free providers return zero cost."""
        usage = {"eval_count": 1000, "prompt_eval_count": 500}
        assert estimate_cost(usage, "local") == 0.0
        assert estimate_cost(usage, "free") == 0.0

    def test_unknown_cost_key_returns_none(self) -> None:
        """Unknown cost_key returns None."""
        usage = {"eval_count": 1000, "prompt_eval_count": 500}
        assert estimate_cost(usage, "mystery_key") is None
        assert estimate_cost(usage, None) is None

    def test_no_usage_returns_none(self) -> None:
        """None or empty usage returns None."""
        assert estimate_cost(None, "local") is None
        assert estimate_cost({}, "local") is None  # empty dict → falsy → None

    def test_api_token_naming(self) -> None:
        """API format (input_tokens, output_tokens) supported."""
        usage = {"input_tokens": 1000, "output_tokens": 500}
        # local rate is 0, so should be 0 regardless of token format
        assert estimate_cost(usage, "local") == 0.0

    def test_fallback_token_keys(self) -> None:
        """Fallback: missing keys treated as 0 tokens."""
        usage_partial = {"eval_count": 500}
        assert estimate_cost(usage_partial, "local") == 0.0
        usage_other = {"unknown_key": 999}
        assert estimate_cost(usage_other, "local") == 0.0


# ---------------------------------------------------------------------------
# Store.accrue_cost — unit tests
# ---------------------------------------------------------------------------


class TestAccrueCost:
    """Unit tests for Store.accrue_cost — NULL-safe accumulation."""

    def test_first_accrual_from_null(self, herder_home: object) -> None:
        """First accrue on a NULL total_cost sets it to the given amount."""
        store = Store.open()
        jid = _enqueue(store, "j1")

        # Precondition: total_cost is NULL at creation
        row = store.get_job(jid)
        assert row["total_cost"] is None

        store.accrue_cost(jid, 0.5)

        row = store.get_job(jid)
        assert row["total_cost"] == pytest.approx(0.5)

    def test_successive_calls_sum(self, herder_home: object) -> None:
        """Successive accrue calls accumulate into total_cost."""
        store = Store.open()
        jid = _enqueue(store, "j2")

        store.accrue_cost(jid, 0.1)
        store.accrue_cost(jid, 0.2)
        store.accrue_cost(jid, 0.3)

        row = store.get_job(jid)
        assert row["total_cost"] == pytest.approx(0.6)

    def test_zero_amount_initialises_from_null(self, herder_home: object) -> None:
        """Accruing zero flips total_cost from NULL to 0.0 (COALESCE(NULL,0)+0=0)."""
        store = Store.open()
        jid = _enqueue(store, "j3")

        store.accrue_cost(jid, 0.0)

        row = store.get_job(jid)
        assert row["total_cost"] == pytest.approx(0.0)

    def test_independent_jobs_isolated(self, herder_home: object) -> None:
        """Accruing cost on one job does not affect another job's total_cost."""
        store = Store.open()
        jid_a = _enqueue(store, "ja")
        jid_b = _enqueue(store, "jb")

        store.accrue_cost(jid_a, 1.0)

        row_b = store.get_job(jid_b)
        assert row_b["total_cost"] is None


# ---------------------------------------------------------------------------
# Integration: estimate_cost → accrue_cost wiring
# ---------------------------------------------------------------------------


class TestAccrueCostIntegration:
    """Integration tests verifying estimate_cost feeds correctly into accrue_cost."""

    def test_known_rate_populates_total_cost(self, herder_home: object) -> None:
        """A provider with a known rate key ends with total_cost populated after accrual.

        Uses a temporary custom rate entry so the test is deterministic and
        independent of production rate changes.
        """
        # Inject a test rate (1 USD per 1M input tokens, 2 USD per 1M output tokens)
        _RATES["test_rate"] = (1.0, 2.0)
        try:
            usage = {"input_tokens": 500_000, "output_tokens": 250_000}
            # expected: (0.5 * 1.0) + (0.25 * 2.0) = 0.5 + 0.5 = 1.0 USD
            cost = estimate_cost(usage, "test_rate")
            assert cost == pytest.approx(1.0)

            store = Store.open()
            jid = _enqueue(store, "ji1")
            assert cost is not None
            store.accrue_cost(jid, cost)

            row = store.get_job(jid)
            assert row["total_cost"] == pytest.approx(1.0)
        finally:
            _RATES.pop("test_rate", None)

    def test_unknown_rate_leaves_total_cost_null(self, herder_home: object) -> None:
        """estimate_cost returning None must NOT call accrue_cost — total_cost stays NULL.

        This mirrors the supervisor guard: ``if attempt_cost is not None: accrue``.
        """
        usage = {"input_tokens": 1000, "output_tokens": 500}
        cost = estimate_cost(usage, "no_such_key")
        assert cost is None

        store = Store.open()
        jid = _enqueue(store, "ji2")

        # Replicate the supervisor guard exactly — do not accrue when None
        if cost is not None:
            store.accrue_cost(jid, cost)

        row = store.get_job(jid)
        assert row["total_cost"] is None

    def test_multi_attempt_accumulation(self, herder_home: object) -> None:
        """Multiple attempts on the same job accumulate into total_cost.

        Verifies the COALESCE pattern survives repeated calls (simulating retries).
        """
        _RATES["retry_rate"] = (0.0, 2.0)  # 2 USD per 1M output tokens
        try:
            store = Store.open()
            jid = _enqueue(store, "ji3")

            # Three attempts, each with 1M output tokens → 2.0 USD each
            for _ in range(3):
                usage = {"output_tokens": 1_000_000}
                cost = estimate_cost(usage, "retry_rate")
                assert cost is not None
                store.accrue_cost(jid, cost)

            row = store.get_job(jid)
            assert row["total_cost"] == pytest.approx(6.0)
        finally:
            _RATES.pop("retry_rate", None)
