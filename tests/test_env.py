"""Tests for herder.env module."""
import pytest
from herder import env as env_mod
from herder.env import build_env


@pytest.fixture(autouse=True)
def _reset_login_cache():
    env_mod._login_cache = None
    yield
    env_mod._login_cache = None


def _fake_login(monkeypatch, d):
    """Monkeypatch _login_shell_env to return a fixed dict."""
    monkeypatch.setattr(env_mod, "_login_shell_env", lambda: d)


def test_only_base_keys_when_no_allowlist(monkeypatch):
    """When allow_env is None, build_env returns only base keys."""
    _fake_login(
        monkeypatch,
        {"PATH": "/login/bin", "SECRET_TOKEN": "abc", "HOME": "/h"},
    )
    out = build_env(None, base={})
    assert out["PATH"] == "/login/bin" and out["HOME"] == "/h"
    assert "SECRET_TOKEN" not in out  # secret NOT leaked without allowlist


def test_allowlisted_secret_included(monkeypatch):
    """When a secret is allowlisted, it is included in the output."""
    _fake_login(
        monkeypatch,
        {
            "PATH": "/b",
            "COMMAND_CODE_API_KEY": "sk-x",
            "OTHER_KEY": "y",
        },
    )
    out = build_env(["COMMAND_CODE_API_KEY"], base={})
    assert out["COMMAND_CODE_API_KEY"] == "sk-x"  # this provider's secret present
    assert "OTHER_KEY" not in out  # unrelated secret absent


def test_returns_copy_not_login_dict(monkeypatch):
    """build_env returns a copy, not the cached login dict."""
    d = {"PATH": "/b"}
    _fake_login(monkeypatch, d)
    out = build_env(None, base={})
    out["PATH"] = "mutated"
    assert d["PATH"] == "/b"


def test_login_capture_failure_falls_back_to_base(monkeypatch):
    """When login shell capture fails, fall back to base env."""

    def boom():
        raise OSError("no shell")

    monkeypatch.setattr(env_mod, "_login_shell_env_raw", boom)
    out = build_env(["K"], base={"PATH": "/p", "K": "v"})
    assert out["PATH"] == "/p" and out["K"] == "v"  # base used; allowlisted K kept


def test_login_shell_cached(monkeypatch):
    """Login shell env is captured once and cached across multiple calls."""
    calls = []

    def mock_spawn():
        calls.append(1)
        return "A=1\x00PATH=/b\x00"

    monkeypatch.setattr(env_mod, "_login_shell_env_raw", mock_spawn)
    build_env(None, base={})
    build_env(["A"], base={})
    assert len(calls) == 1


def test_multiple_allowlisted_keys(monkeypatch):
    """Multiple allowlisted keys are all included."""
    _fake_login(
        monkeypatch,
        {
            "PATH": "/b",
            "COMMAND_CODE_API_KEY": "sk-x",
            "GH_TOKEN": "ghp-abc",
            "OTHER": "z",
        },
    )
    out = build_env(["COMMAND_CODE_API_KEY", "GH_TOKEN"], base={})
    assert out["COMMAND_CODE_API_KEY"] == "sk-x"
    assert out["GH_TOKEN"] == "ghp-abc"
    assert "OTHER" not in out


def test_allowlist_key_not_in_env_ignored(monkeypatch):
    """If an allowlisted key is not in login or base, it is omitted."""
    _fake_login(monkeypatch, {"PATH": "/b"})
    out = build_env(["MISSING_KEY"], base={})
    assert "MISSING_KEY" not in out
    assert out["PATH"] == "/b"


def test_base_keys_sourced_from_login_first(monkeypatch):
    """Base keys prefer login shell env, fall back to base."""
    _fake_login(monkeypatch, {"PATH": "/login/bin", "HOME": "/login/home"})
    out = build_env(None, base={"PATH": "/base/path", "CUSTOM": "x"})
    assert out["PATH"] == "/login/bin"  # login wins
    assert "HOME" in out  # only base keys included
    assert "CUSTOM" not in out  # non-base keys excluded


def test_empty_allowlist_is_same_as_none(monkeypatch):
    """Empty allowlist behaves the same as None."""
    _fake_login(monkeypatch, {"PATH": "/b", "SECRET": "s"})
    out1 = build_env(None, base={})
    out2 = build_env([], base={})
    assert out1 == out2
    assert "SECRET" not in out1 and "SECRET" not in out2
