"""Tests for herder.registry module."""
import json
import pytest

from herder.config import Config, ConfigError, PERMISSION_LEVELS
from herder.registry import resolve, _PERM


def _cfg(**proj):
    """Create a test Config with standard roles and providers."""
    return Config(**{
        "providers": {"echo": {"type": "cli", "executable": "cat", "input": "stdin"}},
        "roles": {
            "planner": {"provider": "echo", "permissions": "read_only"},
            "coder": {"provider": "echo", "permissions": "worktree_write"}
        },
        "projects": {"p": {"root": "/tmp/x", **proj}},
        "worker": {"global_concurrency": 1},
    })


def test_unknown_project_rejected():
    """resolve raises ConfigError for unknown project."""
    with pytest.raises(ConfigError, match="unknown project"):
        resolve(_cfg(allowed_roles=["planner"]), role="planner", project="nope")


def test_unknown_role_rejected():
    """resolve raises ConfigError (not KeyError) for unknown role."""
    with pytest.raises(ConfigError, match="unknown role"):
        resolve(_cfg(allowed_roles=[]), role="ghost", project="p")


def test_role_not_in_allowed_roles_rejected():
    """resolve raises ConfigError when role not in project's allowed_roles."""
    with pytest.raises(ConfigError, match="not allowed"):
        resolve(_cfg(allowed_roles=["planner"]), role="coder", project="p")


def test_inplace_without_allow_inplace_rejected():
    """resolve raises ConfigError when inplace mode used but not allowed."""
    with pytest.raises(ConfigError, match="inplace not allowed"):
        resolve(
            _cfg(
                allowed_roles=["planner"],
                default_workspace_mode="inplace",
                allow_inplace=False
            ),
            role="planner",
            project="p"
        )


def test_happy_path_shape():
    """resolve returns correct structure for valid role+project."""
    r = resolve(_cfg(allowed_roles=["planner"]), role="planner", project="p")
    assert r["provider"] == "echo"
    assert r["workspace_mode"] == "readonly"
    assert r["cwd"] == "/tmp/x"
    perms = json.loads(r["permissions"])
    assert perms["filesystem"] == "read_only"
    assert perms["secret_access"] is True  # read_only now grants secret_access


def test_untrusted_preset_denies_secrets_and_network():
    """untrusted preset denies both secret_access and network."""
    cfg = Config(**{
        "providers": {"echo": {"type": "cli", "executable": "cat", "input": "stdin"}},
        "roles": {"scout": {"provider": "echo", "permissions": "untrusted"}},
        "projects": {"p": {"root": "/tmp/x", "allowed_roles": ["scout"]}},
        "worker": {"global_concurrency": 1},
    })
    r = resolve(cfg, role="scout", project="p")
    perms = json.loads(r["permissions"])
    assert perms["secret_access"] is False
    assert perms["network"] is False
    assert perms["filesystem"] == "read_only"
    assert perms["shell_tools"] is False


# ---------------------------------------------------------------------------
# Tier 1: Pre-call capability check in registry.resolve()
# ---------------------------------------------------------------------------

def _cfg_with_supports(perm: str, supports: list[str]) -> Config:
    """Build a Config where the provider's supports list is set.

    Args:
        perm: Role permissions value.
        supports: List of permission levels the provider declares it supports.

    Returns:
        Constructed Config object.
    """
    return Config(**{
        "providers": {
            "echo": {
                "type": "cli",
                "executable": "cat",
                "input": "stdin",
                "supports": supports,
            }
        },
        "roles": {"r": {"provider": "echo", "permissions": perm}},
        "projects": {"p": {"root": "/tmp/x", "allowed_roles": ["r"]}},
        "worker": {"global_concurrency": 1},
    })


def test_resolve_supports_empty_allows_any_permission():
    """Empty supports list means no restriction — any permission level is accepted."""
    cfg = _cfg_with_supports("inplace_write", [])
    r = resolve(cfg, role="r", project="p")
    assert r["provider"] == "echo"


def test_resolve_permission_in_supports_is_accepted():
    """Role permission in provider supports list must resolve without error."""
    cfg = _cfg_with_supports("read_only", ["read_only", "worktree_write"])
    r = resolve(cfg, role="r", project="p")
    assert r["provider"] == "echo"


def test_resolve_permission_not_in_supports_raises():
    """Role permission absent from non-empty provider supports must raise ConfigError."""
    cfg = _cfg_with_supports("inplace_write", ["read_only", "worktree_write"])
    with pytest.raises(ConfigError, match="inplace_write"):
        resolve(cfg, role="r", project="p")


def test_perm_keys_match_permission_levels():
    """_PERM keys must exactly equal PERMISSION_LEVELS — guards silent privilege drift."""
    assert set(_PERM.keys()) == PERMISSION_LEVELS
