"""Tests for herder.paths module."""
from pathlib import Path


from herder import paths


def test_home_defaults_to_user_dir(monkeypatch):
    """When HERDER_HOME is not set, home() returns ~/.herder."""
    monkeypatch.delenv("HERDER_HOME", raising=False)
    assert paths.home() == Path.home() / ".herder"


def test_home_respects_env(herder_home):
    """When HERDER_HOME is set, home() returns that path."""
    assert paths.home() == herder_home


def test_db_path(herder_home):
    """db_path() returns {home}/herder.db."""
    assert paths.db_path() == herder_home / "herder.db"


def test_runs_dir(herder_home):
    """runs_dir() returns {home}/runs."""
    assert paths.runs_dir() == herder_home / "runs"


def test_worktrees_dir(herder_home):
    """worktrees_dir() returns {home}/worktrees."""
    assert paths.worktrees_dir() == herder_home / "worktrees"
