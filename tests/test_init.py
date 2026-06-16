"""Tests for herder init command (src/herder/init_cmd.py) and install.sh.

Coverage:
- config bootstrap: create from example / keep existing
- brain detection: mocked presence/absence of dirs and binaries
- brain detection: non-mocked smoke test (P1 regression guard)
- cheat-sheet intro block: idempotent write (one block after two inits)
- init↔add coexistence: agent line survives a second write_intro_block call
- --yes path: does not launch interactive add menu
- install.sh: POSIX syntax check (sh -n) + optional shellcheck
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from herder.adapters import BLOCK_END, BLOCK_START, _INTRO_PREFIX, write_cheatsheet, write_intro_block
from herder.init_cmd import (
    _bootstrap_config,
    _wire_brain_targets,
    cmd_init,
    detect_brains,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


def _make_args(
    config: str,
    yes: bool = True,
    recipes_dir: str | None = None,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for cmd_init."""
    return argparse.Namespace(config=config, yes=yes, recipes_dir=recipes_dir)


def _write_example(tmp_path: Path) -> Path:
    """Write a minimal config.example.yaml next to where config.yaml will live."""
    example = tmp_path / "config.example.yaml"
    example.write_text("providers: {}\nroles: {}\n", encoding="utf-8")
    return example


# ---------------------------------------------------------------------------
# _bootstrap_config
# ---------------------------------------------------------------------------

class TestBootstrapConfig:
    def test_creates_config_from_example_when_absent(self, tmp_path: Path) -> None:
        """init creates config.yaml from config.example.yaml when file is missing."""
        _write_example(tmp_path)
        config_path = tmp_path / "config.yaml"
        assert not config_path.exists()

        created = _bootstrap_config(config_path)

        assert created is True
        assert config_path.exists()
        assert config_path.read_text() == (tmp_path / "config.example.yaml").read_text()

    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        """init preserves an existing config.yaml byte-for-byte."""
        _write_example(tmp_path)
        config_path = tmp_path / "config.yaml"
        original_content = "# hand-tuned\nproviders: {existing: true}\n"
        config_path.write_text(original_content, encoding="utf-8")

        created = _bootstrap_config(config_path)

        assert created is False
        assert config_path.read_text() == original_content, (
            "Existing config.yaml must not be overwritten"
        )

    def test_falls_back_to_bundled_when_local_example_absent(self, tmp_path: Path) -> None:
        """_bootstrap_config creates config.yaml from the bundled template when
        no local config.example.yaml is present in the same directory."""
        config_path = tmp_path / "config.yaml"
        # No local config.example.yaml written — must use bundled fallback.
        assert not (tmp_path / "config.example.yaml").exists()

        created = _bootstrap_config(config_path)

        assert created is True
        assert config_path.exists()
        assert config_path.stat().st_size > 0, "config.yaml must be non-empty"


# ---------------------------------------------------------------------------
# detect_brains
# ---------------------------------------------------------------------------

def _make_fake_expanduser(tmp_path: Path) -> Callable[[Path], Path]:
    """Return a Path.expanduser replacement that maps ~ to tmp_path.

    Uses os.path.expanduser (string-level) to avoid recursing back into the
    patched Path.expanduser method.
    """
    import os

    real_home = str(Path.home())

    def _fake_expanduser(self: Path) -> Path:
        # Expand using the OS-level function — never re-enters this mock.
        expanded = os.path.expanduser(str(self))
        expanded_path = Path(expanded)
        # Redirect anything under the real home into tmp_path.
        try:
            rel = expanded_path.relative_to(real_home)
            return tmp_path / rel
        except ValueError:
            return expanded_path

    return _fake_expanduser


class TestDetectBrains:
    def test_detects_claude_via_directory(self, tmp_path: Path) -> None:
        """Claude Code detected when ~/.claude directory exists."""
        (tmp_path / ".claude").mkdir()

        with patch.object(Path, "expanduser", _make_fake_expanduser(tmp_path)):
            with patch("herder.init_cmd.shutil.which", return_value=None):
                found = detect_brains()

        assert "Claude Code" in found

    def test_detects_codex_via_directory(self, tmp_path: Path) -> None:
        """Codex detected when ~/.codex directory exists."""
        (tmp_path / ".codex").mkdir()

        with patch.object(Path, "expanduser", _make_fake_expanduser(tmp_path)):
            with patch("herder.init_cmd.shutil.which", return_value=None):
                found = detect_brains()

        assert "Codex" in found

    def test_detects_neither_when_absent(self, tmp_path: Path) -> None:
        """Returns empty list when neither brain directory nor binary is found."""
        # tmp_path has no .claude or .codex subdirectories; binaries absent.
        with patch.object(Path, "expanduser", _make_fake_expanduser(tmp_path)):
            with patch("herder.init_cmd.shutil.which", return_value=None):
                found = detect_brains()

        assert found == []

    def test_detects_via_binary_when_no_directory(self, tmp_path: Path) -> None:
        """Claude Code detected via shutil.which('claude') when dir is absent."""
        def _fake_which(binary: str) -> str | None:
            return "/usr/local/bin/claude" if binary == "claude" else None

        # tmp_path has no .claude or .codex dirs — detection via binary only.
        with patch.object(Path, "expanduser", _make_fake_expanduser(tmp_path)):
            with patch("herder.init_cmd.shutil.which", side_effect=_fake_which):
                found = detect_brains()

        assert "Claude Code" in found
        assert "Codex" not in found

    def test_detect_brains_does_not_crash_real(self, tmp_path: Path) -> None:
        """detect_brains() never raises — regression guard for the P1 Linux crash.

        Previously, using subprocess.run(["command", "-v", binary]) raised
        FileNotFoundError on Linux because 'command' is a shell builtin, not a
        PATH binary.  This test calls detect_brains() with home redirected to an
        empty directory and asserts it returns a list WITHOUT raising.
        """
        with patch.object(Path, "expanduser", _make_fake_expanduser(tmp_path)):
            result = detect_brains()

        assert isinstance(result, list), "detect_brains must return a list"


