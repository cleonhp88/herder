"""Runtime factory — builds a Runtime instance from a RuntimeSpec.

Dispatches on spec.type; each branch returns the appropriate frozen-dataclass
runtime.  Phase 5 and Phase 6 will add the docker and ssh branches.

CRITICAL: Never use shell=True. argv-only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herder.config import RuntimeSpec
    from herder.runtimes.base import Runtime


def build_runtime(spec: "RuntimeSpec") -> "Runtime":
    """Build a Runtime instance from a parsed RuntimeSpec.

    Dispatches on spec.type to instantiate the appropriate runtime backend.
    Docker and SSH backends raise NotImplementedError until Phase 5/6.

    Args:
        spec: A parsed RuntimeSpec (LocalRuntimeSpec, DockerRuntimeSpec, or
              SSHRuntimeSpec) — discriminated on the ``type`` field.

    Returns:
        A Runtime instance ready to accept ``.run(...)`` calls.

    Raises:
        NotImplementedError: If spec.type is "docker" or "ssh" (Phase 5/6).
        ValueError: If spec.type is unknown.
    """
    if spec.type == "local":
        from herder.runtimes.local import LocalRuntime
        return LocalRuntime()

    if spec.type == "docker":
        from herder.runtimes.docker import DockerRuntime
        return DockerRuntime(
            name="docker",
            image=spec.image,
            network=spec.network,
            extra_args=list(spec.extra_args),
        )

    if spec.type == "ssh":
        from herder.runtimes.ssh import SSHRuntime
        return SSHRuntime(
            name="ssh",
            host=spec.host,
            remote_root=spec.remote_root,
            ssh_opts=list(spec.ssh_opts),
            allow_remote_secrets=spec.allow_remote_secrets,
        )

    raise ValueError(f"unknown runtime type: {spec.type!r}")
