"""Tests for budget caps and active dedup on enqueue."""

import pytest
from pathlib import Path
from herder.config import load_config
from herder.db.store import Store
from herder.errors import BudgetError
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(
    tmp_path: Path, *, active: int = 100, perday: int = 500, dedup: bool = True
) -> str:
    """Create a test config with budget settings.

    Args:
        tmp_path: Temp directory.
        active: max_active_jobs.
        perday: max_jobs_per_day.
        dedup: dedup_active.

    Returns:
        Path to config YAML.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo, permissions: read_only}}\n"
        f"projects: {{p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}}}\n"
        f"budget: {{max_active_jobs: {active}, max_jobs_per_day: {perday}, dedup_active: {str(dedup).lower()}}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    return str(c)


def _req(prompt: str = "hello", **kw) -> EnqueueRequest:
    """Create a test enqueue request.

    Args:
        prompt: Prompt text.
        **kw: Additional EnqueueRequest kwargs.

    Returns:
        EnqueueRequest.
    """
    return EnqueueRequest(
        project="p", role="planner", kind="research", prompt=prompt, **kw
    )


def test_budget_defaults_and_parse(herder_home, tmp_path):
    """Test that budget config parses with defaults and custom values."""
    c = tmp_path / "b.yaml"
    c.write_text(
        "providers: {e: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {r: {provider: e}}\n"
        "projects: {p: {root: '/tmp', allowed_roles: [r]}}\n"
        "budget: {max_active_jobs: 3, max_jobs_per_day: 5}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.budget.max_active_jobs == 3
    assert cfg.budget.max_jobs_per_day == 5
    assert cfg.budget.dedup_active is True


def test_active_cap_refuses(herder_home, tmp_path):
    """Test that exceeding max_active_jobs cap refuses enqueue."""
    cfg = load_config(_cfg(tmp_path, active=2))
    store = Store.open()
    enqueue_job(cfg, store, _req("a"))
    enqueue_job(cfg, store, _req("b"))
    with pytest.raises(BudgetError):
        enqueue_job(cfg, store, _req("c"))  # 3rd active → refused


def test_daily_cap_refuses(herder_home, tmp_path):
    """Test that exceeding max_jobs_per_day cap refuses enqueue."""
    cfg = load_config(_cfg(tmp_path, perday=2))
    store = Store.open()
    enqueue_job(cfg, store, _req("a"))
    enqueue_job(cfg, store, _req("b"))
    with pytest.raises(BudgetError):
        enqueue_job(cfg, store, _req("c"))


def test_active_dedup_collapses(herder_home, tmp_path):
    """Test that identical active submissions are deduped."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    r1 = enqueue_job(cfg, store, _req("same"))
    r2 = enqueue_job(cfg, store, _req("same"))  # identical, still pending → same job
    assert r2.job_id == r1.job_id
    assert len(store.list_jobs()) == 1


def test_dedup_allows_rerun_after_terminal(herder_home, tmp_path):
    """Test that completed jobs can be re-run (dedup only applies to active).

    Setup: enqueue → claim (pending→running) → finish (running→done).
    Both steps are legal FSM transitions.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    r1 = enqueue_job(cfg, store, _req("same"))
    # Advance through the legal path: pending → running → done
    store.claim_job("test-worker", 3600)
    store.finish_job(r1.job_id, "done")
    r2 = enqueue_job(cfg, store, _req("same"))  # previous finished → new job allowed
    assert r2.job_id != r1.job_id
    assert len(store.list_jobs()) == 2


def test_dedup_off_allows_duplicates(herder_home, tmp_path):
    """Test that with dedup_active=false, identical prompts enqueue separately."""
    cfg = load_config(_cfg(tmp_path, dedup=False))
    store = Store.open()
    enqueue_job(cfg, store, _req("same"))
    enqueue_job(cfg, store, _req("same"))
    assert len(store.list_jobs()) == 2


def test_dry_run_not_capped(herder_home, tmp_path):
    """Test that dry-run bypasses budget caps."""
    cfg = load_config(_cfg(tmp_path, active=0))
    store = Store.open()
    res = enqueue_job(cfg, store, _req("x", dry_run=True))  # dry-run bypasses caps
    assert res.dry_run is True
    # Also should have no jobs in DB
    assert len(store.list_jobs()) == 0


def test_idempotency_key_bypasses_dedup(herder_home, tmp_path):
    """Test that idempotency_key prevents active dedup."""
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()
    r1 = enqueue_job(cfg, store, _req("x", idempotency_key="key1"))
    r2 = enqueue_job(cfg, store, _req("x", idempotency_key="key2"))
    # Even though prompts are identical, different idempotency keys allow separate jobs
    assert r2.job_id != r1.job_id
    assert len(store.list_jobs()) == 2


def test_idempotency_key_still_hits_budget(herder_home, tmp_path):
    """Test that idempotency_key does NOT bypass budget caps."""
    cfg = load_config(_cfg(tmp_path, active=1))
    store = Store.open()
    enqueue_job(cfg, store, _req("a", idempotency_key="key1"))
    with pytest.raises(BudgetError):
        enqueue_job(
            cfg, store, _req("b", idempotency_key="key2")
        )  # budget still enforced


def test_enqueue_works_inside_outer_transaction(herder_home, tmp_path):
    """Test that enqueue_job works when called inside an outer transaction (scheduler path).

    This verifies the transaction nesting fix: enqueue_job detects outer BEGIN IMMEDIATE
    and does NOT attempt its own BEGIN (which would raise 'cannot start a transaction within a transaction').
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # Simulate scheduler's outer transaction
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        r = enqueue_job(cfg, store, _req("x"))
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise

    # Verify job was persisted
    assert r.job_id and len(store.list_jobs()) == 1


def test_dedup_distinguishes_kind(herder_home, tmp_path):
    """Test that dedup key includes 'kind', so different kinds don't collapse.

    Same prompt + role + project but different kind → separate jobs.
    This ensures research jobs don't get collapsed with planner jobs
    that happen to have the same prompt text.
    """
    cfg = load_config(_cfg(tmp_path))
    store = Store.open()

    # Enqueue with kind='research' (default from _req)
    r1 = enqueue_job(cfg, store, _req("same"))
    # Enqueue with same prompt but kind='automation' (override the default)
    req2 = EnqueueRequest(project="p", role="planner", kind="automation", prompt="same")
    r2 = enqueue_job(cfg, store, req2)

    # Should be different jobs (different kind)
    assert r2.job_id != r1.job_id
    assert len(store.list_jobs()) == 2

    # Verify kinds are correct
    j1 = store.get_job(r1.job_id)
    j2 = store.get_job(r2.job_id)
    assert j1["kind"] == "research"
    assert j2["kind"] == "automation"
