"""SSHRuntime — remote-host backend for Herder.

Runs jobs on a remote host via SSH with ControlMaster multiplexing.
Cancellation closes the ControlMaster socket, which signals the remote
session to exit — preventing orphaned processes on the remote host.

Security (fail-closed, both guards fire BEFORE any subprocess):
  1. Untrusted jobs (network=False) are refused — a remote host cannot be
     seatbelt-confined; the error mirrors the existing "sandbox unavailable"
     guard pattern.
  2. Jobs that carry *secrets* (i.e. the resolved secret allow list is
     non-empty) are refused unless the runtime declares
     ``allow_remote_secrets: true``.  The guard checks the ``secret_keys``
     argument — the output of ``effective_allow_env(perms, provider_allow)``
     — NOT the full minimised env (which always contains base keys such as
     PATH/HOME that are not secrets).

Pure function ``build_ssh_argv`` is module-level for unit testing without
any SSH connection.

CRITICAL: Never use shell=True. The remote command is a single shell string
(SSH always runs a shell remotely) — every interpolated value MUST be
shlex-quoted to prevent injection.
"""
from __future__ import annotations

import shlex
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from herder.models import Result
from herder.runtimes.base import TerminateFn, run_with_terminate

if TYPE_CHECKING:
    from herder.permissions import Permissions


def build_ssh_argv(
    *,
    inner_argv: list[str],
    host: str,
    remote_root: str,
    env: dict[str, str],
    control_path: str,
    ssh_opts: list[str],
) -> list[str]:
    """Build the ``ssh`` argv list to run a command on a remote host.

    The remote command is a single POSIX shell string passed to the remote
    shell — every interpolated value is shlex-quoted to prevent injection.
    Environment variables are set as ``KEY=VALUE`` prefix tokens (POSIX-
    portable, no ``export`` needed for the exec call).

    Args:
        inner_argv: Command and arguments to run remotely.
        host: SSH destination, e.g. "user@hostname".
        remote_root: Remote working directory; ``cd`` into it first.
        env: Environment variables to set on the remote side.
        control_path: ControlMaster socket path for multiplexing/cancel.
        ssh_opts: Extra SSH options (e.g. ``["-p", "2222"]``).

    Returns:
        Complete argv list starting with "ssh".

    Example:
        >>> build_ssh_argv(inner_argv=["echo", "hi"], host="u@h",
        ...     remote_root="/tmp/herder", env={}, control_path="/tmp/cm",
        ...     ssh_opts=[])
        ['ssh', '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/cm',
         '-o', 'ControlPersist=60', 'u@h', 'cd /tmp/herder && exec echo hi']
    """
    env_prefix = " ".join(
        f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items()
    )
    inner_quoted = " ".join(shlex.quote(a) for a in inner_argv)

    if env_prefix:
        remote_cmd = (
            f"cd {shlex.quote(remote_root)} && {env_prefix} exec {inner_quoted}"
        )
    else:
        remote_cmd = f"cd {shlex.quote(remote_root)} && exec {inner_quoted}"

    return [
        "ssh",
        *ssh_opts,
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path}",
        "-o", "ControlPersist=60",
        host,
        remote_cmd,
    ]


