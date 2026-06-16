"""Tests for the Herder CLI module."""
from pathlib import Path
from herder.cli import main
from herder.db.store import Store
from herder.doctor import ProviderHealth
from herder.services.doctor import DoctorReport


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


def _devcfg_with_manifest(tmp_path: Path) -> str:
    """Create a config with manifest fields set on the provider.

    Args:
        tmp_path: Temporary directory for the config file.

    Returns:
        Path to the created config file as a string.
    """
    p = tmp_path / "manifest.yaml"
    p.write_text(
        "providers:\n"
        "  echo_cli:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    args: []\n"
        "    input: stdin\n"
        "    timeout: 5\n"
        "    output_format: json\n"
        "    supports: [read_only, worktree_write]\n"
        "    cost_hint: '$0'\n"
        "    auth_env: ECHO_KEY\n"
        "roles:\n"
        "  planner: {provider: echo_cli}\n"
        "projects:\n"
        "  example_project: {root: '/path/to/your/project', "
        "default_workspace_mode: readonly, allowed_roles: [planner]}\n"
        "worker: {global_concurrency: 1}\n"
        "doctor: {min_ok_providers: 1}\n"
    )
    return str(p)


def test_doctor_output_includes_manifest_info(
    herder_home: Path, tmp_path: Path, capsys
) -> None:
    """Doctor output line must include manifest fields: fmt, supports, cost, auth_env.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _devcfg_with_manifest(tmp_path), "doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    # The manifest info must appear on the provider's line
    assert "fmt=json" in out
    assert "read_only, worktree_write" in out
    assert "cost=$0" in out
    assert "auth_env=ECHO_KEY" in out


def test_doctor_output_defaults_when_no_manifest(
    herder_home: Path, tmp_path: Path, capsys
) -> None:
    """Doctor output shows default manifest values when provider has none set.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    rc = main(["--config", _devcfg(tmp_path), "doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fmt=text" in out
    assert "supports=*" in out
    assert "cost=-" in out
    assert "auth_env=on-disk" in out


def test_doctor_prov_none_guard_no_crash(
    herder_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """cmd_doctor must not crash when a ProviderHealth row's provider name is
    absent from cfg.providers (prov is None guard).  The line must render
    without the manifest suffix.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
        monkeypatch: Pytest monkeypatch fixture.
        capsys: Pytest fixture for capturing stdout/stderr.
    """
    import herder.cli as cli_mod

    ghost_row = ProviderHealth(
        provider="ghost_provider",
        noninteractive_status="ok",
        auth_status="ok",
        latency_ms=1,
        error_sample=None,
        last_probe_at="2024-01-01T00:00:00+00:00",
    )
    fake_report = DoctorReport(
        rows=[ghost_row],
        ok_count=1,
        min_ok=1,
        passed=True,
        warnings=[],
    )
    monkeypatch.setattr(cli_mod, "run_doctor", lambda *a, **kw: fake_report)

    rc = main(["--config", _devcfg(tmp_path), "doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    # The provider line must be present without crashing
    assert "ghost_provider" in out
    # The manifest suffix (fmt=, supports=, cost=, auth_env=) must NOT appear —
    # the prov-is-None branch emits an empty string instead
    assert "fmt=" not in out
    assert "supports=" not in out
