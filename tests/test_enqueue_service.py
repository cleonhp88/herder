"""Tests for enqueue service and CLI integration."""
from __future__ import annotations

from pathlib import Path

from herder.cli import main
from herder.config import load_config
from herder.db.store import Store
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path: Path) -> str:
    """Create a minimal config YAML for testing.

    Args:
        tmp_path: Temporary test directory.

    Returns:
        Path to the created config file.
    """
    c = tmp_path / "c.yaml"
    proj_root = tmp_path / "proj"
    proj_root.mkdir()
    c.write_text(
        "providers:\n  echo_cli: {type: cli, executable: cat, args: [], input: stdin}\n"
        "roles:\n  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n  p: {{root: '{proj_root}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    return str(c)


def test_dry_run_persists_nothing(herder_home, tmp_path, capsys):
    """Test that dry-run does not persist any job to the database."""
    pf = tmp_path / "t.md"
    pf.write_text("do research")
    rc = main(
        [
            "--config",
            _cfg(tmp_path),
            "enqueue",
            "--project",
            "p",
            "--role",
            "planner",
            "--kind",
            "research",
            "--prompt-file",
            str(pf),
            "--dry-run",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.out
    # Verify no job was persisted
    assert len(Store.open().list_jobs()) == 0


def test_enqueue_then_ps(herder_home, tmp_path, capsys):
    """Test enqueue followed by ps to list jobs."""
    pf = tmp_path / "t.md"
    pf.write_text("do research")
    cfg = _cfg(tmp_path)

    # Enqueue a job
    main(
        [
            "--config",
            cfg,
            "enqueue",
            "--project",
            "p",
            "--role",
            "planner",
            "--kind",
            "research",
            "--prompt-file",
            str(pf),
        ]
    )

    # List jobs
    main(["--config", cfg, "ps"])
    out = capsys.readouterr().out
    assert "research" in out
    assert "pending" in out


def test_missing_project_root_rejected(herder_home, tmp_path):
    """Test that missing project root directory causes error."""
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n  echo_cli: {type: cli, executable: cat, input: stdin}\n"
        "roles:\n  planner: {provider: echo_cli}\n"
        "projects:\n  p: {root: '/no/such/dir', allowed_roles: [planner]}\n"
        "worker: {global_concurrency: 1}\n"
    )
    pf = tmp_path / "t.md"
    pf.write_text("x")
    rc = main(
        [
            "--config",
            str(c),
            "enqueue",
            "--project",
            "p",
            "--role",
            "planner",
            "--kind",
            "research",
            "--prompt-file",
            str(pf),
        ]
    )
    assert rc != 0


def test_duplicate_idempotency_key_dedupes(herder_home, tmp_path):
    """Test that duplicate idempotency key returns existing job without crashing."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    req = EnqueueRequest(
        project="p",
        role="planner",
        kind="research",
        prompt="x",
        idempotency_key="sched:2026-06-10T10:05",
    )
    r1 = enqueue_job(cfg, store, req)
    r2 = enqueue_job(cfg, store, req)  # must NOT raise
    assert r2.job_id == r1.job_id  # deduped to the existing job
    assert len(store.list_jobs()) == 1
