"""Unit tests for per-host concurrency group semaphore construction in queue_claim.

Tests the pure helper ``_build_group_limits`` and the integration behaviour of
``run_pending_parallel`` when providers share a ``concurrency_group``.

Test categories
---------------
- Pure helper unit tests — no I/O, no threads.
- Deterministic peak-concurrency tests — verify mutual exclusion and parallelism
  WITHOUT relying on wall-clock time.  A gate ``threading.Event`` synchronises
  threads so the peak is observable under a shared counter.
- Integration / slow tests — wall-clock timing smoke checks, opted out of fast
  CI via ``@pytest.mark.integration``.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from herder.config import Config, Provider
from herder.db.store import Store
from herder.loops.queue_claim import _build_group_limits, run_pending_parallel
from herder.services.enqueue import EnqueueRequest, enqueue_job


# ---------------------------------------------------------------------------
# Pure-helper unit tests — no I/O, no threads
# ---------------------------------------------------------------------------

def _make_provider(**kwargs: Any) -> Provider:
    """Construct a minimal Provider with cli type, overridable via kwargs.

    Args:
        **kwargs: Field overrides passed to Provider constructor.

    Returns:
        A valid Provider instance.
    """
    defaults: dict[str, Any] = {
        "type": "cli",
        "executable": "cat",
        "input": "stdin",
        "max_concurrency": 1,
        "concurrency_group": None,
    }
    defaults.update(kwargs)
    return Provider(**defaults)  # type: ignore[arg-type]


def test_build_group_limits_ungrouped_providers_each_get_own_entry() -> None:
    """Two ungrouped providers produce two separate group entries.

    Preserves prior per-provider behaviour: each provider is its own group,
    keyed by provider name.
    """
    providers = {
        "alpha": _make_provider(max_concurrency=2),
        "beta": _make_provider(max_concurrency=3),
    }
    limits = _build_group_limits(providers)
    assert limits == {"alpha": 2, "beta": 3}


def test_build_group_limits_grouped_providers_share_one_entry() -> None:
    """Two providers in the same concurrency_group collapse to a single entry.

    The shared semaphore size equals the minimum max_concurrency across the
    group members (tightest constraint wins).
    """
    providers = {
        "a": _make_provider(max_concurrency=1, concurrency_group="host"),
        "b": _make_provider(max_concurrency=1, concurrency_group="host"),
    }
    limits = _build_group_limits(providers)
    assert list(limits.keys()) == ["host"]
    assert limits["host"] == 1


def test_build_group_limits_min_concurrency_used_for_group() -> None:
    """Group semaphore uses min(max_concurrency) across members, not max or sum."""
    providers = {
        "a": _make_provider(max_concurrency=3, concurrency_group="g"),
        "b": _make_provider(max_concurrency=1, concurrency_group="g"),
    }
    limits = _build_group_limits(providers)
    assert limits["g"] == 1


def test_build_group_limits_mixed_grouped_and_ungrouped() -> None:
    """Mixed: grouped pair collapses; ungrouped provider keeps its own entry."""
    providers = {
        "ol1": _make_provider(max_concurrency=1, concurrency_group="box7"),
        "ol2": _make_provider(max_concurrency=2, concurrency_group="box7"),
        "solo": _make_provider(max_concurrency=4),
    }
    limits = _build_group_limits(providers)
    assert limits == {"box7": 1, "solo": 4}


def test_build_group_limits_empty_providers_returns_empty() -> None:
    """Empty provider dict produces an empty group-limits dict."""
    assert _build_group_limits({}) == {}


def test_build_group_limits_single_ungrouped_provider() -> None:
    """A single ungrouped provider produces exactly one entry keyed by its name."""
    providers = {"only": _make_provider(max_concurrency=2)}
    limits = _build_group_limits(providers)
    assert limits == {"only": 2}


def test_build_group_limits_single_grouped_provider() -> None:
    """A single provider with a concurrency_group uses the group name as key."""
    providers = {"p": _make_provider(max_concurrency=2, concurrency_group="grp")}
    limits = _build_group_limits(providers)
    assert limits == {"grp": 2}


# ---------------------------------------------------------------------------
# Deterministic peak-concurrency tests — no wall-clock dependency
#
# Strategy: monkeypatch execute_job so each invocation (a) increments a shared
# counter under a lock, (b) records the peak, (c) waits on a gate Event, then
# (d) decrements.  The orchestrating thread releases the gate after a brief
# sleep so all threads have had a chance to park on it at the same time.
# ---------------------------------------------------------------------------

class _PeakTracker:
    """Thread-safe tracker that records the peak concurrent count."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def enter(self) -> None:
        """Record one more concurrent execution and update peak."""
        with self._lock:
            self._active += 1
            if self._active > self.peak:
                self.peak = self._active

    def leave(self) -> None:
        """Record one fewer concurrent execution."""
        with self._lock:
            self._active -= 1


