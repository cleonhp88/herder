"""Tests for 'herder approve' and 'herder reject' CLI commands."""
from herder.cli import main
from herder.db.store import Store
from herder.runspace import create_run_dir, snapshot_prompt


def _seed(status="waiting_approval"):
    """Helper to create a job with the given status."""
    s = Store.open()
    rd = create_run_dir("job_AP")
    pp, ph = snapshot_prompt(rd, "x")
    s.enqueue(
        id="job_AP",
        kind="coding",
        role=None,
        provider="echo",
        project=None,
        cwd="/tmp/x",
        workspace_mode="inplace",
        permissions='{"require_confirm": true}',
        status=status,
        prompt_path=str(pp),
        prompt_hash=ph,
        run_dir=str(rd),
    )
    return s


def test_approve_via_cli(herder_home, capsys):
    """Approve a waiting_approval job via CLI."""
    _seed()
    rc = main(["approve", "job_AP"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "approved" in out
    assert Store.open().get_job("job_AP")["status"] == "approved"


def test_reject_via_cli(herder_home, capsys):
    """Reject a waiting_approval job via CLI."""
    _seed()
    rc = main(["reject", "job_AP"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert Store.open().get_job("job_AP")["status"] == "rejected"


def test_approve_unknown(herder_home, capsys):
    """Approve a non-existent job should fail."""
    rc = main(["approve", "job_nope"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out


def test_reject_unknown(herder_home, capsys):
    """Reject a non-existent job should fail."""
    rc = main(["reject", "job_nope"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out


def test_approve_non_waiting_reports_status(herder_home, capsys):
    """Approve a pending job is no-op, reports why."""
    _seed(status="pending")
    rc = main(["approve", "job_AP"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "pending" in out


def test_reject_non_waiting_reports_status(herder_home, capsys):
    """Reject a pending job is no-op, reports why."""
    _seed(status="pending")
    rc = main(["reject", "job_AP"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "pending" in out
