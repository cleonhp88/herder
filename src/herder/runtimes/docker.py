"""DockerRuntime — container backend for Herder.

Runs jobs inside a Docker container via ``docker run``.  Cancellation is
implemented by ``docker stop`` (graceful) followed by ``docker kill``
(forceful), replacing the default process-group SIGTERM strategy.

Pure function ``build_docker_argv`` is module-level so it can be unit-tested
without spawning any process.

CRITICAL: Never use shell=True. argv-only. No string interpolation into a
shell command — env vars are passed as ``-e KEY=VALUE`` flags.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from herder.models import Result
from herder.runtimes.base import TerminateFn, run_with_terminate


def build_docker_argv(
    *,
    inner_argv: list[str],
    image: str,
    network: str,
    cwd: Path,
    env: dict[str, str],
    name: str,
    extra_args: list[str],
) -> list[str]:
    """Build the ``docker run`` argv list for a job.

    All values are passed as discrete argv tokens — no shell interpolation.
    Environment variables are passed as ``-e KEY=VALUE`` flags so they are
    never expanded by a shell.

    Args:
        inner_argv: The command and arguments to run inside the container.
        image: Docker image reference (e.g. "alpine", "myorg/tool:1.2").
        network: Docker network mode — "none", "bridge", or "host".
        cwd: Host working directory; mounted at the same path inside the
             container and set as the working directory.
        env: Environment variables to forward into the container.
        name: Stable container name used for ``docker stop`` cancellation.
        extra_args: Additional ``docker run`` flags (e.g. ``["--cpus", "2"]``).

    Returns:
        Complete argv list starting with "docker".

    Example:
        >>> build_docker_argv(inner_argv=["echo", "hi"], image="alpine",
        ...     network="none", cwd=Path("/work"), env={}, name="j1",
        ...     extra_args=[])
        ['docker', 'run', '--rm', '-i', '--name', 'j1', '--network', 'none',
         '-v', '/work:/work', '-w', '/work', 'alpine', 'echo', 'hi']
    """
    cwd_str = str(cwd)
    env_flags: list[str] = [
        flag
        for k, v in env.items()
        for flag in ("-e", f"{k}={v}")
    ]
    return [
        "docker", "run",
        "--rm",
        "-i",
        "--name", name,
        "--network", network,
        "-v", f"{cwd_str}:{cwd_str}",
        "-w", cwd_str,
        *extra_args,
        *env_flags,
        image,
        *inner_argv,
    ]


def _sanitise(segment: str) -> str:
    """Sanitise a string to Docker-compatible name characters.

    Docker names allow ``[a-zA-Z0-9_.-]``; all other characters are replaced
    with ``-``.

    Args:
        segment: Raw string to sanitise.

    Returns:
        Sanitised string safe for use in a Docker container name.
    """
    return "".join(c if c.isalnum() or c in "_.-" else "-" for c in segment)


def _derive_container_name(stdout_path: Path | None) -> str:
    """Derive a collision-safe per-attempt container name.

    Combines the unique run directory name with the attempt stem so that
    concurrent jobs at the same attempt number but different run directories
    (e.g. ``run-AAAA/stdout.1.log`` vs ``run-BBBB/stdout.1.log``) produce
    distinct names — avoiding ``docker run --name`` conflicts under the
    multi-worker pool.

    Real call site (supervisor.py:56):
        ``run_dir / f"stdout.{attempt_no}.log"``
    so ``stdout_path.parent.name`` is the unique per-job run directory name and
    ``stdout_path.stem`` is ``"stdout.N"``.  Both segments are sanitised.

    Args:
        stdout_path: Optional file path; parent.name provides the unique run
                     dir identifier, stem provides the attempt qualifier.

    Returns:
        A Docker-compatible container name guaranteed unique per run directory
        and attempt number.  Falls back to a UUID when no path is provided.
    """
    if stdout_path is not None:
        parent_part = _sanitise(stdout_path.parent.name)
        stem_part = _sanitise(stdout_path.stem)
        return f"herder-{parent_part}-{stem_part}"

    return f"herder-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class DockerRuntime:
    """Runtime that executes jobs inside a Docker container.

    Cancellation is implemented via ``docker stop`` (graceful, 5-second
    timeout) followed by ``docker kill`` (SIGKILL).  The container name is
    derived from the stdout path parent directory and stem to ensure a
    collision-safe, per-attempt identifier even under concurrent workers.

    Attributes:
        name: Runtime identifier used in resolution and logging.
        image: Docker image to run (e.g. "herder-sandbox:latest").
        network: Docker network mode (default "none" for isolation).
        extra_args: Additional flags forwarded to ``docker run``.
    """

    name: str
    image: str
    network: str = "none"
    extra_args: list[str] = field(default_factory=list)

    def _make_terminate(self, container_name: str) -> TerminateFn:
        """Return a terminate callable that stops then kills the container.

        The returned closure runs ``docker stop`` (5-second grace) then
        ``docker kill`` — errors from either command are suppressed because
        the container may have already exited.

        Args:
            container_name: The ``--name`` value passed to ``docker run``.

        Returns:
            A callable accepting a Popen instance that triggers docker cleanup.
        """
        def _terminate(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
            subprocess.run(
                ["docker", "stop", "--time", "5", container_name],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["docker", "kill", container_name],
                check=False,
                capture_output=True,
            )

        return _terminate

    def run(
        self,
        argv: list[str],
        *,
        prompt: str,
        cwd: Path,
        timeout: int,
        env: dict[str, str],
        stdout_path: Path | None,
        stderr_path: Path | None,
        cancel_check: Callable[[], bool] | None,
        heartbeat: Callable[[], None] | None,
        heartbeat_interval: float,
        sandbox_profile: str | None,  # noqa: ARG002 — Docker self-confines; unused
        perms: object,  # noqa: ARG002 — guards live in SSHRuntime; unused here
        secret_keys: list[str] | None = None,  # noqa: ARG002 — guards live in SSHRuntime
    ) -> Result:
        """Execute argv inside a Docker container and return a classified Result.

        Builds a collision-safe container name from the stdout path (combining
        the unique run directory name and attempt stem), constructs the
        ``docker run`` argv, and delegates to the shared poll/kill core with a
        docker-specific terminate strategy.

        When ``stdout_path`` or ``stderr_path`` is None, a ``TemporaryDirectory``
        is created for the run lifetime and cleaned up on return — matching
        ``LocalRuntime`` parity and preventing log file leaks.

        Args:
            argv: Inner command and arguments to run inside the container.
            prompt: Text passed to the container via stdin.
            cwd: Working directory; mounted at the same path inside container.
            timeout: Wall-clock seconds before timeout outcome.
            env: Environment variables forwarded as ``-e`` flags.
            stdout_path: File path for stdout capture (None → temp dir).
            stderr_path: File path for stderr capture (None → temp dir).
            cancel_check: Returns True when the job is cancelled externally.
            heartbeat: Optional callable to renew the job lease periodically.
            heartbeat_interval: Seconds between heartbeat calls.
            sandbox_profile: Unused — Docker provides its own confinement.
            perms: Unused — security guards live in SSHRuntime.

        Returns:
            Result classified as done/failed/timeout/cancelled/unavailable.
        """
        # Compute container name once here so terminate closure is stable.
        container_name = _derive_container_name(stdout_path)

        docker_argv = build_docker_argv(
            inner_argv=argv,
            image=self.image,
            network=self.network,
            cwd=cwd,
            env=env,
            name=container_name,
            extra_args=list(self.extra_args),
        )

        effective_cancel = cancel_check if cancel_check is not None else (lambda: False)

        # When paths are None, use a TemporaryDirectory scoped to this run so
        # log files are cleaned up on return (prevents indefinite leaks).
        if stdout_path is not None and stderr_path is not None:
            return run_with_terminate(
                docker_argv,
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                env={},  # env forwarded via -e flags in docker_argv
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cancel_check=effective_cancel,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
                terminate=self._make_terminate(container_name),
            )

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            return run_with_terminate(
                docker_argv,
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                env={},  # env forwarded via -e flags in docker_argv
                stdout_path=tmp / "docker_stdout.log",
                stderr_path=tmp / "docker_stderr.log",
                cancel_check=effective_cancel,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
                terminate=self._make_terminate(container_name),
            )
