"""Tests for the retry CLI command."""
from herder.cli import main
from herder.db.store import Store
from herder.runspace import create_run_dir, snapshot_prompt


def _seed(status="failed"):
    """Create a test job in the database with a specific status."""
    s = Store.open()
    rd = create_run_dir("job_R")
    pp, ph = snapshot_prompt(rd, "x")
    s.enqueue(
        id="job_R",
        kind="research",
        role=None,
        provider="echo",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status=status,
        prompt_path=str(pp),
        prompt_hash=ph,
        run_dir=str(rd),
    )
    return s


def test_retry_failed_job(herder_home):
    """Retry a failed job — transitions from failed to pending."""
    _seed("failed")
    rc = main(["retry", "job_R"])
    assert rc == 0
    assert Store.open().get_job("job_R")["status"] == "pending"


def test_retry_dead_job(herder_home):
    """Retry a dead job — transitions from dead to pending."""
    _seed("dead")
    assert main(["retry", "job_R"]) == 0
    assert Store.open().get_job("job_R")["status"] == "pending"


def test_retry_running_job_rejected(herder_home):
    """Cannot retry a running job — must be in failed/dead/cancelled state."""
    _seed("running")
    assert main(["retry", "job_R"]) == 1


def test_retry_done_job_rejected(herder_home):
    """Cannot retry a done job."""
    _seed("done")
    assert main(["retry", "job_R"]) == 1


def test_retry_unknown_job(herder_home):
    """Retry nonexistent job — returns 1."""
    assert main(["retry", "job_nope"]) == 1
