"""Tests for the fcntl.flock-based single-flight eval lock in evals/run_pilot.py.

Tests cover flock semantics:
- Happy path: exclusion proven with a raw fd; released on context exit.
- Contention raises EvalLockError (tested in-process via two open-file descriptions).
- Holder PID appears in the error message (or is None if unreadable).
- Cross-process auto-release: kernel releases flock on process death — no stale
  reclaim needed.

The ``herder_home`` fixture (defined in conftest.py) sets HERDER_HOME to a
tmp dir so the lock never touches ~/.herder.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make sure evals/ is importable when tests are run from the project root.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evals.run_pilot import EvalLockError, _single_flight_lock  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lock_path() -> Path:
    """Return the eval lock path using the currently configured herder home."""
    from herder.paths import home as _herder_home

    return _herder_home() / "eval.lock"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """flock acquired and released correctly under normal (no contention) conditions."""

    def test_exclusion_proven_with_raw_fd(self, herder_home: Path) -> None:
        """Inside the context, a second raw flock on the same file is blocked.

        After the context exits the same raw flock SUCCEEDS, proving release.
        """
        lock = _lock_path()

        with _single_flight_lock():
            # Lock file must exist (flock uses an open fd on the file).
            assert lock.exists(), "lock file must exist inside the context"

            # A second open-file description on the same file cannot flock LOCK_EX.
            fd2 = os.open(str(lock), os.O_RDWR, 0o644)
            try:
                with pytest.raises((BlockingIOError, OSError)):
                    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd2)

        # After the context exits the flock is released — a fresh fd can acquire.
        fd3 = os.open(str(lock), os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd3, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(fd3, fcntl.LOCK_UN)
        finally:
            os.close(fd3)

    def test_pid_written_for_diagnostics(self, herder_home: Path) -> None:
        """Current pid is written into the lock file while the context is held."""
        lock = _lock_path()

        with _single_flight_lock():
            content = lock.read_text(encoding="utf-8").strip()
            assert content == str(os.getpid()), (
                f"expected pid={os.getpid()}, got {content!r}"
            )

    def test_lock_file_persists_on_clean_exit(self, herder_home: Path) -> None:
        """The lock file is intentionally NOT removed on exit (persistent inode).

        The flock is released but the file remains; this is the correct flock
        pattern — unlinking would introduce a fresh-inode race.
        """
        lock = _lock_path()

        with _single_flight_lock():
            pass

        # File is still on disk (no unlink) — a raw flock on it now succeeds.
        assert lock.exists(), (
            "lock file must persist after context exit (flock never unlinks)"
        )
        fd = os.open(str(lock), os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def test_flock_released_on_exception(self, herder_home: Path) -> None:
        """The flock is released even when an exception propagates out."""
        lock = _lock_path()

        with pytest.raises(ValueError):
            with _single_flight_lock():
                raise ValueError("boom")

        # Flock released — a raw acquire must succeed.
        fd = os.open(str(lock), os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class TestContention:
    """EvalLockError is raised when another open-file description holds the flock."""

    def test_contention_raises_eval_lock_error(self, herder_home: Path) -> None:
        """Hold a raw LOCK_EX flock on the lock file; entering _single_flight_lock
        raises EvalLockError.

        flock is per open-file description, so two fds in the same process DO
        exclude each other — no subprocess needed.
        """
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)

        # Pre-create the file so our raw fd can open it.
        lock.touch()

        fd_raw = os.open(str(lock), os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd_raw, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with pytest.raises(EvalLockError):
                with _single_flight_lock():
                    pass  # must not reach here
        finally:
            fcntl.flock(fd_raw, fcntl.LOCK_UN)
            os.close(fd_raw)

    def test_error_message_is_non_empty(self, herder_home: Path) -> None:
        """EvalLockError carries a human-readable message."""
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()

        fd_raw = os.open(str(lock), os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd_raw, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with pytest.raises(EvalLockError) as exc_info:
                with _single_flight_lock():
                    pass

            assert str(exc_info.value), "error message must not be empty"
        finally:
            fcntl.flock(fd_raw, fcntl.LOCK_UN)
            os.close(fd_raw)


class TestHolderPidInError:
    """holder_pid in EvalLockError reflects the pid written by the holder."""

    def test_pid_in_error_when_holder_writes_pid(self, herder_home: Path) -> None:
        """When the raw holder writes its pid first, EvalLockError.holder_pid
        reflects that pid (or is None if unreadable — either is acceptable).

        Per spec: assert holder_pid is either the written pid or None, and the
        message is non-empty.
        """
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)

        written_pid = 12345  # arbitrary synthetic pid

        fd_raw = os.open(
            str(lock), os.O_CREAT | os.O_RDWR, 0o644
        )
        try:
            fcntl.flock(fd_raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Holder writes its pid for diagnostics.
            os.ftruncate(fd_raw, 0)
            os.lseek(fd_raw, 0, os.SEEK_SET)
            os.write(fd_raw, f"{written_pid}\n".encode())

            with pytest.raises(EvalLockError) as exc_info:
                with _single_flight_lock():
                    pass

            err = exc_info.value
            assert err.holder_pid in (written_pid, None), (
                f"holder_pid must be {written_pid} or None, got {err.holder_pid!r}"
            )
            assert str(err), "error message must not be empty"
            assert err.lock_path == lock
        finally:
            fcntl.flock(fd_raw, fcntl.LOCK_UN)
            os.close(fd_raw)

    def test_holder_pid_none_when_file_empty(self, herder_home: Path) -> None:
        """When the lock file is empty, holder_pid is None (graceful fallback)."""
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)

        fd_raw = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd_raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Leave file empty — no pid written.

            with pytest.raises(EvalLockError) as exc_info:
                with _single_flight_lock():
                    pass

            err = exc_info.value
            assert err.holder_pid is None
            assert str(err), "error message must not be empty even with no pid"
        finally:
            fcntl.flock(fd_raw, fcntl.LOCK_UN)
            os.close(fd_raw)


class TestCrossProcessAutoRelease:
    """Kernel releases the flock on process death — the regression test.

    The old pid-file approach required stale-lock reclaim; flock makes it
    unnecessary.  This test proves the kernel auto-release property.
    """

    # Child script: open the lock file, flock it, write pid, signal READY, sleep.
    _CHILD_SCRIPT = textwrap.dedent(
        """\
        import fcntl, os, sys, time
        lock_path = sys.argv[1]
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (str(os.getpid()) + "\\n").encode())
        sys.stdout.write("READY\\n")
        sys.stdout.flush()
        # Sleep long enough for the parent to do its checks.
        time.sleep(60)
        """
    )

    def test_auto_release_on_process_death(self, herder_home: Path) -> None:
        """After killing the child holder, _single_flight_lock acquires successfully.

        Steps:
        1. Spawn a child that flocks the lock file and signals READY.
        2. While child holds it, assert EvalLockError is raised.
        3. Kill the child; kernel releases the flock.
        4. Assert _single_flight_lock now acquires.
        """
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HERDER_HOME"] = str(herder_home)

        child = subprocess.Popen(
            [sys.executable, "-c", self._CHILD_SCRIPT, str(lock)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        try:
            # Wait for child to signal READY (with timeout to avoid hanging).
            ready_line = b""
            deadline = time.monotonic() + 10.0  # 10-second timeout
            while time.monotonic() < deadline:
                assert child.poll() is None, (
                    f"child exited early with code {child.returncode}; "
                    f"stderr: {child.stderr.read().decode(errors='replace')!r}"
                )
                child.stdout.flush()
                # Non-blocking read via a short timeout on a thread.
                result: list[bytes] = []
                t = threading.Thread(
                    target=lambda: result.append(child.stdout.readline())
                )
                t.daemon = True
                t.start()
                t.join(timeout=1.0)
                if result and result[0].strip() == b"READY":
                    ready_line = result[0]
                    break

            assert ready_line.strip() == b"READY", (
                "child did not signal READY within 10 seconds"
            )

            # Child holds the lock — contention must be detected.
            with pytest.raises(EvalLockError):
                with _single_flight_lock():
                    pass

            # Kill child; kernel releases flock automatically.
            child.kill()
            child.wait()

            # Give the kernel a moment to process the fd close on macOS.
            time.sleep(0.05)

            # Now the lock must be acquirable.
            acquired = False
            with _single_flight_lock():
                acquired = True

            assert acquired, "_single_flight_lock must succeed after child death"

        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
