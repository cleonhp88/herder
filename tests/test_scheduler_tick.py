"""Scheduler tick tests — cron enqueue, idempotency, missed-slot marking."""
from datetime import datetime, timezone
from pathlib import Path

from herder.config import load_config
from herder.db.store import Store
from herder.loops.scheduler_tick import tick


def _cfg(tmp_path, cron: str = "*/5 * * * *") -> str:
    """Create a minimal test config with one schedule."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    pf = tmp_path / "task.md"
    pf.write_text("scheduled work")
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "schedules:\n"
        f"  - {{id: every5, cron: '{cron}', project: p, role: planner, kind: research, prompt_file: '{pf}'}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600, timezone: 'Asia/Ho_Chi_Minh'}\n"
    )
    return str(c)


def _t(h: int, m: int) -> datetime:
    """Create a fixed UTC instant for testing.

    Note: 03:05:30 UTC = 10:05:30 Asia/Ho_Chi_Minh (UTC+7)
    """
    return datetime(2026, 6, 10, h, m, 30, tzinfo=timezone.utc)


def test_due_minute_enqueues_once(herder_home, tmp_path):
    """Job due now should be enqueued exactly once."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # 03:05:30 UTC = 10:05:30 local → matches */5 cron
    n = tick(cfg, store, _t(3, 5))
    assert n == 1

    jobs = store.list_jobs(status="pending")
    assert len(jobs) == 1
    assert jobs[0]["idempotency_key"].startswith("every5:")


def test_same_minute_restart_does_not_duplicate(herder_home, tmp_path):
    """Restart tick in the same minute should not duplicate the job."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    assert tick(cfg, store, _t(3, 5)) == 1
    assert tick(cfg, store, _t(3, 5)) == 0  # restart within the minute → no dup
    assert len(store.list_jobs()) == 1


def test_non_matching_minute_skips(herder_home, tmp_path):
    """Non-matching minute should not enqueue."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # 03:07 UTC = 10:07 local → doesn't match */5
    assert tick(cfg, store, _t(3, 7)) == 0
    assert len(store.list_jobs()) == 0


def test_missed_slots_marked_not_backfilled(herder_home, tmp_path):
    """Downtime should mark missed slots but not backfill jobs."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # First run at 10:05 local
    assert tick(cfg, store, _t(3, 5)) == 1

    # Daemon down for 20 minutes; next tick at 10:25 local
    # Should enqueue at 10:25 and mark 10:10, 10:15, 10:20 as missed
    assert tick(cfg, store, _t(3, 25)) == 1

    runs = store.list_schedule_runs("every5")
    statuses = sorted(r["status"] for r in runs)
    assert statuses.count("enqueued") == 2
    assert statuses.count("missed") == 3  # 10:10, 10:15, 10:20
    assert len(store.list_jobs()) == 2  # missed ≠ backfilled


def test_disabled_schedule_ignored(herder_home, tmp_path):
    """Disabled schedules should not enqueue jobs."""
    path = _cfg(tmp_path)
    text = Path(path).read_text()
    text = text.replace(
        "  - {id: every5,", "  - {id: every5, enabled: false,"
    )
    Path(path).write_text(text)

    cfg = load_config(path)
    store = Store.open()

    assert tick(cfg, store, _t(3, 5)) == 0


def test_multiple_schedules_independent(herder_home, tmp_path):
    """Multiple schedules should be processed independently."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    pf1 = tmp_path / "task1.md"
    pf1.write_text("task 1")
    pf2 = tmp_path / "task2.md"
    pf2.write_text("task 2")

    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "schedules:\n"
        f"  - {{id: every5, cron: '*/5 * * * *', project: p, role: planner, kind: research, prompt_file: '{pf1}'}}\n"
        f"  - {{id: hourly, cron: '0 * * * *', project: p, role: planner, kind: research, prompt_file: '{pf2}'}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600, timezone: 'Asia/Ho_Chi_Minh'}\n"
    )

    cfg = load_config(str(c))
    store = Store.open()

    # 03:05:00 UTC = 10:05:00 local → every5 matches, hourly doesn't
    n = tick(cfg, store, datetime(2026, 6, 10, 3, 5, 0, tzinfo=timezone.utc))
    assert n == 1
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert "every5:" in jobs[0]["idempotency_key"]

    # 03:00:00 UTC = 10:00:00 local → both every5 AND hourly match
    # (since */5 includes 0, and 0 * matches 10:00)
    n = tick(cfg, store, datetime(2026, 6, 10, 3, 0, 0, tzinfo=timezone.utc))
    assert n == 2  # both match
    jobs = store.list_jobs()
    assert len(jobs) == 3
    assert sum(1 for j in jobs if "every5:" in j["idempotency_key"]) == 2
    assert sum(1 for j in jobs if "hourly:" in j["idempotency_key"]) == 1


