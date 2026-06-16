"""Tests for garbage collection service."""
from datetime import datetime, timezone, timedelta
from pathlib import Path

from herder.config import load_config
from herder.db.store import Store
from herder.runspace import create_run_dir, snapshot_prompt
from herder.services.gc import run_gc


def _cfg(tmp_path: Path) -> str:
    """Create a minimal test config file.

    Args:
        tmp_path: Temporary directory for the config file.

    Returns:
        Path to the created config file as a string.
    """
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {r: {provider: echo}}\n"
        "projects: {p: {root: '/tmp/x', allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1}\n"
        "retention: {keep_done_days: 30, keep_failed_days: 90, keep_logs_days: 30}\n"
    )
    return str(c)


def _job(store: Store, jid: str, status: str, finished_at: str | None) -> Path:
    """Create a test job with sample output.

    Args:
        store: Database store.
        jid: Job ID.
        status: Job status.
        finished_at: ISO8601 finished timestamp, or None.

    Returns:
        Path to the job's run directory.
    """
    rd = create_run_dir(jid)
    pp, ph = snapshot_prompt(rd, "x")
    (rd / "result.md").write_text("y" * 100)  # 100 bytes to verify size calc
    store.enqueue(
        id=jid,
        kind="research",
        role="r",
        provider="echo",
        project="p",
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status=status,
        prompt_path=str(pp),
        prompt_hash=ph,
        run_dir=str(rd),
    )
    if finished_at:
        store.conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", (finished_at, jid))
    return rd


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _ago(days: int) -> str:
    """Return an ISO8601 timestamp N days ago from NOW.

    Args:
        days: Number of days in the past.

    Returns:
        ISO8601 string.
    """
    return (NOW - timedelta(days=days)).isoformat()


def test_gc_removes_old_done(herder_home: Path, tmp_path: Path) -> None:
    """Verify old done jobs are garbage collected.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "old_done", "done", _ago(40))  # 40 days ago > 30 day retention
    rep = run_gc(store, cfg, NOW)
    assert "old_done" in rep.deleted
    assert not rd.exists()
    assert rep.freed_bytes > 0


def test_gc_keeps_recent_done(herder_home: Path, tmp_path: Path) -> None:
    """Verify recent done jobs are retained.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "new_done", "done", _ago(5))  # 5 days ago < 30 day retention
    rep = run_gc(store, cfg, NOW)
    assert "new_done" not in rep.deleted
    assert rd.exists()
    assert rep.skipped_too_recent == 1


def test_gc_failed_uses_90d(herder_home: Path, tmp_path: Path) -> None:
    """Verify failed jobs use 90-day retention, not 30-day.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd40 = _job(store, "fail40", "failed", _ago(40))  # < 90 → kept
    rd100 = _job(store, "fail100", "failed", _ago(100))  # > 90 → deleted
    rep = run_gc(store, cfg, NOW)
    assert rd40.exists()
    assert not rd100.exists()
    assert rep.deleted == ["fail100"]


def test_gc_never_touches_nonterminal(herder_home: Path, tmp_path: Path) -> None:
    """Verify non-terminal jobs are never garbage collected.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "running", "running", _ago(999))  # very old but running
    rep = run_gc(store, cfg, NOW)
    assert rd.exists()
    assert "running" not in rep.deleted


def test_gc_dry_run_deletes_nothing(herder_home: Path, tmp_path: Path) -> None:
    """Verify dry_run reports deletions without actually deleting.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "old", "done", _ago(40))  # old done job
    rep = run_gc(store, cfg, NOW, dry_run=True)
    assert rep.dry_run
    assert "old" in rep.deleted
    assert rd.exists()  # should NOT have been deleted


def test_gc_respects_terminal_states(herder_home: Path, tmp_path: Path) -> None:
    """Verify only terminal states are collected.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    # All these should be skipped (non-terminal), even if very old
    states = ["pending", "waiting", "approved", "cancelling"]
    rds = {}
    for state in states:
        rd = _job(store, f"job_{state}", state, _ago(999))
        rds[state] = rd

    rep = run_gc(store, cfg, NOW)
    assert rep.skipped_nonterminal == len(states)
    for state in states:
        assert rds[state].exists()
        assert f"job_{state}" not in rep.deleted


def test_gc_handles_missing_finished_at(herder_home: Path, tmp_path: Path) -> None:
    """Verify jobs without finished_at are skipped.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "no_finish", "done", None)  # Terminal but no finished_at
    rep = run_gc(store, cfg, NOW)
    assert rd.exists()
    assert "no_finish" not in rep.deleted
    assert rep.skipped_nonterminal >= 1  # counted as non-collectible


def test_gc_handles_missing_run_dir(herder_home: Path, tmp_path: Path) -> None:
    """Verify jobs with non-existent run_dir are skipped safely.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    import shutil

    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    rd = _job(store, "missing_dir", "done", _ago(40))
    shutil.rmtree(rd)  # Remove the run_dir after job was created
    # Should not crash, should not delete anything
    rep = run_gc(store, cfg, NOW)
    assert "missing_dir" not in rep.deleted
    assert rep.freed_bytes == 0