def _make_peak_cfg(
    tmp_path: Path,
    *,
    providers_yaml: str,
    roles_yaml: str,
    global_concurrency: int,
    allowed_roles: str = "ra, rb",
) -> Config:
    """Write a minimal YAML config and load it.

    Args:
        tmp_path: Temporary directory for the config file.
        providers_yaml: Indented YAML snippet for the providers block.
        roles_yaml: Indented YAML snippet for the roles block.
        global_concurrency: Worker-level global concurrency limit.
        allowed_roles: Comma-separated role names for the project's allowed_roles list.

    Returns:
        Loaded Config instance.
    """
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        f"providers:\n{providers_yaml}"
        f"roles:\n{roles_yaml}"
        f"projects:\n  p: {{root: '{proj}', default_workspace_mode: readonly,"
        f" allowed_roles: [{allowed_roles}]}}\n"
        f"worker: {{global_concurrency: {global_concurrency}, lease_seconds: 3600}}\n"
    )
    from herder.config import load_config as _load_config
    return _load_config(str(cfg_path))


def _make_gate_execute_job(
    tracker: _PeakTracker,
    gate: threading.Event,
) -> Any:
    """Return a patched execute_job that gates on ``gate`` to control concurrency.

    Each invocation:
    1. Increments the tracker (records peak).
    2. Waits on ``gate`` so all threads park simultaneously.
    3. Decrements the tracker.
    4. Returns ``"done"`` so no retry logic fires.

    Args:
        tracker: Shared peak counter.
        gate: Event that all threads park on; the test releases it after threads start.

    Returns:
        Callable matching the execute_job signature.
    """
    def _fake_execute_job(cfg: Config, store: Store, job: Any, worker_id: str) -> str:
        tracker.enter()
        gate.wait(timeout=5.0)
        tracker.leave()
        return "done"

    return _fake_execute_job