def test_schedule_config_persisted_across_ticks(herder_home, tmp_path):
    """Schedule config should be upserted to database."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    tick(cfg, store, _t(3, 5))

    # Verify the schedule was inserted
    row = store.conn.execute(
        "SELECT * FROM schedules WHERE id=?", ("every5",)
    ).fetchone()
    assert row is not None
    assert row["cron"] == "*/5 * * * *"
    assert row["project"] == "p"
    assert row["enabled"] == 1


def test_missed_lookback_bounded(herder_home, tmp_path):
    """Missed slot marking should be bounded by MISSED_LOOKBACK (24 hours)."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # First run at 10:05
    tick(cfg, store, _t(3, 5))

    # Jump forward by 3 days (way beyond MISSED_LOOKBACK)
    # Should only mark ~288 slots (24 hours * 60 / 5), not 864 (3 days * 60 / 5)
    from datetime import timedelta

    future_time = _t(3, 5) + timedelta(days=3)
    tick(cfg, store, future_time)

    runs = store.list_schedule_runs("every5")
    missed_count = sum(1 for r in runs if r["status"] == "missed")
    enqueued_count = sum(1 for r in runs if r["status"] == "enqueued")

    # Expect ~288 missed slots (24h / 5min per slot)
    # + 1 enqueued at the 3-day mark
    assert enqueued_count == 2  # first one + the 3-day one
    assert 280 < missed_count < 300  # ~288, allow some wiggle room


def test_missing_prompt_file_fails_gracefully(herder_home, tmp_path):
    """Missing prompt file should fail gracefully without leaving enqueued row."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    pf = tmp_path / "missing.md"  # don't create it

    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "schedules:\n"
        f"  - {{id: badfile, cron: '* * * * *', project: p, role: planner, kind: research, prompt_file: '{pf}'}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600, timezone: 'Asia/Ho_Chi_Minh'}\n"
    )

    cfg = load_config(str(c))
    store = Store.open()

    n = tick(cfg, store, datetime(2026, 6, 10, 3, 5, 0, tzinfo=timezone.utc))
    # Should not raise, no enqueued job
    assert n == 0

    # Check that no schedule_runs row was recorded (transaction rolled back)
    runs = store.list_schedule_runs("badfile")
    assert len(runs) == 0


def test_timezone_conversion_correct(herder_home, tmp_path):
    """Timezone conversion should be applied correctly."""
    cfg = load_config(_cfg(tmp_path, cron="0 10 * * *"))  # 10:00 local
    store = Store.open()

    # 03:00 UTC = 10:00 local → should match
    n = tick(cfg, store, datetime(2026, 6, 10, 3, 0, 0, tzinfo=timezone.utc))
    assert n == 1

    # 03:01 UTC = 10:01 local → should not match
    n = tick(cfg, store, datetime(2026, 6, 10, 3, 1, 0, tzinfo=timezone.utc))
    assert n == 0


def test_scheduled_for_stored_as_utc(herder_home, tmp_path):
    """All scheduled_for timestamps should be stored as UTC (normalized)."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    tick(cfg, store, _t(3, 5))

    runs = store.list_schedule_runs("every5")
    assert len(runs) == 1
    # scheduled_for should end with +00:00 (UTC)
    assert runs[0]["scheduled_for"].endswith("+00:00")


def test_enqueue_crash_rolls_back_slot_and_retries(herder_home, tmp_path, monkeypatch):
    """Enqueue crash should rollback schedule_runs row and allow retry."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    from herder.loops import scheduler_tick as st

    real = st.enqueue_job

    def boom(*a, **k):
        raise RuntimeError("mid-pass crash")

    monkeypatch.setattr(st, "enqueue_job", boom)
    assert tick(cfg, store, _t(3, 5)) == 0  # crash → no enqueue

    # No schedule_runs row should exist (transaction rolled back)
    runs = store.list_schedule_runs("every5")
    assert len(runs) == 0

    # Restore real enqueue_job and retry the same minute
    monkeypatch.setattr(st, "enqueue_job", real)
    assert tick(cfg, store, _t(3, 5)) == 1  # same minute retried successfully
    assert len(store.list_jobs()) == 1
