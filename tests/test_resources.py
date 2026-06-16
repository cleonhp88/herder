"""Tests for src/herder/resources.py — bundled-resource resolvers.

Coverage:
- bundled_recipes_dir(): exists + contains kiro.yaml
- bundled_config_example(): exists + non-empty
- resolve_recipes_dir(): explicit honoured; cwd recipes/ → cwd; absent → bundled
- resolve_config_example(): local example wins; absent local → bundled
- _bootstrap_config via bundled: empty temp dir produces non-empty config.yaml
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from herder.resources import (
    bundled_config_example,
    bundled_recipes_dir,
    resolve_config_example,
    resolve_recipes_dir,
)


# ---------------------------------------------------------------------------
# bundled_recipes_dir
# ---------------------------------------------------------------------------

class TestBundledRecipesDir:
    def test_exists(self) -> None:
        """bundled_recipes_dir() returns a directory that actually exists."""
        path = bundled_recipes_dir()
        assert path.is_dir(), f"Bundled recipes directory not found: {path}"

    def test_contains_kiro_yaml(self) -> None:
        """bundled_recipes_dir() contains kiro.yaml."""
        path = bundled_recipes_dir()
        kiro = path / "kiro.yaml"
        assert kiro.exists(), f"kiro.yaml missing from bundled recipes at {path}"
        assert kiro.stat().st_size > 0, "kiro.yaml must not be empty"


# ---------------------------------------------------------------------------
# bundled_config_example
# ---------------------------------------------------------------------------

class TestBundledConfigExample:
    def test_exists(self) -> None:
        """bundled_config_example() returns a file that actually exists."""
        path = bundled_config_example()
        assert path.is_file(), f"Bundled config.example.yaml not found: {path}"

    def test_non_empty(self) -> None:
        """bundled_config_example() is non-empty (not a stub/placeholder)."""
        path = bundled_config_example()
        assert path.stat().st_size > 0, "bundled config.example.yaml is empty"

    def test_contains_providers_key(self) -> None:
        """Bundled template is a valid YAML file containing 'providers:'."""
        path = bundled_config_example()
        content = path.read_text(encoding="utf-8")
        assert "providers:" in content, (
            "bundled config.example.yaml does not contain 'providers:' key"
        )


# ---------------------------------------------------------------------------
# resolve_recipes_dir
# ---------------------------------------------------------------------------

class TestResolveRecipesDir:
    def test_explicit_path_honoured(self, tmp_path: Path) -> None:
        """Explicit --recipes-dir value is returned as-is (Path conversion only)."""
        explicit = str(tmp_path)
        result = resolve_recipes_dir(explicit)
        assert result == Path(explicit)

    def test_cwd_recipes_used_when_present(self, tmp_path: Path) -> None:
        """When ./recipes/ exists in CWD, it is returned without falling back."""
        recipes = tmp_path / "recipes"
        recipes.mkdir()
        (recipes / "dummy.yaml").write_text("name: dummy\n")

        original_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            result = resolve_recipes_dir(None)
        finally:
            os.chdir(original_cwd)

        assert result == recipes.resolve()
        assert result.is_dir()

    def test_bundled_returned_when_no_cwd_recipes(self, tmp_path: Path) -> None:
        """Bundled recipes dir is returned when no ./recipes/ exists in CWD."""
        original_cwd = Path.cwd()
        os.chdir(tmp_path)  # tmp_path has no recipes/ subdir
        try:
            result = resolve_recipes_dir(None)
        finally:
            os.chdir(original_cwd)

        bundled = bundled_recipes_dir()
        assert result == bundled
        assert result.is_dir()
        assert (result / "kiro.yaml").exists()

    def test_none_returns_bundled_when_no_cwd_recipes(self, tmp_path: Path) -> None:
        """None explicit with no CWD ./recipes/ → bundled dir exists."""
        original_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            result = resolve_recipes_dir(None)
        finally:
            os.chdir(original_cwd)

        assert result.is_dir(), "Resolved dir must exist"
        assert (result / "kiro.yaml").exists(), "kiro.yaml must be in resolved dir"


# ---------------------------------------------------------------------------
# resolve_config_example
# ---------------------------------------------------------------------------

class TestResolveConfigExample:
    def test_local_example_wins(self, tmp_path: Path) -> None:
        """Local config.example.yaml takes priority over bundled."""
        local = tmp_path / "config.example.yaml"
        local.write_text("# local example\nproviders: {local: true}\n", encoding="utf-8")

        result = resolve_config_example(tmp_path)

        assert result == local

    def test_bundled_used_when_local_absent(self, tmp_path: Path) -> None:
        """Bundled config.example.yaml used when local file is absent."""
        # tmp_path has no config.example.yaml
        result = resolve_config_example(tmp_path)

        assert result == bundled_config_example()
        assert result.exists()

    def test_result_is_non_empty(self, tmp_path: Path) -> None:
        """Resolved config.example.yaml (bundled) is non-empty."""
        result = resolve_config_example(tmp_path)
        assert result.stat().st_size > 0


# ---------------------------------------------------------------------------
# _bootstrap_config with bundled fallback (integration)
# ---------------------------------------------------------------------------

class TestBootstrapConfigBundledFallback:
    def test_creates_config_yaml_from_bundled_in_empty_dir(self, tmp_path: Path) -> None:
        """_bootstrap_config in an empty temp dir (no local config.example.yaml)
        creates config.yaml by copying the bundled template."""
        from herder.init_cmd import _bootstrap_config

        config_path = tmp_path / "config.yaml"
        assert not (tmp_path / "config.example.yaml").exists()

        created = _bootstrap_config(config_path)

        assert created is True
        assert config_path.exists(), "config.yaml must be created"
        assert config_path.stat().st_size > 0, "config.yaml must be non-empty"
        # Content must come from bundled template (contains 'providers:')
        content = config_path.read_text(encoding="utf-8")
        assert "providers:" in content, (
            "config.yaml created from bundled template must contain 'providers:'"
        )
