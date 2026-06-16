"""Tests for runtime field in EnqueueRequest — Task 1.4."""
from __future__ import annotations

import pytest
from herder.config import Config, ConfigError
from herder.db.store import Store
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Config(
        providers={"p": {"type": "ollama", "base_url": "http://x", "model": "m"}},
        roles={"r": {"provider": "p", "permissions": "read_only"}},
        projects={"proj": {"root": str(proj), "allowed_roles": ["r"]}},
        runtimes={"d": {"type": "docker", "image": "i"}},
    )


# NOTE: `herder_home` is the existing conftest fixture that points the default
# store DB at a tmp dir (see tests/test_budget.py). Store.open() takes NO args.

def test_enqueue_persists_runtime(herder_home, tmp_path):
    cfg = _cfg(tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    store = Store.open()
    res = enqueue_job(cfg, store, EnqueueRequest(
        project="proj", role="r", kind="research", prompt_file=str(prompt), runtime="d"))
    row = store.get_job(res.job_id)
    assert row["runtime"] == "d"


def test_enqueue_unknown_runtime_rejected(herder_home, tmp_path):
    cfg = _cfg(tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    store = Store.open()
    with pytest.raises(ConfigError, match="unknown runtime"):
        enqueue_job(cfg, store, EnqueueRequest(
            project="proj", role="r", kind="research", prompt_file=str(prompt), runtime="ghost"))


def test_enqueue_runtime_defaults_none(herder_home, tmp_path):
    cfg = _cfg(tmp_path)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    store = Store.open()
    res = enqueue_job(cfg, store, EnqueueRequest(
        project="proj", role="r", kind="research", prompt_file=str(prompt)))
    assert store.get_job(res.job_id)["runtime"] is None
