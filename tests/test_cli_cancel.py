"""Tests for 'herder cancel' CLI command."""
from herder.cli import main
from herder.db.store import Store
from herder.runspace import create_run_dir, snapshot_prompt


def test_cancel_pending_via_cli(herder_home, capsys):
    """Cancel a pending job via CLI; should report cancelled status."""
    s = Store.open()
    rd = create_run_dir("job_C")
    pp, ph = snapshot_prompt(rd, "x")
    s.enqueue(
        id="job_C",
        kind="research",
        role=None,
        provider="echo",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="pending",
        prompt_path=str(pp),
        prompt_hash=ph,
        run_dir=str(rd),
    )
    rc = main(["cancel", "job_C"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cancelled" in out
    assert Store.open().get_job("job_C")["status"] == "cancelled"


def test_cancel_unknown_via_cli(herder_home, capsys):
    """Cancel a non-existent job; should report not found."""
    rc = main(["cancel", "job_nope"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not found" in out
