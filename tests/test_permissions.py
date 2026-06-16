"""Tests for herder.permissions module."""
import pytest

from herder.permissions import Permissions, effective_allow_env


def test_from_json_defaults():
    """from_json with None uses all defaults."""
    p = Permissions.from_json(None)
    assert p.filesystem == "read_only"
    assert p.network is False
    assert p.shell_tools is False
    assert p.secret_access is False
    assert p.require_confirm is False


def test_from_json_parses_all_fields():
    """from_json parses all fields from a complete JSON string."""
    json_str = (
        '{"filesystem": "worktree_write", "network": "limited", '
        '"shell_tools": true, "secret_access": true, "require_confirm": true}'
    )
    p = Permissions.from_json(json_str)
    assert p.filesystem == "worktree_write"
    assert p.network == "limited"
    assert p.shell_tools is True
    assert p.secret_access is True
    assert p.require_confirm is True


def test_from_json_partial():
    """from_json fills in missing fields with defaults."""
    json_str = '{"secret_access": true, "network": "limited", "filesystem": "inplace_write"}'
    p = Permissions.from_json(json_str)
    assert p.secret_access is True
    assert p.network == "limited"
    assert p.filesystem == "inplace_write"
    assert p.shell_tools is False
    assert p.require_confirm is False


def test_from_json_empty_object():
    """from_json with empty JSON object uses all defaults."""
    p = Permissions.from_json("{}")
    assert p.filesystem == "read_only"
    assert p.network is False
    assert p.shell_tools is False
    assert p.secret_access is False
    assert p.require_confirm is False


def test_from_json_invalid_json_raises():
    """from_json raises JSONDecodeError on invalid JSON."""
    with pytest.raises(Exception):  # json.JSONDecodeError
        Permissions.from_json("not valid json")


def test_effective_allow_env_gated_by_secret_access_true():
    """effective_allow_env returns provider_allow when secret_access=True."""
    allow = ["COMMAND_CODE_API_KEY", "ANOTHER_SECRET"]
    perms = Permissions(secret_access=True)
    result = effective_allow_env(perms, allow)
    assert result == allow


def test_effective_allow_env_gated_by_secret_access_false():
    """effective_allow_env returns empty list when secret_access=False."""
    allow = ["COMMAND_CODE_API_KEY", "ANOTHER_SECRET"]
    perms = Permissions(secret_access=False)
    result = effective_allow_env(perms, allow)
    assert result == []


def test_effective_allow_env_empty_provider_allow():
    """effective_allow_env with empty provider_allow returns empty."""
    perms = Permissions(secret_access=True)
    result = effective_allow_env(perms, [])
    assert result == []


def test_permissions_is_frozen():
    """Permissions is frozen (immutable)."""
    perms = Permissions(secret_access=True)
    with pytest.raises(AttributeError):
        perms.secret_access = False  # type: ignore


def test_network_normalizes_fail_closed():
    """FIX 5: Network field normalizes to fail-closed for invalid values.

    Only "limited" or True are valid; everything else (0, null, "", unknown) → False.
    """
    # 0 → False (deny)
    assert Permissions.from_json('{"network": 0}').network is False
    # null → False (deny)
    assert Permissions.from_json('{"network": null}').network is False
    # "" → False (deny)
    assert Permissions.from_json('{"network": ""}').network is False
    # "weird" (unknown) → False (deny)
    assert Permissions.from_json('{"network": "weird"}').network is False
    # "limited" → "limited" (allow, limited scope)
    assert Permissions.from_json('{"network": "limited"}').network == "limited"
    # true → True (allow, unrestricted)
    assert Permissions.from_json('{"network": true}').network is True
