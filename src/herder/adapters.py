"""Brain-file adapters — idempotent markdown management.

Maintains a marked managed block in CLAUDE.md / AGENTS.md so that
herder can add / update agent cheat-sheet lines without ever touching
content outside the managed region.
"""
from __future__ import annotations

from pathlib import Path

BLOCK_START = "<!-- herder:hands START -->"
BLOCK_END = "<!-- herder:hands END -->"


class MalformedCheatsheetError(ValueError):
    """Raised when the managed block markers are in an invalid state.

    Invalid states include:
    - BLOCK_START present but BLOCK_END missing.
    - BLOCK_END appears before BLOCK_START.
    - Duplicate blocks (more than one START or END marker).
    """


def validate_block(content: str) -> None:
    """Validate the managed block markers in a file's content.

    Zero markers (no block yet) is valid — a fresh block will be appended.
    Exactly one START followed by one END is valid.

    Args:
        content: Full text of the file to validate.

    Raises:
        MalformedCheatsheetError: If markers are in an invalid state.
    """
    start_count = content.count(BLOCK_START)
    end_count = content.count(BLOCK_END)

    if start_count == 0 and end_count == 0:
        return  # no block yet — fresh append will be used

    if start_count > 1 or end_count > 1:
        raise MalformedCheatsheetError(
            f"Duplicate managed block markers detected "
            f"(START×{start_count}, END×{end_count}). "
            "Manual repair required."
        )

    if start_count == 1 and end_count == 0:
        raise MalformedCheatsheetError(
            f"Found {BLOCK_START!r} without a matching {BLOCK_END!r}. "
            "File appears truncated or hand-edited. Manual repair required."
        )

    if start_count == 0 and end_count == 1:
        raise MalformedCheatsheetError(
            f"Found {BLOCK_END!r} without a preceding {BLOCK_START!r}. "
            "Manual repair required."
        )

    # Both count == 1 — verify ordering
    if content.index(BLOCK_END) < content.index(BLOCK_START):
        raise MalformedCheatsheetError(
            f"{BLOCK_END!r} appears before {BLOCK_START!r}. "
            "Manual repair required."
        )


def _build_line(agent_name: str, usage_line: str) -> str:
    """Format a single cheat-sheet entry.

    Args:
        agent_name: Short name, e.g. "kiro".
        usage_line: Invocation hint, e.g. "kiro chat --no-interactive".

    Returns:
        Markdown list item string (no trailing newline).
    """
    return f"- **{agent_name}**: `{usage_line}`"


def write_cheatsheet(path: Path, agent_name: str, usage_line: str) -> None:
    """Idempotently add or update an agent line in the managed block.

    Behaviour:
    - If the file does not exist, creates it with the managed block.
    - If the file exists but has no managed block, appends the block at end.
    - If the managed block exists, adds the agent line inside it (or updates
      the existing line for this agent — no duplicate).
    - Content outside the markers is NEVER modified.

    Args:
        path: Absolute path to CLAUDE.md or AGENTS.md.
        agent_name: Short name used as the key for idempotency.
        usage_line: Invocation hint written next to the name.

    Raises:
        MalformedCheatsheetError: If existing block markers are malformed
            (missing END, END before START, or duplicate markers). No write
            is performed when this error is raised.
    """
    new_entry = _build_line(agent_name, usage_line)
    prefix = f"- **{agent_name}**:"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{BLOCK_START}\n{new_entry}\n{BLOCK_END}\n",
            encoding="utf-8",
        )
        return

    original = path.read_text(encoding="utf-8")

    # Validate before any write — raises MalformedCheatsheetError on bad state
    validate_block(original)

    if BLOCK_START not in original:
        # Append block at end; ensure clean newline boundary
        separator = "" if original.endswith("\n") or not original else "\n"
        path.write_text(
            original + separator + f"\n{BLOCK_START}\n{new_entry}\n{BLOCK_END}\n",
            encoding="utf-8",
        )
        return

    # Block exists and validated — update or insert the agent line inside it
    before, after_start = original.split(BLOCK_START, 1)
    block_body, after_end = after_start.split(BLOCK_END, 1)

    lines = block_body.split("\n")
    updated_lines: list[str] = []
    agent_seen = False

    for line in lines:
        if line.startswith(prefix):
            updated_lines.append(new_entry)
            agent_seen = True
        else:
            updated_lines.append(line)

    if not agent_seen:
        # Insert before the last blank line (which precedes BLOCK_END)
        insert_at = len(updated_lines)
        while insert_at > 0 and updated_lines[insert_at - 1].strip() == "":
            insert_at -= 1
        updated_lines.insert(insert_at, new_entry)

    new_block_body = "\n".join(updated_lines)
    path.write_text(
        before + BLOCK_START + new_block_body + BLOCK_END + after_end,
        encoding="utf-8",
    )


_INTRO_PREFIX = "<!-- herder:intro -->"


def write_intro_block(path: Path) -> None:
    """Idempotently write an Herder intro header into the managed block.

    Ensures the managed block exists and contains an intro paragraph that
    describes Herder to the brain (Claude Code / Codex).  If the block
    already contains the intro prefix marker, the block is not modified
    (idempotent).  Agent cheat-sheet lines written later by write_cheatsheet()
    are appended inside the same block as usual.

    Uses the same ``<!-- herder:hands START/END -->`` markers so that the
    intro and agent lines coexist in a single managed section.

    Args:
        path: Absolute path to CLAUDE.md or AGENTS.md.

    Raises:
        MalformedCheatsheetError: If existing block markers are malformed.
    """
    intro_body = (
        f"{_INTRO_PREFIX}\n"
        "## Herder — connected hands\n\n"
        "You are the brain. Herder provides hands: specialist agents that\n"
        "execute tasks on your behalf. Connect a hand: `herder add`.\n"
        "Enqueue work: `herder enqueue --role <role> --prompt-file <f>`.\n"
        "Run pending jobs: `herder worker --once`.\n"
    )

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{BLOCK_START}\n{intro_body}\n{BLOCK_END}\n",
            encoding="utf-8",
        )
        return

    original = path.read_text(encoding="utf-8")
    validate_block(original)  # raises MalformedCheatsheetError if bad

    if BLOCK_START not in original:
        separator = "" if original.endswith("\n") or not original else "\n"
        path.write_text(
            original + separator + f"\n{BLOCK_START}\n{intro_body}\n{BLOCK_END}\n",
            encoding="utf-8",
        )
        return

    # Block exists — check if intro is already present; if so, skip (idempotent)
    before, after_start = original.split(BLOCK_START, 1)
    block_body, after_end = after_start.split(BLOCK_END, 1)

    if _INTRO_PREFIX in block_body:
        return  # already written — nothing to do

    # Prepend intro to the existing block body
    new_block_body = f"\n{intro_body}{block_body}"
    path.write_text(
        before + BLOCK_START + new_block_body + BLOCK_END + after_end,
        encoding="utf-8",
    )


def default_brain_targets(config_path: Path) -> list[Path]:
    """Return project-local CLAUDE.md and AGENTS.md next to config.yaml.

    These are project-local files only. The global ~/.claude/CLAUDE.md is
    intentionally excluded to avoid clobbering the user's global file.

    Args:
        config_path: Absolute path to the project's config.yaml.

    Returns:
        List of Path objects for CLAUDE.md and AGENTS.md in the same
        directory as config_path.
    """
    project_root = config_path.resolve().parent
    return [
        project_root / "CLAUDE.md",
        project_root / "AGENTS.md",
    ]