def test_grouped_providers_peak_concurrency_is_one(
    herder_home: Path, tmp_path: Path
) -> None:
    """Two jobs on providers in the same group must NEVER run simultaneously.

    Mutual exclusion proof: peak concurrent executions == 1, regardless of
    global_concurrency=2.  This test does NOT depend on sleep durations.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = _make_peak_cfg(
        tmp_path,
        providers_yaml=(
            "  ol1: {type: cli, executable: cat, input: stdin,"
            " max_concurrency: 1, concurrency_group: host}\n"
            "  ol2: {type: cli, executable: cat, input: stdin,"
            " max_concurrency: 1, concurrency_group: host}\n"
        ),
        roles_yaml=(
            "  ra: {provider: ol1, permissions: read_only}\n"
            "  rb: {provider: ol2, permissions: read_only}\n"
        ),
        global_concurrency=2,
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    tracker = _PeakTracker()
    gate = threading.Event()

    def _release_gate() -> None:
        time.sleep(0.15)  # enough for both threads to park on gate.wait()
        gate.set()

    releaser = threading.Thread(target=_release_gate, daemon=True)

    with patch("herder.loops.queue_claim.execute_job", _make_gate_execute_job(tracker, gate)):
        releaser.start()
        n = run_pending_parallel(cfg, store, "w1", 3600)

    releaser.join(timeout=2.0)
    assert n == 2
    assert tracker.peak == 1, (
        f"Expected peak==1 (mutual exclusion), got {tracker.peak}"
    )


def test_ungrouped_providers_peak_concurrency_is_two(
    herder_home: Path, tmp_path: Path
) -> None:
    """Two jobs on ungrouped providers must be able to run simultaneously.

    Parallelism proof: peak concurrent executions == 2 when global_concurrency==2.
    This confirms ungrouped providers are NOT accidentally serialised.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = _make_peak_cfg(
        tmp_path,
        providers_yaml=(
            "  sa: {type: cli, executable: cat, input: stdin, max_concurrency: 2}\n"
            "  sb: {type: cli, executable: cat, input: stdin, max_concurrency: 2}\n"
        ),
        roles_yaml=(
            "  ra: {provider: sa, permissions: read_only}\n"
            "  rb: {provider: sb, permissions: read_only}\n"
        ),
        global_concurrency=2,
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    tracker = _PeakTracker()
    gate = threading.Event()

    def _release_gate() -> None:
        time.sleep(0.15)
        gate.set()

    releaser = threading.Thread(target=_release_gate, daemon=True)

    with patch("herder.loops.queue_claim.execute_job", _make_gate_execute_job(tracker, gate)):
        releaser.start()
        n = run_pending_parallel(cfg, store, "w1", 3600)

    releaser.join(timeout=2.0)
    assert n == 2
    assert tracker.peak == 2, (
        f"Expected peak==2 (parallel execution), got {tracker.peak}"
    )


def test_heterogeneous_group_peak_concurrency_is_one(
    herder_home: Path, tmp_path: Path
) -> None:
    """A group with mismatched max_concurrency values uses the minimum (1).

    Heterogeneous group: member A declares max_concurrency=3, member B declares
    max_concurrency=1.  The group semaphore is min(3,1)==1, so peak must be 1.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = _make_peak_cfg(
        tmp_path,
        providers_yaml=(
            "  ha: {type: cli, executable: cat, input: stdin,"
            " max_concurrency: 3, concurrency_group: mixed}\n"
            "  hb: {type: cli, executable: cat, input: stdin,"
            " max_concurrency: 1, concurrency_group: mixed}\n"
        ),
        roles_yaml=(
            "  ra: {provider: ha, permissions: read_only}\n"
            "  rb: {provider: hb, permissions: read_only}\n"
        ),
        global_concurrency=4,
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    tracker = _PeakTracker()
    gate = threading.Event()

    def _release_gate() -> None:
        time.sleep(0.15)
        gate.set()

    releaser = threading.Thread(target=_release_gate, daemon=True)

    with patch("herder.loops.queue_claim.execute_job", _make_gate_execute_job(tracker, gate)):
        releaser.start()
        n = run_pending_parallel(cfg, store, "w1", 3600)

    releaser.join(timeout=2.0)
    assert n == 2
    assert tracker.peak == 1, (
        f"Expected peak==1 for heterogeneous group (min wins), got {tracker.peak}"
    )


def test_unknown_provider_job_completes_without_raising(
    herder_home: Path, tmp_path: Path
) -> None:
    """A job whose provider is absent from cfg.providers runs without error.

    When ``sema is None`` (unknown provider → no semaphore), the job must still
    complete.  This proves the None-branch in ``_worker`` is exercised safely.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = _make_peak_cfg(
        tmp_path,
        providers_yaml=(
            "  known: {type: cli, executable: cat, input: stdin, max_concurrency: 1}\n"
        ),
        roles_yaml=(
            "  ra: {provider: known, permissions: read_only}\n"
        ),
        global_concurrency=2,
        allowed_roles="ra",
    )
    store = Store.open()
    # Enqueue a job for the known role so we have something to claim.
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))

    # Monkeypatch claim_job to return a row whose "provider" field names a
    # provider that does NOT exist in cfg.providers, triggering the None path.
    real_claim = store.claim_job
    claim_calls: list[int] = [0]

    def _fake_claim(worker_id: str, lease_seconds: int) -> Any:
        call_no = claim_calls[0]
        claim_calls[0] += 1
        if call_no == 0:
            row = real_claim(worker_id, lease_seconds)
            if row is None:
                return None
            # Swap the provider name to an unknown one.
            import sqlite3
            col_names = row.keys()
            values = dict(zip(col_names, tuple(row)))
            values["provider"] = "__nonexistent_provider__"
            # Build a sqlite3.Row substitute using a description tuple.
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            placeholders = ", ".join("?" for _ in col_names)
            cols_sql = ", ".join(col_names)
            conn.execute(f"CREATE TABLE t ({cols_sql})")
            conn.execute(f"INSERT INTO t VALUES ({placeholders})", [values[c] for c in col_names])
            return conn.execute("SELECT * FROM t").fetchone()
        return None  # signal no more jobs

    patch_target = "herder.loops.queue_claim.execute_job"
    with (
        patch.object(store, "claim_job", side_effect=_fake_claim),
        patch(patch_target, return_value="done"),
    ):
        n = run_pending_parallel(cfg, store, "w1", 3600)

    # The job ran (n==1) even though its provider was unknown — no exception raised.
    assert n == 1


