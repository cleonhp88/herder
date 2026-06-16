"""Tests for Phase 4: supervisor resolves and passes runtime to execute."""
from __future__ import annotations

import herder.loops.supervisor as sup
from herder.config import load_config
from herder.db.store import Store
from herder.models import Result
from herder.runtimes.local import LocalRuntime
from herder.services.enqueue import EnqueueRequest, enqueue_job


def _make_cfg(tmp_path) -> object:
    """Build a minimal Config with a cat CLI provider."""
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
        "worker: {global_concurrency: 1}\n"
    )
    return load_config(str(c))


def _fake_result() -> Result:
    """Minimal successful Result for monkeypatching execute."""
    return Result(status="done", exit_code=0, output="ok",
                  started_at=None, finished_at=None)


def test_supervisor_passes_resolved_runtime(monkeypatch, herder_home, tmp_path):
    """execute_job must resolve a runtime and hand it to providers.run.execute.

    With no runtime declared anywhere the resolution chain falls through to
    "local", so the captured runtime must be a LocalRuntime instance.
    """
    captured: dict = {}

    def fake_execute(provider, prompt, **kw):
        captured["runtime"] = kw.get("runtime")
        captured["perms"] = kw.get("perms")
        return _fake_result()

    monkeypatch.setattr(sup, "execute", fake_execute)

    cfg = _make_cfg(tmp_path)
    store = Store.open()

    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("hello")

    enqueue_job(
        cfg,
        store,
        EnqueueRequest(project="p", role="planner", kind="research",
                       prompt="hello"),
    )

    # Claim the job row so supervisor.execute_job receives a real sqlite3.Row.
    job = store.claim_job("w1", lease_seconds=3600)
    assert job is not None

    sup.execute_job(cfg, store, job, "w1")

    assert isinstance(captured["runtime"], LocalRuntime), (
        f"Expected LocalRuntime, got {type(captured['runtime'])!r}"
    )
    assert captured["perms"] is not None, "perms must be forwarded to execute"


def test_supervisor_default_runtime_resolves_local(monkeypatch, herder_home, tmp_path):
    """When no runtime is set on job/provider/project the runtime name resolves to 'local'."""
    from herder.runtimes.resolve import resolve_runtime_name

    name = resolve_runtime_name(
        job_runtime=None,
        provider_runtime=None,
        project_default=None,
    )
    assert name == "local"