# ---------------------------------------------------------------------------
# write_intro_block (adapters)
# ---------------------------------------------------------------------------

class TestWriteIntroBlock:
    def test_creates_file_with_block_when_missing(self, tmp_path: Path) -> None:
        """Creates CLAUDE.md with managed block containing intro marker."""
        p = tmp_path / "CLAUDE.md"
        write_intro_block(p)
        content = p.read_text()
        assert BLOCK_START in content
        assert BLOCK_END in content
        assert _INTRO_PREFIX in content

    def test_idempotent_single_block_after_two_calls(self, tmp_path: Path) -> None:
        """Running write_intro_block twice produces exactly one managed block."""
        p = tmp_path / "CLAUDE.md"
        write_intro_block(p)
        write_intro_block(p)
        content = p.read_text()
        assert content.count(BLOCK_START) == 1
        assert content.count(BLOCK_END) == 1
        assert content.count(_INTRO_PREFIX) == 1

    def test_preserves_content_before_block(self, tmp_path: Path) -> None:
        """Existing content before the managed block is untouched."""
        p = tmp_path / "CLAUDE.md"
        header = "# My Project\n\nExisting notes.\n"
        p.write_text(header, encoding="utf-8")
        write_intro_block(p)
        content = p.read_text()
        assert content.startswith("# My Project")

    def test_appends_to_existing_file_without_block(self, tmp_path: Path) -> None:
        """Intro block is appended when file exists but has no managed block."""
        p = tmp_path / "AGENTS.md"
        p.write_text("# Agents\n", encoding="utf-8")
        write_intro_block(p)
        content = p.read_text()
        assert BLOCK_START in content
        assert _INTRO_PREFIX in content

    def test_does_not_duplicate_intro_when_block_already_has_it(self, tmp_path: Path) -> None:
        """If existing block already contains intro marker, nothing is added."""
        p = tmp_path / "CLAUDE.md"
        write_intro_block(p)
        original_content = p.read_text()
        write_intro_block(p)
        assert p.read_text() == original_content


# ---------------------------------------------------------------------------
# _wire_brain_targets
# ---------------------------------------------------------------------------

class TestWireBrainTargets:
    def test_creates_both_brain_files(self, tmp_path: Path) -> None:
        """CLAUDE.md and AGENTS.md are created next to config.yaml."""
        config_path = tmp_path / "config.yaml"
        _wire_brain_targets(config_path)
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "AGENTS.md").exists()

    def test_idempotent_two_calls(self, tmp_path: Path) -> None:
        """Second call to _wire_brain_targets produces no duplicate blocks."""
        config_path = tmp_path / "config.yaml"
        _wire_brain_targets(config_path)
        _wire_brain_targets(config_path)
        for name in ("CLAUDE.md", "AGENTS.md"):
            content = (tmp_path / name).read_text()
            assert content.count(BLOCK_START) == 1
            assert content.count(BLOCK_END) == 1


# ---------------------------------------------------------------------------
# init↔add coexistence
# ---------------------------------------------------------------------------

class TestInitAddCoexistence:
    def test_agent_line_survives_second_write_intro_block(self, tmp_path: Path) -> None:
        """Agent cheat-sheet line written by write_cheatsheet survives a second
        write_intro_block call (re-init must not cause data loss).

        Sequence:
          1. write_intro_block(p)      — first init: creates managed block
          2. write_cheatsheet(p, ...)  — add: inserts 'kiro' agent line
          3. write_intro_block(p)      — second init: must not wipe agent line

        Invariants asserted:
          - Exactly one BLOCK_START (no duplication)
          - Exactly one _INTRO_PREFIX (no duplication)
          - 'kiro' agent line still present (no data loss)
        """
        p = tmp_path / "CLAUDE.md"

        write_intro_block(p)
        write_cheatsheet(p, "kiro", "kiro do <task>")
        write_intro_block(p)

        content = p.read_text(encoding="utf-8")
        assert content.count(BLOCK_START) == 1, "Duplicate BLOCK_START after second init"
        assert content.count(_INTRO_PREFIX) == 1, "Duplicate _INTRO_PREFIX after second init"
        assert "kiro" in content, "Agent line 'kiro' was lost after second write_intro_block"


