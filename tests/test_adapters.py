"""Tests for src/herder/adapters.py.

Covers: write_cheatsheet (create, append block, update existing line,
idempotency, content outside markers untouched) and default_brain_targets.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from herder.adapters import (
    BLOCK_END,
    BLOCK_START,
    MalformedCheatsheetError,
    default_brain_targets,
    validate_block,
    write_cheatsheet,
)


# ---------------------------------------------------------------------------
# write_cheatsheet — file creation
# ---------------------------------------------------------------------------

class TestWriteCheatsheetCreate:
    def test_creates_file_with_block_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        write_cheatsheet(p, "kiro", "kiro chat --no-interactive")
        content = p.read_text()
        assert BLOCK_START in content
        assert BLOCK_END in content
        assert "**kiro**" in content

    def test_creates_nested_parents(self, tmp_path: Path) -> None:
        p = tmp_path / "nested" / "deep" / "AGENTS.md"
        write_cheatsheet(p, "kiro", "kiro chat")
        assert p.exists()


# ---------------------------------------------------------------------------
# write_cheatsheet — existing file, no block yet
# ---------------------------------------------------------------------------

class TestWriteCheatsheetAppendBlock:
    def test_appends_block_to_existing_file_without_block(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        original = "# My Project\n\nSome existing content.\n"
        p.write_text(original)
        write_cheatsheet(p, "kiro", "kiro chat --no-interactive")
        content = p.read_text()
        assert content.startswith("# My Project")
        assert BLOCK_START in content
        assert "**kiro**" in content

    def test_does_not_modify_content_before_block(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        prefix = "# Header\n\nKeep this.\n"
        p.write_text(prefix)
        write_cheatsheet(p, "kiro", "kiro chat")
        content = p.read_text()
        assert content.startswith(prefix.rstrip())


# ---------------------------------------------------------------------------
# write_cheatsheet — idempotency
# ---------------------------------------------------------------------------

class TestWriteCheatsheetIdempotency:
    def test_second_call_does_not_duplicate_line(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        write_cheatsheet(p, "kiro", "kiro chat --no-interactive")
        write_cheatsheet(p, "kiro", "kiro chat --no-interactive")
        content = p.read_text()
        assert content.count("**kiro**") == 1

    def test_second_agent_added_without_duplicating_first(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        write_cheatsheet(p, "kiro", "kiro chat")
        write_cheatsheet(p, "codex", "codex exec")
        content = p.read_text()
        assert content.count("**kiro**") == 1
        assert content.count("**codex**") == 1

    def test_update_changes_usage_line(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        write_cheatsheet(p, "kiro", "kiro chat --old-flag")
        write_cheatsheet(p, "kiro", "kiro chat --new-flag")
        content = p.read_text()
        assert "--new-flag" in content
        assert "--old-flag" not in content
        assert content.count("**kiro**") == 1


# ---------------------------------------------------------------------------
# write_cheatsheet — content outside markers untouched
# ---------------------------------------------------------------------------

class TestWriteCheatsheetPreservesOuterContent:
    def test_content_before_block_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        before = "# Title\n\nSome text before.\n\n"
        after = "\n## Section After\nMore text.\n"
        p.write_text(before + BLOCK_START + "\n" + BLOCK_END + after)
        write_cheatsheet(p, "kiro", "kiro chat")
        content = p.read_text()
        assert content.startswith(before)

    def test_content_after_block_preserved(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        after_text = "\n## After Section\nShould survive.\n"
        p.write_text(f"{BLOCK_START}\n{BLOCK_END}{after_text}")
        write_cheatsheet(p, "kiro", "kiro chat")
        content = p.read_text()
        assert content.endswith(after_text)

    def test_only_one_block_start_and_end_after_multiple_writes(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        p.write_text("# Header\n")
        write_cheatsheet(p, "kiro", "kiro chat")
        write_cheatsheet(p, "codex", "codex exec")
        write_cheatsheet(p, "kiro", "kiro chat --updated")
        content = p.read_text()
        assert content.count(BLOCK_START) == 1
        assert content.count(BLOCK_END) == 1


# ---------------------------------------------------------------------------
# default_brain_targets
# ---------------------------------------------------------------------------

class TestDefaultBrainTargets:
    def test_returns_project_local_files(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        targets = default_brain_targets(config_path)
        assert len(targets) == 2
        names = {t.name for t in targets}
        assert "CLAUDE.md" in names
        assert "AGENTS.md" in names

    def test_targets_are_in_same_dir_as_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        targets = default_brain_targets(config_path)
        for t in targets:
            assert t.parent == tmp_path.resolve()

    def test_does_not_include_global_claude_md(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        targets = default_brain_targets(config_path)
        home = Path.home()
        for t in targets:
            assert not str(t).startswith(str(home / ".claude")), (
                "global ~/.claude/CLAUDE.md must NOT be in default targets"
            )


# ---------------------------------------------------------------------------
# validate_block + write_cheatsheet — malformed marker tests (P1)
# ---------------------------------------------------------------------------

class TestValidateBlock:
    """validate_block raises MalformedCheatsheetError on invalid states."""

    def test_zero_markers_is_valid(self) -> None:
        """No markers = fresh file — must not raise."""
        validate_block("# Header\n\nSome content.\n")

    def test_valid_one_block_is_accepted(self) -> None:
        content = f"before\n{BLOCK_START}\n- **kiro**: `kiro run`\n{BLOCK_END}\nafter\n"
        validate_block(content)  # must not raise

    def test_missing_end_raises(self) -> None:
        content = f"# Header\n{BLOCK_START}\n- **kiro**: `kiro run`\n"
        with pytest.raises(MalformedCheatsheetError, match="without a matching"):
            validate_block(content)

    def test_end_before_start_raises(self) -> None:
        content = f"{BLOCK_END}\n{BLOCK_START}\n- **kiro**: `kiro run`\n"
        with pytest.raises(MalformedCheatsheetError, match="before"):
            validate_block(content)

    def test_duplicate_start_raises(self) -> None:
        content = (
            f"{BLOCK_START}\n- **a**: `a`\n{BLOCK_END}\n"
            f"{BLOCK_START}\n- **b**: `b`\n{BLOCK_END}\n"
        )
        with pytest.raises(MalformedCheatsheetError, match="Duplicate"):
            validate_block(content)

    def test_missing_start_end_only_raises(self) -> None:
        content = f"Some text\n{BLOCK_END}\nMore text\n"
        with pytest.raises(MalformedCheatsheetError):
            validate_block(content)


class TestWriteCheatsheetMalformed:
    """write_cheatsheet raises MalformedCheatsheetError and writes NOTHING."""

    def test_missing_end_raises_no_write(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        original = f"# Header\n{BLOCK_START}\n- **kiro**: `kiro run`\n"
        p.write_text(original)
        with pytest.raises(MalformedCheatsheetError):
            write_cheatsheet(p, "codex", "codex exec")
        # File must be unmodified
        assert p.read_text() == original

    def test_end_before_start_raises_no_write(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        original = f"{BLOCK_END}\n{BLOCK_START}\n- **kiro**: `kiro run`\n"
        p.write_text(original)
        with pytest.raises(MalformedCheatsheetError):
            write_cheatsheet(p, "codex", "codex exec")
        assert p.read_text() == original

    def test_duplicate_block_raises_no_write(self, tmp_path: Path) -> None:
        p = tmp_path / "CLAUDE.md"
        original = (
            f"{BLOCK_START}\n- **a**: `a`\n{BLOCK_END}\n"
            f"{BLOCK_START}\n- **b**: `b`\n{BLOCK_END}\n"
        )
        p.write_text(original)
        with pytest.raises(MalformedCheatsheetError):
            write_cheatsheet(p, "codex", "codex exec")
        assert p.read_text() == original

    def test_zero_markers_appends_fresh_block(self, tmp_path: Path) -> None:
        """Zero markers is valid — a fresh block is appended, content preserved."""
        p = tmp_path / "CLAUDE.md"
        header = "# My Project\n\nSome existing notes.\n"
        p.write_text(header)
        write_cheatsheet(p, "kiro", "kiro run")
        content = p.read_text()
        assert "# My Project" in content
        assert BLOCK_START in content
        assert BLOCK_END in content
        assert "**kiro**" in content

    def test_one_good_block_updates_in_place_no_dup(self, tmp_path: Path) -> None:
        """One well-formed block is updated in place without creating a second block."""
        p = tmp_path / "CLAUDE.md"
        outer_before = "# Header\n\n"
        outer_after = "\n## Section After\n"
        p.write_text(
            outer_before
            + f"{BLOCK_START}\n- **kiro**: `kiro old`\n{BLOCK_END}"
            + outer_after
        )
        write_cheatsheet(p, "kiro", "kiro new")
        content = p.read_text()
        assert content.count(BLOCK_START) == 1
        assert content.count(BLOCK_END) == 1
        assert "kiro new" in content
        assert "kiro old" not in content
        assert content.startswith(outer_before)
        assert content.endswith(outer_after)
