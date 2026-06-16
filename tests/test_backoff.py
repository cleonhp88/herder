"""Tests for exponential backoff computation and store/claim eligibility gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from herder.backoff import compute_backoff_seconds
from herder.db.store import Store

# ---------------------------------------------------------------------------
# Unit tests: compute_backoff_seconds
# ---------------------------------------------------------------------------


class TestComputeBackoffSeconds:
    """Unit tests for the pure compute_backoff_seconds function."""

    def test_disabled_when_base_zero(self) -> None:
        """Returns 0 for any attempts when base_seconds == 0 (default-OFF)."""
        assert compute_backoff_seconds(0, base_seconds=0, max_seconds=300) == 0
        assert compute_backoff_seconds(1, base_seconds=0, max_seconds=300) == 0
        assert compute_backoff_seconds(5, base_seconds=0, max_seconds=300) == 0

    def test_disabled_when_base_negative(self) -> None:
        """Returns 0 for negative base_seconds (guard clause)."""
        assert compute_backoff_seconds(1, base_seconds=-1, max_seconds=300) == 0

    def test_first_retry_equals_base(self) -> None:
        """First retry (attempts=1) waits exactly base_seconds."""
        assert compute_backoff_seconds(1, base_seconds=5, max_seconds=300) == 5

    def test_exponential_growth(self) -> None:
        """Delay doubles on each subsequent attempt."""
        assert compute_backoff_seconds(2, base_seconds=5, max_seconds=300) == 10
        assert compute_backoff_seconds(3, base_seconds=5, max_seconds=300) == 20
        assert compute_backoff_seconds(4, base_seconds=5, max_seconds=300) == 40

    def test_cap_clamp(self) -> None:
        """Delay is capped at max_seconds even when exponential exceeds it."""
        assert compute_backoff_seconds(10, base_seconds=5, max_seconds=300) == 300
        assert compute_backoff_seconds(100, base_seconds=5, max_seconds=300) == 300

    def test_zero_attempts_floored(self) -> None:
        """Attempts ≤ 0 are treated as no prior attempts → no delay beyond base."""
        # exp = max(0, 0 - 1) = 0 → base * 2**0 = base
        assert compute_backoff_seconds(0, base_seconds=5, max_seconds=300) == 5

    def test_negative_attempts_floored(self) -> None:
        """Negative attempts use the same floor (exp = 0)."""
        assert compute_backoff_seconds(-3, base_seconds=5, max_seconds=300) == 5

    def test_max_seconds_exact(self) -> None:
        """A delay that exactly equals max_seconds is not truncated."""
        assert compute_backoff_seconds(1, base_seconds=300, max_seconds=300) == 300

    def test_base_one_second(self) -> None:
        """Base of 1 second gives 1, 2, 4, 8 … sequence."""
        assert compute_backoff_seconds(1, base_seconds=1, max_seconds=1000) == 1
        assert compute_backoff_seconds(2, base_seconds=1, max_seconds=1000) == 2
        assert compute_backoff_seconds(3, base_seconds=1, max_seconds=1000) == 4
        assert compute_backoff_seconds(4, base_seconds=1, max_seconds=1000) == 8


# ---------------------------------------------------------------------------
# Store tests: next_eligible_at column + requeue / claim_job gate
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


def _add(store: Store, jid: str, **overrides: object) -> str:
    """Enqueue a test job and return its id."""
    fields = dict(_BASE_JOB, id=jid)
    fields.update(overrides)
    store.enqueue(**fields)
    return jid


class TestMigrationV5:
    """Verify schema v5 added the next_eligible_at column to jobs."""

    def test_next_eligible_at_column_exists(self, herder_home: object) -> None:
        """Schema v5: jobs table has a next_eligible_at TEXT column."""
        store = Store.open()
        col_names = {
            row[1] for row in store.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert "next_eligible_at" in col_names

    def test_user_version_is_five(self, herder_home: object) -> None:
        """Schema version pragma is 6 after migration (v6 adds jobs.runtime)."""
        store = Store.open()
        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 6


class TestRequeueNextEligibleAt:
    """Verify requeue sets / clears next_eligible_at correctly."""

    def test_requeue_with_future_timestamp_sets_column(
        self, herder_home: object
    ) -> None:
        """requeue(..., next_eligible_at=<future>) stores the timestamp in the column."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        store.requeue("j1", next_eligible_at=future)

        row = store.get_job("j1")
        assert row["next_eligible_at"] == future

    def test_requeue_with_none_clears_column(self, herder_home: object) -> None:
        """requeue(..., next_eligible_at=None) stores NULL (immediately eligible)."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        store.requeue("j1", next_eligible_at=None)

        row = store.get_job("j1")
        assert row["next_eligible_at"] is None

    def test_requeue_with_provider_and_future_timestamp(
        self, herder_home: object
    ) -> None:
        """requeue with both next_provider and next_eligible_at sets both columns."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        store.requeue("j1", next_provider="other", next_eligible_at=future)

        row = store.get_job("j1")
        assert row["provider"] == "other"
        assert row["next_eligible_at"] == future


