"""Tests for the Herder CLI module."""
import pytest
from pathlib import Path
from herder.cli import main
from herder.db.store import Store


def _devcfg(tmp_path: Path) -> str:
    """Create a minimal test config file.

    Args:
        tmp_path: Temporary directory for the config file.

    Returns:
        Path to the created config file as a string.
    """
    p = tmp_path / "c.yaml"
    p.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 5}\n"
        "roles:\n"
        "  planner: {provider: echo_cli}\n"
        "projects:\n"
        "  example_project: {root: '/path/to/your/project', default_workspace_mode: readonly, allowed_roles: [planner]}\n"
        "worker: {global_concurrency: 1}\n"
        "doctor: {min_ok_providers: 1}\n"
    )
    return str(p)


def test_doctor_persists_provider_health(herder_home: Path, tmp_path: Path, capsys) -> None:
    """Verify that doctor probe results are persisted to the database.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _devcfg(tmp_path), "doctor"])
    assert rc == 0
    rows = Store.open().list_provider_health()
    assert any(
        r["provider"] == "echo_cli" and r["noninteractive_status"] == "ok"
        for r in rows
    )


def test_doctor_min_ok_threshold_fails(herder_home: Path, tmp_path: Path) -> None:
    """Verify that doctor fails when ok count falls below threshold.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    rc = main(["--config", _devcfg(tmp_path), "doctor", "--min-ok", "3"])
    assert rc == 1  # only 1 provider ok, threshold 3 → fail