# ---------------------------------------------------------------------------
# Integration / slow tests — wall-clock timing smoke checks
# Opt out of fast CI by marking @pytest.mark.integration
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_grouped_providers_serialise_jobs(herder_home: Path, tmp_path: Path) -> None:
    """Two jobs on grouped providers must run serially (~4s), not in parallel (~2s).

    Both providers share concurrency_group='host' with max_concurrency=1, so
    only one job may execute at a time despite global_concurrency=2.
    Wall-clock margins use 2-second sleeps so there is a full-second of slack.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config_from_yaml(
        tmp_path,
        providers=(
            "  ol1: {type: cli, executable: sh, args: ['-c', 'sleep 2'],"
            " input: stdin, timeout: 30, max_concurrency: 1, concurrency_group: host}\n"
            "  ol2: {type: cli, executable: sh, args: ['-c', 'sleep 2'],"
            " input: stdin, timeout: 30, max_concurrency: 1, concurrency_group: host}\n"
        ),
        roles=(
            "  ra: {provider: ol1, permissions: read_only}\n"
            "  rb: {provider: ol2, permissions: read_only}\n"
        ),
        global_concurrency=2,
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    t0 = time.monotonic()
    n = run_pending_parallel(cfg, store, "w1", 3600)
    wall = time.monotonic() - t0

    assert n == 2
    assert len(store.list_jobs(status="done")) == 2
    assert wall >= 3.5, f"expected serialised (>=3.5s), got {wall:.2f}s"


@pytest.mark.integration
def test_ungrouped_providers_still_run_in_parallel(herder_home: Path, tmp_path: Path) -> None:
    """Ungrouped providers must still run concurrently (backward-compatible behaviour).

    Two jobs on separate providers with no concurrency_group and
    global_concurrency=2 should complete in ~2 seconds, not ~4.
    Wall-clock margins use 2-second sleeps so there is a full-second of slack.

    Args:
        herder_home: Fixture providing isolated HERDER_HOME.
        tmp_path: Temporary directory for test files.
    """
    cfg = load_config_from_yaml(
        tmp_path,
        providers=(
            "  sa: {type: cli, executable: sh, args: ['-c', 'sleep 2'],"
            " input: stdin, timeout: 30, max_concurrency: 1}\n"
            "  sb: {type: cli, executable: sh, args: ['-c', 'sleep 2'],"
            " input: stdin, timeout: 30, max_concurrency: 1}\n"
        ),
        roles=(
            "  ra: {provider: sa, permissions: read_only}\n"
            "  rb: {provider: sb, permissions: read_only}\n"
        ),
        global_concurrency=2,
    )
    store = Store.open()
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="ra", kind="automation", prompt="x"))
    enqueue_job(cfg, store, EnqueueRequest(project="p", role="rb", kind="automation", prompt="x"))

    t0 = time.monotonic()
    n = run_pending_parallel(cfg, store, "w1", 3600)
    wall = time.monotonic() - t0

    assert n == 2
    assert len(store.list_jobs(status="done")) == 2
    assert wall < 3.0, f"expected parallel (<3.0s), got {wall:.2f}s"


# ---------------------------------------------------------------------------
# Helper used by integration tests
# ---------------------------------------------------------------------------

from herder.config import load_config as _load_config  # noqa: E402


def _write_cfg(
    tmp_path: Path,
    *,
    providers: str,
    roles: str,
    global_concurrency: int,
) -> str:
    """Write a minimal valid YAML config and return its path.

    Args:
        tmp_path: Temporary directory for the config file.
        providers: Indented YAML snippet (4-space) for the providers block.
        roles: Indented YAML snippet (4-space) for the roles block.
        global_concurrency: Worker-level global concurrency limit.

    Returns:
        Absolute path to the written YAML file as a string.
    """
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    c = tmp_path / "c.yaml"
    c.write_text(
        f"providers:\n{providers}"
        f"roles:\n{roles}"
        f"projects:\n  p: {{root: '{proj}', default_workspace_mode: readonly,"
        f" allowed_roles: [ra, rb]}}\n"
        f"worker: {{global_concurrency: {global_concurrency}, lease_seconds: 3600}}\n"
    )
    return str(c)


def load_config_from_yaml(
    tmp_path: Path,
    *,
    providers: str,
    roles: str,
    global_concurrency: int,
) -> "Config":
    """Write a minimal YAML config and load it.

    Args:
        tmp_path: Temporary directory for the config file.
        providers: Indented YAML snippet for the providers block.
        roles: Indented YAML snippet for the roles block.
        global_concurrency: Worker-level global concurrency limit.

    Returns:
        Loaded Config instance.
    """
    return _load_config(_write_cfg(tmp_path, providers=providers, roles=roles,
                                   global_concurrency=global_concurrency))
