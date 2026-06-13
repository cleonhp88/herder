"""Tests for herder stats service and CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from herder.cli import main
from herder.db.store import Store
from herder.services.stats import compute_stats


def _job(
    store: Store,
    jid: str,
    provider: str,
    status: str,
    created: str = "2026-06-10T00:00:00+00:00",
) -> None:
    """Helper to enqueue a test job."""
    store.enqueue(
        id=jid,
        kind="research",
        role="r",
        provider=provider,
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status=status,
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
        created_at=created,
    )


def test_jobs_by_status(herder_home):
    """Test that jobs are counted by status."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    _job(s, "b", "claude", "failed")
    _job(s, "c", "codex", "pending")
    rep = compute_stats(s)
    assert rep.jobs_by_status == {"done": 1, "failed": 1, "pending": 1}


def test_provider_success_rate_and_latency_and_tokens(herder_home):
    """Test per-provider success rate, latency percentiles, and token totals."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    _job(s, "b", "claude", "done")
    _job(s, "c", "claude", "failed")

    s.record_attempt(
        job_id="a",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        usage={"eval_count": 10, "prompt_eval_count": 5},
        duration_ms=100,
    )
    s.record_attempt(
        job_id="b",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        usage={"eval_count": 20},
        duration_ms=300,
    )
    s.record_attempt(
        job_id="c",
        attempt_no=1,
        worker_id="w",
        exit_code=1,
        status="failed",
        duration_ms=50,
    )

    rep = compute_stats(s)
    claude = next(p for p in rep.providers if p.provider == "claude")
    assert claude.runs == 3
    assert claude.done == 2
    assert claude.failed == 1
    assert abs(claude.success_rate - 2 / 3) < 0.01
    assert claude.total_tokens == 35  # 10+5 + 20
    assert claude.p50_ms in (50, 100, 300)
    assert claude.p95_ms == 300


def test_empty_store(herder_home):
    """Test that compute_stats handles empty database gracefully."""
    rep = compute_stats(Store.open())
    assert rep.jobs_by_status == {}
    assert rep.providers == []
    assert rep.jobs_last_7d == {}


def test_seven_day_volume(herder_home):
    """Test that 7-day job volume is computed correctly.

    Dates are seeded relative to now (UTC) so the test never rots: the prior
    hard-coded version silently fell out of the trailing-7-day window as time
    passed. The window in stats.py is ``date(created_at) >= date('now','-6 days')``
    (today + previous 6 days). We use offsets 0/1/5 (well inside, immune to a
    midnight boundary skew) and 10 (well outside), and assert against date keys
    derived from the SAME ``now`` the seeds were built from.
    """
    now = datetime.now(timezone.utc)

    def at(days_ago: int, hour: int = 0) -> str:
        return (now - timedelta(days=days_ago)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        ).isoformat()

    def key(days_ago: int) -> str:
        return (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    s = Store.open()
    _job(s, "a1", "claude", "done", at(0, 0))
    _job(s, "a2", "claude", "done", at(0, 10))
    _job(s, "b1", "claude", "done", at(1))
    _job(s, "c1", "claude", "done", at(5))
    _job(s, "old", "claude", "done", at(10))

    rep = compute_stats(s)
    # Within window: today (×2), yesterday, 5 days ago. NOT 10 days ago.
    assert rep.jobs_last_7d.get(key(0)) == 2
    assert rep.jobs_last_7d.get(key(1)) == 1
    assert rep.jobs_last_7d.get(key(5)) == 1
    assert key(10) not in rep.jobs_last_7d


def test_percentile_calculation_with_no_durations(herder_home):
    """Test that percentiles are None when no duration data exists."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    s.record_attempt(
        job_id="a",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        duration_ms=None,  # No duration
    )
    rep = compute_stats(s)
    claude = next(p for p in rep.providers if p.provider == "claude")
    assert claude.p50_ms is None
    assert claude.p95_ms is None


def test_tokens_with_missing_usage(herder_home):
    """Test that tokens default to 0 when usage is not provided."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    s.record_attempt(
        job_id="a",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        usage=None,  # No usage
        duration_ms=100,
    )
    rep = compute_stats(s)
    claude = next(p for p in rep.providers if p.provider == "claude")
    assert claude.total_tokens == 0


def test_stats_cli_runs(herder_home, capsys):
    """Test that stats CLI command runs without error."""
    from herder.cli import main

    rc = main(["stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "jobs by status" in out


def test_stats_cli_with_data(herder_home, capsys):
    """Test that stats CLI displays collected data correctly."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    _job(s, "b", "claude", "failed")
    s.record_attempt(
        job_id="a",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        usage={"eval_count": 100},
        duration_ms=250,
    )

    rc = main(["stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "done" in out
    assert "failed" in out
    assert "claude" in out
    assert "100" in out  # tokens


def test_stats_cli_json_output(herder_home, capsys):
    """Test that stats --json outputs valid JSON."""
    s = Store.open()
    _job(s, "a", "claude", "done")
    s.record_attempt(
        job_id="a",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        duration_ms=150,
    )

    rc = main(["stats", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "jobs_by_status" in data
    assert "providers" in data
    assert "jobs_last_7d" in data


def test_stats_buckets_null_provider(herder_home):
    """Test that NULL provider attempts are bucketed as '(none)'."""
    s = Store.open()
    s.enqueue(
        id="np",
        kind="research",
        role="r",
        provider=None,
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/p",
        prompt_hash="h",
        run_dir="/tmp/x",
    )
    s.record_attempt(
        job_id="np",
        attempt_no=1,
        worker_id="w",
        exit_code=0,
        status="done",
        duration_ms=10,
    )
    rep = compute_stats(s)
    assert any(p.provider == "(none)" for p in rep.providers)
