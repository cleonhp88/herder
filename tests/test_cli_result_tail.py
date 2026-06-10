"""Tests for the 'herder result' and 'herder tail' CLI commands."""
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


def _run_one(cfg: str, tmp_path: Path, capsys) -> str:
    """Enqueue and run a single job, then return its job ID.

    Args:
        cfg: Path to config file.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.

    Returns:
        The job ID of the executed job.
    """
    pf = tmp_path / "t.md"
    pf.write_text("xin chào")
    main(["--config", cfg, "enqueue", "--project", "p", "--role", "planner",
          "--kind", "research", "--prompt-file", str(pf)])
    main(["--config", cfg, "worker", "--once"])
    capsys.readouterr()  # discard enqueue/worker output
    return Store.open().list_jobs(status="done")[0]["id"]


def test_result_prints_result_md(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder result' prints the result.md file.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    cfg = _cfg(tmp_path)
    jid = _run_one(cfg, tmp_path, capsys)
    rc = main(["--config", cfg, "result", jid])
    out = capsys.readouterr().out
    assert rc == 0
    assert "xin chào" in out          # cat echoed the prompt into the body
    assert "job_id:" in out           # frontmatter included


def test_result_unknown_job(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder result' returns 1 for unknown job.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _cfg(tmp_path), "result", "job_nope"])
    assert rc == 1


def test_tail_prints_logs(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder tail' prints stdout and stderr logs.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    cfg = _cfg(tmp_path)
    jid = _run_one(cfg, tmp_path, capsys)
    rc = main(["--config", cfg, "tail", jid])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stdout" in out            # section header
    assert "xin chào" in out          # cat's stdout


def test_tail_unknown_job(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that 'herder tail' returns 1 for unknown job.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _cfg(tmp_path), "tail", "job_nope"])
    assert rc == 1
