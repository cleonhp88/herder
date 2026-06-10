"""Permissions model — structured representation of job access control.

The permissions JSON is snapshotted on each job at enqueue time.
At execution time, effective_allow_env() gates which provider secrets the job receives.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Permissions:
    """Immutable permissions model for a job.

    Attributes:
        filesystem: "read_only" | "worktree_write" | "inplace_write"
        network: False | "limited" | True
        shell_tools: Whether job can use shell execution tools
        secret_access: Whether job may receive secret environment variables
        require_confirm: Whether human confirmation is required before execution
    """

    filesystem: str = "read_only"
    network: object = False  # False | "limited" | True
    shell_tools: bool = False
    secret_access: bool = False
    require_confirm: bool = False

    @classmethod
    def from_json(cls, s: str | None) -> Permissions:
        """Parse a JSON permissions string into a Permissions object.

        Args:
            s: JSON string (or None, which uses all defaults).

        Returns:
            Permissions instance with parsed fields.

        Raises:
            json.JSONDecodeError: If s is not valid JSON.
        """
        d = json.loads(s) if s else {}

        # FIX 5: Normalize network to fail-closed (deny by default)
        # Only "limited" or True are valid open values; anything else → False
        raw_net = d.get("network", False)
        if raw_net == "limited":
            network = "limited"
        elif raw_net is True:
            network = True
        else:
            # 0, null, "", unknown, or any other value → deny (fail-closed)
            network = False

        return cls(
            filesystem=d.get("filesystem", "read_only"),
            network=network,
            shell_tools=bool(d.get("shell_tools", False)),
            secret_access=bool(d.get("secret_access", False)),
            require_confirm=bool(d.get("require_confirm", False)),
        )


def effective_allow_env(perms: Permissions, provider_allow: list[str]) -> list[str]:
    """Compute the environment variables a job may actually receive.

    This is the runtime enforcement of the secret_access bit. A job gets
    the provider's allowlisted secret env keys ONLY if its permissions
    grant secret_access=true. Otherwise, returns an empty list.

    Args:
        perms: Job permissions.
        provider_allow: List of secret env keys the provider is willing to share.

    Returns:
        List of env keys the job may actually receive (empty if secret_access=False).
    """
    return list(provider_allow) if perms.secret_access else []
