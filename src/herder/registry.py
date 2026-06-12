"""Registry — resolves role+project permissions and provider configuration."""
from __future__ import annotations

import json

from herder.config import Config, ConfigError, PERMISSION_LEVELS, format_supports


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

    role_obj = cfg.roles[role]
    perms = dict(_PERM.get(role_obj.permissions, _PERM["read_only"]))
    mode = proj.default_workspace_mode
    if mode == "inplace":
        if not proj.allow_inplace:
            raise ConfigError(f"inplace not allowed for project '{project}'")
        perms["require_confirm"] = True

    provider_name = cfg.resolve_provider_for_role(role)
    provider_obj = cfg.providers[provider_name]

    # Pre-call capability check: if the provider declares a non-empty supports
    # list, the role's permission level must be one of the declared values.
    # Empty supports means the provider imposes no restriction.
    if provider_obj.supports and role_obj.permissions not in provider_obj.supports:
        supports_str = format_supports(provider_obj.supports)
        raise ConfigError(
            f"provider '{provider_name}' does not support permission "
            f"'{role_obj.permissions}' required by role '{role}' "
            f"(supports: {supports_str})"
        )

    return {
        "provider": provider_name,
        "cwd": proj.root,
        "workspace_mode": mode,
        "permissions": json.dumps(perms),
    }
