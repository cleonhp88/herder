"""Bundled-resource resolvers for Herder.

Provides stable, install-location-agnostic paths to files that ship inside
the ``herder`` package (recipes/, config.example.yaml).  Works correctly after
``uv tool install`` where the package is unpacked to a tool-managed directory
and there is no ``recipes/`` directory relative to the CWD.

Resolution strategy for each helper:
- If the caller provides an explicit path, honour it unconditionally.
- Then try a CWD-relative conventional path (for in-repo / local dev use).
- Fall back to the file bundled inside the installed package.
"""
from __future__ import annotations

import importlib.resources
from pathlib import Path


# ---------------------------------------------------------------------------
# Internal: locate bundled package root
# ---------------------------------------------------------------------------

def _package_dir() -> Path:
    """Return the on-disk directory of the ``herder`` package.

    Uses ``importlib.resources.files`` which works for both editable installs
    (returns the source tree) and regular/tool installs (returns the unpacked
    wheel location).

    Returns:
        Absolute Path to the herder package directory.
    """
    return Path(str(importlib.resources.files("herder")))


# ---------------------------------------------------------------------------
# Bundled accessors
# ---------------------------------------------------------------------------

def bundled_recipes_dir() -> Path:
    """Return the path to the recipes directory bundled inside the package.

    Returns:
        Absolute Path to ``herder/recipes/`` within the installed package.

    Raises:
        FileNotFoundError: If the bundled recipes directory is somehow absent
            (should never happen in a correctly installed package).
    """
    path = _package_dir() / "recipes"
    if not path.is_dir():
        raise FileNotFoundError(
            f"Bundled recipes directory not found at {path}. "
            "The package installation may be incomplete."
        )
    return path


def bundled_config_example() -> Path:
    """Return the path to config.example.yaml bundled inside the package.

    Returns:
        Absolute Path to ``herder/config.example.yaml`` within the installed
        package.

    Raises:
        FileNotFoundError: If the bundled template is somehow absent.
    """
    path = _package_dir() / "config.example.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Bundled config.example.yaml not found at {path}. "
            "The package installation may be incomplete."
        )
    return path


# ---------------------------------------------------------------------------
# Resolvers (explicit → cwd-relative → bundled)
# ---------------------------------------------------------------------------

def resolve_recipes_dir(explicit: str | None) -> Path:
    """Resolve the recipes directory using a three-tier fallback.

    Resolution order:
      1. ``explicit`` path if provided (caller override, e.g. ``--recipes-dir``).
      2. ``./recipes`` relative to the current working directory if it exists.
      3. The recipes directory bundled inside the installed package.

    Args:
        explicit: Value of ``--recipes-dir`` CLI argument, or ``None`` when
            the argument was not provided.

    Returns:
        Resolved Path to a recipes directory (always exists when returned from
        tier 2 or 3; tier 1 is returned as-is even if it does not exist so the
        caller can surface a useful error).
    """
    if explicit is not None:
        return Path(explicit)

    cwd_recipes = Path("recipes")
    if cwd_recipes.is_dir():
        return cwd_recipes.resolve()  # absolute → safe if the caller later changes cwd

    return bundled_recipes_dir()


def resolve_config_example(start_dir: Path) -> Path:
    """Resolve config.example.yaml using a two-tier fallback.

    Resolution order:
      1. ``start_dir / config.example.yaml`` if it exists (in-repo / local).
      2. The template bundled inside the installed package.

    Args:
        start_dir: Directory to search first (typically the directory that will
            contain config.yaml, i.e. ``config_path.parent``).

    Returns:
        Resolved Path to a config.example.yaml that exists.

    Raises:
        FileNotFoundError: If neither the local nor the bundled file can be
            found (should not happen in a correctly installed package).
    """
    local = start_dir / "config.example.yaml"
    if local.exists():
        return local

    return bundled_config_example()
