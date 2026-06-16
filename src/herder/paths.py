"""Environment-overridable paths for herder home and data directories."""
import os
from pathlib import Path


def home() -> Path:
    """Return the herder home directory.

    Defaults to ~/.herder, but can be overridden via HERDER_HOME env var.

    Returns:
        Path to the herder home directory.
    """
    env = os.environ.get("HERDER_HOME")
    return Path(env) if env else Path.home() / ".herder"


def db_path() -> Path:
    """Return the path to the herder database file.

    Returns:
        Path to herder.db in the home directory.
    """
    return home() / "herder.db"


def runs_dir() -> Path:
    """Return the path to the runs directory.

    Returns:
        Path to the runs directory in the home directory.
    """
    return home() / "runs"


def worktrees_dir() -> Path:
    """Return the path to the worktrees directory.

    Returns:
        Path to the worktrees directory in the home directory.
    """
    return home() / "worktrees"
