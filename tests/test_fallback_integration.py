"""End-to-end integration tests for Tier 2 provider fallback / cooldown routing.

Tests the full pipeline:
  enqueue → (cooldown-aware resolve) → worker run → fail → requeue with next_provider
  → worker run → done.

Provider stubs use real executables so no mocking is needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def _two_provider_cfg(tmp_path: Path, failing_exec: str, failing_args: list[str]) -> str:
    """Config with two providers: 'failing' (always exits 1) and 'succeeding' (cat).

    Args:
        tmp_path: Temporary directory for project root and config file.
        failing_exec: Executable for the failing provider.
        failing_args: Args for the failing provider.

    Returns:
        Path to the config YAML file.
    """
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"

    # YAML-escape the args list
    args_yaml = ", ".join(f"'{a}'" for a in failing_args)
    c.write_text(
        "providers:\n"
        f"  failing: {{type: cli, executable: '{failing_exec}', args: [{args_yaml}],"
        " input: stdin, parser: text, timeout: 10}\n"
        "  succeeding: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  r:\n"
        "    providers: [failing, succeeding]\n"
        "    permissions: read_only\n"
        "    cooldown: {allowed_fails: 3, window_seconds: 300}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def _single_provider_cfg(tmp_path: Path, failing_exec: str, failing_args: list[str]) -> str:
    """Config with a single provider that always fails."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"
    args_yaml = ", ".join(f"'{a}'" for a in failing_args)
    c.write_text(
        "providers:\n"
        f"  failing: {{type: cli, executable: '{failing_exec}', args: [{args_yaml}],"
        " input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  r: {provider: failing, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


def _cooldown_enqueue_cfg(tmp_path: Path) -> str:
    """Config with two providers where primary is seeded with enough failures."""
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  primary: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "  secondary: {type: cli, executable: cat, args: [], input: stdin, parser: text, timeout: 10}\n"
        "roles:\n"
        "  r:\n"
        "    providers: [primary, secondary]\n"
        "    permissions: read_only\n"
        "    cooldown: {allowed_fails: 3, window_seconds: 300}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [r]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


# ---------------------------------------------------------------------------
# Test (a): failing → requeue with 'succeeding' → done
# ---------------------------------------------------------------------------

def test_fallback_failing_to_succeeding_provider(herder_home, tmp_path):
    """Two-provider role: first attempt uses 'failing', requeue advances to 'succeeding'.

    run_pending_once loops until the queue is empty, so both the initial failure
    and the successful retry happen within a single call.

    Verifies:
    - Job starts on 'failing' provider (no prior failures → primary chosen).
    - After failure, it is internally requeued with provider='succeeding'.
    - The succeeding provider runs and the job reaches 'done'.
    - Attempts table records the correct provider for each attempt:
        attempt 0 = 'failing', attempt 1 = 'succeeding'.
    """
    cfg = load_config(_two_provider_cfg(tmp_path, "sh", ["-c", "exit 1"]))
    store = Store.open()

    r = enqueue_job(cfg, store, EnqueueRequest(project="p", role="r", kind="test", prompt="hello"))
    job_id = r.job_id

    # Initial provider should be 'failing' (no failures yet, primary is chosen)
    assert store.get_job(job_id)["provider"] == "failing"

    # run_pending_once loops until the queue is empty:
    #   iteration 1: claim 'failing' job → fails → requeue with 'succeeding'
    #   iteration 2: claim 'succeeding' job → succeeds → done
    #   iteration 3: no more claimable jobs → exit
    run_pending_once(cfg, store, "w1", 3600)

    job = store.get_job(job_id)
    assert job["status"] == "done", f"Expected 'done', got {job['status']!r}"

    # Verify attempts table records correct providers.
    attempts = store.attempts_for_job(job_id)
    assert len(attempts) == 2, f"Expected 2 attempts, got {len(attempts)}"
    providers_in_attempts = [a["provider"] for a in attempts]
    assert providers_in_attempts[0] == "failing", (
        f"Attempt 0 expected provider='failing', got {providers_in_attempts[0]!r}"
    )
    assert providers_in_attempts[1] == "succeeding", (
        f"Attempt 1 expected provider='succeeding', got {providers_in_attempts[1]!r}"
    )


# ---------------------------------------------------------------------------
# Test (b): single-provider role → retries until dead, provider never changes
# ---------------------------------------------------------------------------

def test_single_provider_retries_until_dead(herder_home, tmp_path):
    """Single-provider role: retries exhausted → dead. Provider never changes."""
    cfg = load_config(_single_provider_cfg(tmp_path, "sh", ["-c", "exit 1"]))
    store = Store.open()

    r = enqueue_job(cfg, store, EnqueueRequest(project="p", role="r", kind="test", prompt="x"))
    job_id = r.job_id
    max_retries = store.get_job(job_id)["max_retries"]

    # Worker runs until the job is dead (all retries exhausted).
    run_pending_once(cfg, store, "w1", 3600)

    job = store.get_job(job_id)
    assert job["status"] == "dead"
    assert job["attempts"] == max_retries
    # Provider must never have changed (single-provider role)
    assert job["provider"] == "failing"

    # Verify every attempt used the same provider.
    for attempt in store.attempts_for_job(job_id):
        assert attempt["provider"] == "failing", (
            f"Attempt {attempt['attempt_no']} used unexpected provider {attempt['provider']!r}"
        )


# ---------------------------------------------------------------------------
# Test (c): cooldown at enqueue — primary is seeded → enqueue resolves to secondary
# ---------------------------------------------------------------------------

def test_cooldown_at_enqueue_resolves_to_secondary(herder_home, tmp_path):
    """When primary exceeds allowed_fails at enqueue time, secondary is chosen directly."""
    cfg = load_config(_cooldown_enqueue_cfg(tmp_path))
    store = Store.open()

    # Seed a job so we can attach failure attempts to "primary"
    store.enqueue(
        id="seed_job",
        kind="test",
        role="r",
        provider="primary",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/x/p.md",
        prompt_hash="seed_hash",
        run_dir="/tmp/x",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    # 3 failures within window = allowed_fails → primary is cooling
    for i in range(1, 4):
        store.record_attempt(
            job_id="seed_job",
            attempt_no=i,
            worker_id="w",
            exit_code=1,
            status="failed",
            provider="primary",
            finished_at=now_iso,
        )

    # Enqueue a new job — resolve should pick 'secondary' directly.
    r = enqueue_job(cfg, store, EnqueueRequest(project="p", role="r", kind="test", prompt="y"))
    job = store.get_job(r.job_id)
    assert job["provider"] == "secondary", (
        f"Expected 'secondary' at enqueue due to cooldown, got {job['provider']!r}"
    )

    # Worker runs and job succeeds (secondary = cat).
    run_pending_once(cfg, store, "w1", 3600)
    assert store.get_job(r.job_id)["status"] == "done"
