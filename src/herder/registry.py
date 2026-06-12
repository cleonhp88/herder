"""Registry — resolves role+project permissions and provider configuration."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from herder.config import Config, ConfigError, PERMISSION_LEVELS, format_supports

if TYPE_CHECKING:
    from herder.db.store import Store


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


def resolve(cfg: Config, *, role: str, project: str, store: "Store | None" = None) -> dict:
    """Resolve provider and permissions for a given role and project.

    When ``store`` is provided and the role has more than one provider, the
    cooldown-aware ``select_provider`` function is used to pick the best
    available provider at enqueue time.  When ``store`` is ``None`` (backward-
    compatible default), the primary provider (first in the list) is used
    unconditionally — existing callers without a DB handle keep working.

    Every production caller MUST pass ``store`` so that cooldown routing is
    applied at enqueue; omitting it disables cooldown (test/dry-run only).

    Args:
        cfg: Loaded configuration.
        role: Role name.
        project: Project name.
        store: Optional SQLite store.  Required for cooldown routing in production.

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

    # Select provider: cooldown-aware when store is available and role has
    # multiple providers; falls back to the primary provider otherwise.
    if store is not None and len(role_obj.providers) > 1:
        from herder.routing import select_provider
        provider_name = select_provider(
            role_obj.providers, None, role_obj.cooldown, store
        )
    else:
        provider_name = cfg.resolve_provider_for_role(role)

    provider_obj = cfg.providers[provider_name]

    # Pre-call capability check: if the provider declares a non-empty supports
    # list, the role's permission level must be one of the declared values.
    # Empty supports means the provider imposes no restriction.
    # All providers in the list were validated at load time by validate_refs(),
    # so a runtime check on the selected provider is consistent.
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
