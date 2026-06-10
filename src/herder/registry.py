"""Registry — resolves role+project permissions and provider configuration."""
from __future__ import annotations

import json

from herder.config import Config, ConfigError


_PERM = {
    "read_only": {
        "filesystem": "read_only",
        "network": "limited",
        "shell_tools": False,
        "secret_access": True,
        "require_confirm": False,
    },
    "worktree_write": {
        "filesystem": "worktree_write",
        "network": "limited",
        "shell_tools": True,
        "secret_access": True,
        "require_confirm": False,
    },
    "inplace_write": {
        "filesystem": "inplace_write",
        "network": "limited",
        "shell_tools": True,
        "secret_access": True,
        "require_confirm": True,
    },
    "untrusted": {
        "filesystem": "read_only",
        "network": False,
        "shell_tools": False,
        "secret_access": False,
        "require_confirm": False,
    },
}


def resolve(cfg: Config, *, role: str, project: str) -> dict:
    """Resolve provider and permissions for a given role and project.

    Args:
        cfg: Loaded configuration.
        role: Role name.
        project: Project name.

    Returns:
        Dictionary with keys: provider, cwd, workspace_mode, permissions.

    Raises:
        ConfigError: If project/role unknown or role not allowed in project.
    """
    if project not in cfg.projects:
        raise ConfigError(f"unknown project: {project}")
    if role not in cfg.roles:
        raise ConfigError(f"unknown role: {role}")
    proj = cfg.projects[project]
    if proj.allowed_roles and role not in proj.allowed_roles:
        raise ConfigError(f"role '{role}' not allowed in project '{project}'")

    perms = dict(_PERM.get(cfg.roles[role].permissions, _PERM["read_only"]))
    mode = proj.default_workspace_mode
    if mode == "inplace":
        if not proj.allow_inplace:
            raise ConfigError(f"inplace not allowed for project '{project}'")
        perms["require_confirm"] = True

    return {
        "provider": cfg.resolve_provider_for_role(role),
        "cwd": proj.root,
        "workspace_mode": mode,
        "permissions": json.dumps(perms),
    }
