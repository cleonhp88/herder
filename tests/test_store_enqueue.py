"""Tests for Store.enqueue, get_job, and list_jobs methods."""
import pytest

from herder.db.store import Store, StoreError

BASE = dict(
    id="job_T1",
    kind="research",
    role="planner",
    provider=None,
    project="p",
    cwd="/tmp/x",
    workspace_mode="readonly",
    permissions="{}",
    status="pending",
    prompt_path="/tmp/x/prompt.md",
    prompt_hash="abc",
    run_dir="/tmp/x",
)


def test_enqueue_and_get(herder_home):
    """Enqueue a job and retrieve it."""
    s = Store.open()
    s.enqueue(**BASE)
    j = s.get_job("job_T1")
    assert j["status"] == "pending"
    assert j["prompt_hash"] == "abc"


def test_unknown_field_rejected(herder_home):
    """Enqueue rejects unknown fields."""
    s = Store.open()
    with pytest.raises(StoreError):
        s.enqueue(**BASE, bogus_field="x")


def test_list_filter(herder_home):
    """List jobs filters by status."""
    s = Store.open()
    s.enqueue(**BASE)
    assert len(s.list_jobs(status="pending")) == 1
    assert len(s.list_jobs(status="done")) == 0
