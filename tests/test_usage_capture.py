"""E2E test: attempt records timestamps and usage."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import EnqueueRequest, enqueue_job


def _cfg(tmp_path: Path) -> str:
    """Create minimal config for testing."""
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo_cli: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles:\n"
        "  planner: {provider: echo_cli, permissions: read_only}\n"
        f"projects:\n"
        f"  p: {{root: '{proj}', default_workspace_mode: readonly, allowed_roles: [planner]}}\n"
        "worker: {global_concurrency: 1, lease_seconds: 3600}\n"
    )
    return str(c)


class TestUsageCapture:
    """Attempt records timestamps and optional usage."""

    def test_attempt_records_timestamps(self, herder_home: Path, tmp_path: Path) -> None:
        """Completed job records started_at and finished_at."""
        cfg = load_config(_cfg(tmp_path))
        store = Store.open()

        # Enqueue a simple job
        r = enqueue_job(
            cfg,
            store,
            EnqueueRequest(project="p", role="planner", kind="research", prompt="test\n"),
        )

        # Run one pass
        run_pending_once(cfg, store, "w1", 3600)

        # Verify attempt exists and has timestamps
        rows = store.attempts_for_job(r.job_id)
        assert len(rows) == 1
        a = rows[0]

        # started_at and finished_at should be real ISO timestamps
        assert a["started_at"] is not None
        assert a["finished_at"] is not None

        # Both should be parseable as ISO (not None, not empty)
        from datetime import datetime, timezone

        started = datetime.fromisoformat(a["started_at"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(a["finished_at"].replace("Z", "+00:00"))
        assert finished >= started

        # Status should be done (cat echoes and exits 0)
        assert a["status"] == "done"

        # usage may be None for cat (no provider reports tokens)
        # but the column must exist and be accessible
        assert a["usage"] is None or isinstance(a["usage"], (str, type(None)))

    def test_all_attempts_query(self, herder_home: Path, tmp_path: Path) -> None:
        """Store.all_attempts() returns all attempts ordered by finished_at."""
        cfg = load_config(_cfg(tmp_path))
        store = Store.open()

        # Enqueue 2 jobs
        r1 = enqueue_job(
            cfg, store, EnqueueRequest(project="p", role="planner", kind="research", prompt="a\n")
        )
        r2 = enqueue_job(
            cfg, store, EnqueueRequest(project="p", role="planner", kind="research", prompt="b\n")
        )

        # Run both
        run_pending_once(cfg, store, "w1", 3600)

        # all_attempts should return both
        attempts = store.all_attempts()
        assert len(attempts) >= 2

        # Should be ordered by finished_at
        for i in range(len(attempts) - 1):
            curr = attempts[i]["finished_at"]
            next_ = attempts[i + 1]["finished_at"]
            if curr is not None and next_ is not None:
                assert curr <= next_