# ---------------------------------------------------------------------------
# cmd_init — end-to-end
# ---------------------------------------------------------------------------

class TestCmdInit:
    def test_init_creates_config_and_brain_files(self, tmp_path: Path) -> None:
        """Full init creates config.yaml + brain files with managed block."""
        _write_example(tmp_path)
        args = _make_args(config=str(tmp_path / "config.yaml"), yes=True)

        # Patch doctor to avoid real provider probes in unit tests
        with patch("herder.init_cmd._run_doctor_summary"):
            with patch("herder.init_cmd.detect_brains", return_value=[]):
                rc = cmd_init(args)

        assert rc == 0
        assert (tmp_path / "config.yaml").exists()
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "AGENTS.md").exists()

    def test_init_keeps_existing_config(self, tmp_path: Path) -> None:
        """init with existing config.yaml does not overwrite it."""
        _write_example(tmp_path)
        config_path = tmp_path / "config.yaml"
        original = "# custom config\nproviders: {}\n"
        config_path.write_text(original, encoding="utf-8")
        args = _make_args(config=str(config_path), yes=True)

        with patch("herder.init_cmd._run_doctor_summary"):
            with patch("herder.init_cmd.detect_brains", return_value=[]):
                rc = cmd_init(args)

        assert rc == 0
        assert config_path.read_text() == original, (
            "cmd_init must not overwrite an existing config.yaml"
        )

    def test_init_idempotent_no_duplicate_block(self, tmp_path: Path) -> None:
        """Running cmd_init twice does not create duplicate managed blocks."""
        _write_example(tmp_path)
        args = _make_args(config=str(tmp_path / "config.yaml"), yes=True)

        with patch("herder.init_cmd._run_doctor_summary"):
            with patch("herder.init_cmd.detect_brains", return_value=[]):
                cmd_init(args)
                cmd_init(args)

        for name in ("CLAUDE.md", "AGENTS.md"):
            content = (tmp_path / name).read_text()
            assert content.count(BLOCK_START) == 1, (
                f"{name} has duplicate managed block after two init runs"
            )

    def test_yes_flag_does_not_launch_add_wizard(self, tmp_path: Path) -> None:
        """--yes suppresses the interactive add wizard prompt."""
        _write_example(tmp_path)
        args = _make_args(config=str(tmp_path / "config.yaml"), yes=True)

        launched_add: list[bool] = []

        def _fake_prompt_add(a: argparse.Namespace, c: Path) -> None:
            launched_add.append(True)

        with patch("herder.init_cmd._run_doctor_summary"):
            with patch("herder.init_cmd.detect_brains", return_value=[]):
                with patch("herder.init_cmd._prompt_add_wizard", side_effect=_fake_prompt_add):
                    rc = cmd_init(args)

        assert rc == 0
        assert launched_add == [], "--yes must not trigger _prompt_add_wizard"

    def test_init_creates_config_from_bundled_when_no_local_example(
        self, tmp_path: Path
    ) -> None:
        """cmd_init succeeds using the bundled config.example.yaml when no local
        config.example.yaml is present in tmp_path."""
        args = _make_args(config=str(tmp_path / "config.yaml"), yes=True)
        # No local config.example.yaml — bundled fallback must be used.

        with patch("herder.init_cmd._run_doctor_summary"):
            with patch("herder.init_cmd.detect_brains", return_value=[]):
                rc = cmd_init(args)

        assert rc == 0
        config_path = tmp_path / "config.yaml"
        assert config_path.exists(), "config.yaml must be created via bundled template"
        assert config_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# install.sh — syntax + optional shellcheck
# ---------------------------------------------------------------------------

class TestInstallSh:
    INSTALL_SH = REPO_ROOT / "install.sh"

    def test_install_sh_exists(self) -> None:
        assert self.INSTALL_SH.exists(), "install.sh not found in repo root"

    def test_posix_syntax_check(self) -> None:
        """sh -n install.sh must pass (POSIX syntax check)."""
        result = subprocess.run(
            ["sh", "-n", str(self.INSTALL_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"sh -n install.sh failed:\n{result.stderr}"
        )

    @pytest.mark.skipif(
        shutil.which("shellcheck") is None,
        reason="shellcheck not installed",
    )
    def test_shellcheck_clean(self) -> None:
        """shellcheck install.sh must report zero findings."""
        result = subprocess.run(
            ["shellcheck", str(self.INSTALL_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"shellcheck found issues:\n{result.stdout}\n{result.stderr}"
        )
