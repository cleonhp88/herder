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


# ---------------------------------------------------------------------------
# Tier 2: resolve() with store= parameter — cooldown-aware routing
# ---------------------------------------------------------------------------

def _cfg_two_providers(perm: str = "read_only") -> Config:
    """Build a Config with two providers and a two-provider role."""
    return Config(**{
        "providers": {
            "primary": {"type": "cli", "executable": "cat", "input": "stdin"},
            "secondary": {"type": "cli", "executable": "cat", "input": "stdin"},
        },
        "roles": {
            "r": {
                "providers": ["primary", "secondary"],
                "permissions": perm,
                "cooldown": {"allowed_fails": 3, "window_seconds": 300},
            }
        },
        "projects": {"p": {"root": "/tmp/x", "allowed_roles": ["r"]}},
        "worker": {"global_concurrency": 1},
    })


def test_resolve_without_store_uses_primary():
    """resolve(store=None) falls back to primary provider (backward compat)."""
    cfg = _cfg_two_providers()
    r = resolve(cfg, role="r", project="p")
    assert r["provider"] == "primary"


def test_resolve_with_store_no_failures_uses_primary(herder_home):
    """resolve(store=...) with no failures returns the primary provider."""
    from herder.db.store import Store

    cfg = _cfg_two_providers()
    store = Store.open()
    r = resolve(cfg, role="r", project="p", store=store)
    assert r["provider"] == "primary"


def test_resolve_with_store_cooling_primary_uses_secondary(herder_home):
    """resolve(store=...) skips a cooling primary and returns secondary."""
    from datetime import datetime, timezone
    from herder.db.store import Store

    cfg = _cfg_two_providers()
    store = Store.open()

    # Seed a minimal job so we can attach attempts to it.
    store.enqueue(
        id="j1",
        kind="test",
        role="r",
        provider="primary",
        project=None,
        cwd="/tmp/x",
        workspace_mode="readonly",
        permissions="{}",
        status="done",
        prompt_path="/tmp/x/p.md",
        prompt_hash="h",
        run_dir="/tmp/x",
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    # 3 failures within window → primary is cooling (allowed_fails=3)
    for attempt_no in range(1, 4):
        store.record_attempt(
            job_id="j1",
            attempt_no=attempt_no,
            worker_id="w",
            exit_code=1,
            status="failed",
            provider="primary",
            finished_at=now_iso,
        )

    r = resolve(cfg, role="r", project="p", store=store)
    assert r["provider"] == "secondary"


def test_resolve_single_provider_with_store_uses_primary(herder_home):
    """resolve(store=...) with a single-provider role always returns that provider."""
    from herder.db.store import Store

    cfg = Config(**{
        "providers": {"echo": {"type": "cli", "executable": "cat", "input": "stdin"}},
        "roles": {"r": {"provider": "echo", "permissions": "read_only"}},
        "projects": {"p": {"root": "/tmp/x", "allowed_roles": ["r"]}},
        "worker": {"global_concurrency": 1},
    })
    store = Store.open()
    r = resolve(cfg, role="r", project="p", store=store)
    assert r["provider"] == "echo"