class TestClaimJobEligibilityGate:
    """Verify claim_job honours the next_eligible_at eligibility gate."""

    def test_future_eligible_at_blocks_claim(self, herder_home: object) -> None:
        """A pending job with next_eligible_at in the future is NOT claimed."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        # Requeue with a future timestamp (60 seconds from now)
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        store.requeue("j1", next_eligible_at=future)

        assert store.get_job("j1")["status"] == "pending"
        # Must NOT be claimable yet
        claimed = store.claim_job("w2", 3600)
        assert claimed is None, (
            f"Expected None (future-eligible job must not be claimed), "
            f"got job id={claimed['id'] if claimed else None!r}"
        )

    def test_past_eligible_at_allows_claim(self, herder_home: object) -> None:
        """A pending job with next_eligible_at already past IS claimable."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        # Requeue with a past timestamp (60 seconds ago)
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        store.requeue("j1", next_eligible_at=past)

        claimed = store.claim_job("w2", 3600)
        assert claimed is not None
        assert claimed["id"] == "j1"

    def test_null_eligible_at_is_immediately_claimable(
        self, herder_home: object
    ) -> None:
        """A pending job with NULL next_eligible_at (manual retry) is claimed immediately."""
        store = Store.open()
        _add(store, "j1")
        store.claim_job("w1", 3600)
        store.finish_job("j1", "failed")

        store.requeue("j1", next_eligible_at=None)

        claimed = store.claim_job("w2", 3600)
        assert claimed is not None
        assert claimed["id"] == "j1"

    def test_future_eligible_blocks_only_that_job(self, herder_home: object) -> None:
        """A future-eligible job does not block other immediately-claimable jobs."""
        store = Store.open()
        # j_blocked has a future eligible_at; j_ready is immediately claimable
        _add(store, "j_blocked")
        store.claim_job("w1", 3600)
        store.finish_job("j_blocked", "failed")
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        store.requeue("j_blocked", next_eligible_at=future)

        _add(store, "j_ready")  # status=pending, next_eligible_at=NULL

        claimed = store.claim_job("w2", 3600)
        assert claimed is not None
        assert claimed["id"] == "j_ready"


class TestDefaultOffBehaviour:
    """Confirm that with base_seconds=0 the auto-retry path passes next_eligible_at=None."""

    def test_zero_base_produces_none_eligible_at(self) -> None:
        """compute_backoff_seconds(..., base_seconds=0) returns 0 → nea is None."""
        delay = compute_backoff_seconds(1, base_seconds=0, max_seconds=300)
        assert delay == 0
        # Mimic the queue_claim logic: delay > 0 → isoformat, else None
        nea = (
            (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            if delay > 0
            else None
        )
        assert nea is None

    def test_nonzero_base_produces_isoformat_eligible_at(self) -> None:
        """compute_backoff_seconds with base > 0 returns a positive delay → nea is set."""
        delay = compute_backoff_seconds(1, base_seconds=5, max_seconds=300)
        assert delay == 5
        nea = (
            (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            if delay > 0
            else None
        )
        assert nea is not None
        # Must be a parseable ISO timestamp in the future
        parsed = datetime.fromisoformat(nea)
        assert parsed > datetime.now(timezone.utc)
