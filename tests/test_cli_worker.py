"""Tests for the 'herder worker' and 'herder schedules' CLI commands."""
import stat
from pathlib import Path

from herder.cli import main
from herder.db.store import Store


def _cfg(tmp_path: Path) -> str:
    """Create a minimal test config with cat CLI provider.

    Args:
        tmp_path: Temporary directory for the config file.

    Returns:
        Path to the created config file as a string.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def test_worker_once_processes_pending(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder worker --once' processes all claimable jobs then exits.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    cfg = _cfg(tmp_path)
    pf = tmp_path / "t.md"
    pf.write_text("hello cat")

    # Enqueue a job
    rc = main(["--config", cfg, "enqueue", "--project", "p", "--role", "planner",
               "--kind", "research", "--prompt-file", str(pf)])
    assert rc == 0

    # Run worker --once
    rc = main(["--config", cfg, "worker", "--once"])
    assert rc == 0

    # Check output
    out = capsys.readouterr().out
    assert "processed 1" in out

    # Verify job is marked done
    done = Store.open().list_jobs(status="done")
    assert len(done) == 1


def test_worker_once_empty_queue(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder worker --once' with no pending jobs returns 0 and reports count.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    cfg = _cfg(tmp_path)
    rc = main(["--config", cfg, "worker", "--once"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "processed 0" in out


def test_worker_once_runs_due_schedule(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder worker --once' enqueues and processes scheduled jobs.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    pf = tmp_path / "task.md"
    pf.write_text("scheduled hello")
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "schedules:\n"
        f"  - {{id: everymin, cron: '* * * * *', project: p, role: planner, kind: research, prompt_file: '{pf}'}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600, timezone: 'UTC'}\n"
    )
    rc = main(["--config", str(c), "worker", "--once"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "processed 1" in out  # '* * * * *' is due every minute → enqueued + processed
    done = Store.open().list_jobs(status="done")
    assert len(done) == 1
    assert done[0]["idempotency_key"].startswith("everymin:")


def test_schedules_command_lists_config_and_last_run(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder schedules' lists all configured schedules with cron and last run time.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    pf = tmp_path / "task.md"
    pf.write_text("x")
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "schedules:\n"
        f"  - {{id: daily, cron: '0 22 * * *', project: p, role: planner, kind: research, prompt_file: '{pf}'}}\n"
        "worker: {global_concurrency: 1, timezone: 'UTC'}\n"
    )
    rc = main(["--config", str(c), "schedules"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "daily" in out and "0 22 * * *" in out


def test_worker_once_registers_in_workers_table(herder_home: Path, tmp_path: Path) -> None:
    """Verify that 'herder worker --once' registers the worker in the workers table and marks it stopped after exit.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    rc = main(["--config", _cfg(tmp_path), "worker", "--once", "--worker-id", "w_test"])
    assert rc == 0

    row = Store.open().conn.execute(
        "SELECT * FROM workers WHERE worker_id='w_test'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "stopped"


def test_worker_once_marks_stopped_on_exit(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder worker --once' marks worker as stopped on exit.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _cfg(tmp_path), "worker", "--once", "--worker-id", "w_stop"])
    assert rc == 0
    row = Store.open().conn.execute("SELECT status FROM workers WHERE worker_id='w_stop'").fetchone()
    assert row["status"] == "stopped"


def test_worker_run_artifacts_are_owner_only(herder_home: Path, tmp_path: Path) -> None:
    """Verify that worker creates run artifacts with owner-only permissions (0o077 umask).

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    pf = tmp_path / "t.md"
    pf.write_text("hi")
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    main(["--config", str(c), "enqueue", "--project", "p", "--role", "planner",
          "--kind", "research", "--prompt-file", str(pf)])
    main(["--config", str(c), "worker", "--once"])
    j = Store.open().list_jobs(status="done")[0]
    result = Path(j["run_dir"]) / "result.md"
    mode = stat.S_IMODE(result.stat().st_mode)
    assert mode & 0o077 == 0, f"result.md is group/other-accessible: {oct(mode)}"
