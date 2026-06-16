"""Herder init command — guided first-run setup.

Walks a new user through:
  1. Copying config.example.yaml → config.yaml (idempotent).
  2. Detecting installed brains (Claude Code, Codex).
  3. Writing an intro managed block to each brain target file.
  4. Running doctor to confirm provider readiness.
  5. Optionally chaining into the `add` wizard.

Re-running init is safe: config is never overwritten, the managed block is
never duplicated.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from herder.adapters import (
    MalformedCheatsheetError,
    default_brain_targets,
    write_intro_block,
)
from herder.resources import resolve_config_example, resolve_recipes_dir


# ---------------------------------------------------------------------------
# Brain detection
# ---------------------------------------------------------------------------

_BRAINS: list[tuple[str, str, str]] = [
    ("Claude Code", "~/.claude", "claude"),
    ("Codex", "~/.codex", "codex"),
]


def detect_brains() -> list[str]:
    """Return names of detected brains (Claude Code / Codex).

    A brain is considered present when its config directory exists under HOME
    OR its CLI binary is on PATH.  Both conditions are checked so detection
    works regardless of install method.

    Returns:
        List of brain display names that are detected.
    """
    found: list[str] = []
    for display_name, dir_suffix, binary in _BRAINS:
        dir_path = Path(dir_suffix).expanduser()
        has_dir = dir_path.exists()
        has_bin = shutil.which(binary) is not None
        if has_dir or has_bin:
            found.append(display_name)
    return found


# ---------------------------------------------------------------------------
# Config bootstrap
# ---------------------------------------------------------------------------

def _resolve_config(config_arg: str) -> Path:
    """Resolve and return the config path as an absolute Path.

    Args:
        config_arg: Raw value of --config CLI argument.

    Returns:
        Resolved absolute Path to config.yaml target.
    """
    return Path(config_arg).resolve()


def _bootstrap_config(config_path: Path) -> bool:
    """Copy config.example.yaml → config.yaml if config does not yet exist.

    Locates config.example.yaml via ``resolve_config_example``: first checks
    next to config_path, then falls back to the template bundled inside the
    installed package.  If config already exists, does nothing (idempotent).

    Args:
        config_path: Target path for config.yaml.

    Returns:
        True when a new config was created, False when it already existed.

    Raises:
        FileNotFoundError: If neither a local nor a bundled config.example.yaml
            can be found (should not occur in a correctly installed package).
    """
    if config_path.exists():
        print(f"config.yaml already exists at {config_path}, keeping it")
        return False

    example = resolve_config_example(config_path.parent)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(example, config_path)
    print(f"created config.yaml at {config_path}")
    return True


# ---------------------------------------------------------------------------
# Brain wiring
# ---------------------------------------------------------------------------

def _wire_brain_targets(config_path: Path) -> list[Path]:
    """Write an intro block to each project-local brain target file.

    Uses default_brain_targets() to compute CLAUDE.md / AGENTS.md paths next
    to config.yaml (never touches global ~/.claude/CLAUDE.md).  The write is
    idempotent: re-running produces no duplicate blocks.

    Args:
        config_path: Resolved path to config.yaml (used to locate brain files).

    Returns:
        List of Path objects that were processed (regardless of changes made).

    Raises:
        MalformedCheatsheetError: If any existing brain file has broken markers.
    """
    targets = default_brain_targets(config_path)
    for target in targets:
        existed = target.exists()
        write_intro_block(target)
        print(f"  {target.name}: {'updated' if existed else 'created'}")
    return targets


# ---------------------------------------------------------------------------
# Doctor summary (reuses cmd_doctor logic)
# ---------------------------------------------------------------------------

def _run_doctor_summary(config_path: Path) -> None:
    """Run doctor probe and print provider readiness.

    Loads config from config_path, runs provider health checks, and prints a
    one-line summary per provider plus the pass/fail count.  Errors during
    doctor (e.g. missing provider binaries) are printed but do not abort init.

    Args:
        config_path: Path to config.yaml to load for doctor probes.
    """
    try:
        from herder.config import load_config
        from herder.db.store import Store
        from herder.services.doctor import run_doctor

        cfg = load_config(str(config_path))
        report = run_doctor(cfg, Store.open(), Path.cwd(), config_path=str(config_path))
        for h in report.rows:
            flag = "ok" if h.noninteractive_status == "ok" else "WARN"
            print(f"  {h.provider:14} {flag}")
        print(f"  {report.ok_count}/{len(report.rows)} providers ready")
    except Exception as exc:  # noqa: BLE001 — doctor failure is non-fatal during init
        print(f"  doctor check skipped: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    """Execute the 'init' subcommand — guided first-run setup.

    Steps (each idempotent, never destructive):
      1. Bootstrap config.yaml from config.example.yaml if absent.
      2. Detect installed brains (Claude Code, Codex).
      3. Wire an intro managed block to each project-local brain target file.
      4. Run doctor summary to show provider readiness.
      5. Optionally chain into the interactive 'add' wizard.

    Args:
        args: Parsed CLI arguments.  Expects: args.config (str), args.yes (bool).

    Returns:
        0 on success, 1 on unrecoverable error.
    """
    print("=== Herder init ===")

    # Step 1: Config
    config_path = _resolve_config(args.config)
    try:
        _bootstrap_config(config_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Step 2: Brain detection
    brains = detect_brains()
    if brains:
        print(f"brains detected: {', '.join(brains)}")
    else:
        print(
            "note: no brain detected (Claude Code or Codex).\n"
            "Install one first: https://claude.ai/download  or  https://github.com/openai/codex"
        )

    # Step 3: Wire cheat-sheet intro block to brain target files
    print("wiring brain target files:")
    try:
        _wire_brain_targets(config_path)
    except MalformedCheatsheetError as exc:
        print(f"error: brain file has malformed markers — {exc}", file=sys.stderr)
        return 1

    # Step 4: Doctor summary
    print("provider readiness:")
    _run_doctor_summary(config_path)

    # Step 5: Chain into add wizard (unless --yes suppresses interactive prompts)
    if not args.yes:
        _prompt_add_wizard(args, config_path)

    print("\nDone. Run 'herder add' to connect more hands.")
    return 0


def _prompt_add_wizard(args: argparse.Namespace, config_path: Path) -> None:
    """Ask the user if they want to run 'herder add' now.

    In --yes / non-interactive mode this prompt is skipped entirely.

    Args:
        args: Parsed CLI namespace (used for recipes_dir if available).
        config_path: Resolved config path (used to build a mock namespace).
    """
    try:
        answer = input("\nAdd your first hand now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if answer in ("", "y"):
        from herder.cli import _interactive_confirm_cli, _show_recipe_menu

        recipes_dir = resolve_recipes_dir(getattr(args, "recipes_dir", None))
        _show_recipe_menu(recipes_dir, config_path, _interactive_confirm_cli)
