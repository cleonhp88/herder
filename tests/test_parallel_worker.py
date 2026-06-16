"""Tests for the parallel worker pass with per-provider concurrency."""
import time
from pathlib import Path

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_parallel
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path: Path, *, providers: str, roles: str, global_concurrency: int) -> str:
    """Create a test config with custom providers and roles.

    Args:
        tmp_path: Temporary directory for the config file.
        providers: YAML snippet for providers.
        roles: YAML snippet for roles.
        global_concurrency: Worker global concurrency limit.

    Returns:
        Path to the created config file as a string.
    """
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"
    c.write_text(
        f"providers:\n{providers}"
        f"roles:\n{roles}"
        f"projects:\n  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [ra, rb]}}\n"
        f"worker: {{global_concurrency: {global_concurrency}, lease_seconds: 3600}}\n"
    )
    return str(c)


def test_different_providers_run_in_parallel(herder_home: Path, tmp_path: Path) -> None:
    """Verify that jobs on different providers run in parallel.

    Two jobs on different providers with max_concurrency: 1 each and
    global_concurrency: 2 should complete in ~1 second (parallel), not ~2 (serial).

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(
        _cfg(
            tmp_path,
            providers=(
                "  sa: {type: cli, executable: sh, args: ['-c', 'sleep 1'], input: stdin, timeout: 30, max_concurrency: 1}\n"
                "  sb: {type: cli, executable: sh, args: ['-c', 'sleep 1'], input: stdin, timeout: 30, max_concurrency: 1}\n"
            ),
            roles=(
                "  ra: {provider: sa, permissions: read_only}\n"
                "  rb: {provider: sb, permissions: read_only}\n"
            ),
            global_concurrency=2,
        )
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    t0 = time.monotonic()
    n = run_pending_parallel(cfg, store, "w1", 3600)
    wall = time.monotonic() - t0

    assert n == 2
    assert len(store.list_jobs(status="done")) == 2
    assert wall < 1.9, f"expected parallel (<1.9s), got {wall:.2f}s"


def test_same_provider_max_concurrency_serializes(herder_home: Path, tmp_path: Path) -> None:
    """Verify that jobs on the same provider respect max_concurrency: 1.

    Two jobs on the same provider with max_concurrency: 1 and
    global_concurrency: 2 should complete in ~2 seconds (serial), not ~1 (parallel).

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(
        _cfg(
            tmp_path,
            providers="  sa: {type: cli, executable: sh, args: ['-c', 'sleep 1'], input: stdin, timeout: 30, max_concurrency: 1}\n",
            roles=(
                "  ra: {provider: sa, permissions: read_only}\n"
                "  rb: {provider: sa, permissions: read_only}\n"
            ),
            global_concurrency=2,
        )
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    t0 = time.monotonic()
    n = run_pending_parallel(cfg, store, "w1", 3600)
    wall = time.monotonic() - t0

    assert n == 2
    assert len(store.list_jobs(status="done")) == 2
    assert wall >= 2.0, f"expected serialized (≥2s), got {wall:.2f}s"


def test_parallel_empty_queue(herder_home: Path, tmp_path: Path) -> None:
    """Verify that parallel pass returns 0 when queue is empty.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config(
        _cfg(
            tmp_path,
            providers="  sa: {type: cli, executable: cat, args: [], input: stdin, timeout: 10, max_concurrency: 1}\n",
            roles="  ra: {provider: sa, permissions: read_only}\n  rb: {provider: sa, permissions: read_only}\n",
            global_concurrency=2,
        )
    )
    assert run_pending_parallel(cfg, Store.open(), "w1", 3600) == 0