@dataclass(frozen=True)
class SSHRuntime:
    """Runtime that executes jobs on a remote host via SSH.

    Cancellation closes the ControlMaster socket (``ssh -O exit``), which
    terminates the remote session without leaving orphaned processes.

    Fail-closed security guards (fire BEFORE any subprocess spawn):
    - Refuses jobs with ``perms.network is False`` — the remote host cannot
      apply macOS seatbelt or equivalent confinement.
    - Refuses jobs whose *resolved secret allow list* is non-empty unless
      ``allow_remote_secrets`` is True.  The secret list is the output of
      ``effective_allow_env(perms, provider_allow)`` passed in via the
      ``secret_keys`` argument — NOT the full minimised env, which always
      contains base keys (PATH/HOME/…) that are not secrets.

    Attributes:
        name: Runtime identifier used in resolution and logging.
        host: SSH destination (e.g. "user@host.example").
        remote_root: Remote working directory for the job.
        ssh_opts: Extra SSH flags (e.g. ``["-p", "2222"]``).
        allow_remote_secrets: If False (default), refuse jobs that carry
                              secrets (non-empty ``secret_keys``) to the
                              remote host.
    """

    name: str
    host: str
    remote_root: str = "/tmp/herder"
    ssh_opts: list[str] = field(default_factory=list)
    allow_remote_secrets: bool = False

    def _make_terminate(self, control_path: str) -> TerminateFn:
        """Return a terminate callable that closes the ControlMaster socket.

        Closing the ControlMaster connection terminates the multiplexed SSH
        session, which causes the remote shell to receive SIGHUP and exit —
        preventing orphaned remote processes.

        Args:
            control_path: Path to the ControlMaster socket file.

        Returns:
            A callable accepting a Popen instance that triggers SSH cleanup.
        """
        host = self.host
        ssh_opts = list(self.ssh_opts)

        def _terminate(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
            # Close the ControlMaster — remote shell receives SIGHUP
            subprocess.run(
                [
                    "ssh",
                    *ssh_opts,
                    "-O", "exit",
                    "-o", f"ControlPath={control_path}",
                    host,
                ],
                check=False,
                capture_output=True,
            )
            # Kill the local ssh client process as well (may already be dead)
            try:
                proc.kill()
            except ProcessLookupError:
                pass

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
        sandbox_profile: str | None,  # noqa: ARG002 — SSH cannot apply local sandbox
        perms: "Permissions | None",
        secret_keys: list[str] | None = None,
    ) -> Result:
        """Execute argv on the remote host and return a classified Result.

        Enforces both fail-closed guards before spawning any subprocess.
        Builds a per-attempt ControlMaster socket path, constructs the SSH
        argv via ``build_ssh_argv``, and delegates to the shared poll/kill
        core with an SSH-specific terminate strategy.

        Args:
            argv: Inner command and arguments to run remotely.
            prompt: Text passed to the remote process via stdin.
            cwd: Local working directory (used for output file paths).
            timeout: Wall-clock seconds before timeout outcome.
            env: Full minimised environment (base keys + any allowlisted
                 secrets) passed to the remote shell. Base keys (PATH/HOME/…)
                 are always present and are NOT considered secrets.
            stdout_path: File path for captured stdout (None → temp dir).
            stderr_path: File path for captured stderr (None → temp dir).
            cancel_check: Returns True when the job is cancelled externally.
            heartbeat: Optional callable to renew the job lease periodically.
            heartbeat_interval: Seconds between heartbeat calls.
            sandbox_profile: Unused — SSH cannot apply macOS seatbelt remotely.
            perms: Permissions instance; network=False triggers refusal.
            secret_keys: The *resolved* secret allow list — output of
                         ``effective_allow_env(perms, provider_allow)``.
                         Non-empty ⇒ the job carries secrets; the guard fires
                         unless ``allow_remote_secrets=True``.  Defaults to
                         ``[]`` (no secrets) when omitted.

        Returns:
            Result classified as done/failed/timeout/cancelled/unavailable.

        Raises:
            RuntimeError: If the job is untrusted (network=False) or would
                          carry secrets without ``allow_remote_secrets=True``.
        """
        effective_secret_keys: list[str] = secret_keys if secret_keys is not None else []

        # Guard 1: untrusted job — remote host cannot confine it
        if perms is not None and perms.network is False:
            raise RuntimeError(
                f"ssh runtime '{self.name}' cannot confine untrusted job "
                f"(network denied) — use docker or local runtime for sandboxed jobs"
            )

        # Guard 2: secrets leak prevention — check the RESOLVED secret allow
        # list, not the full env (which always has base keys like PATH/HOME).
        if effective_secret_keys and not self.allow_remote_secrets:
            raise RuntimeError(
                f"ssh runtime '{self.name}' would leak secrets to remote host "
                f"(set allow_remote_secrets: true to permit)"
            )

        # Build a per-attempt ControlMaster socket path
        unique_suffix = uuid.uuid4().hex[:12]
        control_path = f"/tmp/herder-cm-{unique_suffix}"

        ssh_argv = build_ssh_argv(
            inner_argv=argv,
            host=self.host,
            remote_root=self.remote_root,
            env=env,
            control_path=control_path,
            ssh_opts=list(self.ssh_opts),
        )

        effective_cancel = cancel_check if cancel_check is not None else (lambda: False)

        if stdout_path is not None and stderr_path is not None:
            return run_with_terminate(
                ssh_argv,
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                env={},
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cancel_check=effective_cancel,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
                terminate=self._make_terminate(control_path),
            )

        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            return run_with_terminate(
                ssh_argv,
                prompt=prompt,
                cwd=cwd,
                timeout=timeout,
                env={},
                stdout_path=tmp / "ssh_stdout.log",
                stderr_path=tmp / "ssh_stderr.log",
                cancel_check=effective_cancel,
                heartbeat=heartbeat,
                heartbeat_interval=heartbeat_interval,
                terminate=self._make_terminate(control_path),
            )
