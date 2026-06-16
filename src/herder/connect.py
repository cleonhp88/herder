"""Agent Connect — recipe-driven onboarding of AI-agent hands.

Orchestrates detect → install → login → verify → register for a given
recipe YAML. All side-effectful steps require explicit confirmation via a
callable inject so the flow is fully testable without network or TTY.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML

from herder.adapters import validate_block, write_cheatsheet


# ---------------------------------------------------------------------------
# Recipe models
# ---------------------------------------------------------------------------

class RecipeProvider(BaseModel):
    """Provider block nested inside a recipe.

    Args:
        type: Provider type (cli / api / ollama / acp).
        executable: Binary name or path.
        args: Fixed CLI arguments passed after the executable.
        parser: Output parser name (default "text").
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    executable: str
    args: list[str] = []
    parser: str = "text"


class Recipe(BaseModel):
    """Data recipe for a single agent hand.

    Args:
        name: Short identifier (e.g. "kiro").
        detect: Shell command whose exit-0 means the agent is installed.
        install: Shell command to install; always confirmed before running.
        login: Shell command to authenticate; always confirmed before running.
        verify: Shell command to confirm the agent is usable after install.
        provider: Provider registration block.
        default_role: Role name to create / ensure in config.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    detect: str
    install: str
    login: str
    verify: str
    provider: RecipeProvider
    default_role: str


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ConnectResult:
    """Outcome of a connect() run.

    Attributes:
        success: True when registration completed without abort.
        provider_name: Key written into config.yaml (e.g. "kiro_cli").
        role_name: Role key written into config.yaml.
        files_updated: Absolute paths of brain files that were updated.
        skipped_install: True when detect found the agent already installed.
        abort_reason: Non-empty string when success is False.
    """

    success: bool
    provider_name: str = ""
    role_name: str = ""
    files_updated: list[Path] = field(default_factory=list)
    skipped_install: bool = False
    abort_reason: str = ""


# ---------------------------------------------------------------------------
# Confirm callables
# ---------------------------------------------------------------------------

def _interactive_confirm(prompt: str) -> bool:
    """Ask the user y/N interactively via stdin.

    Args:
        prompt: Message shown before [y/N].

    Returns:
        True only when user types 'y' or 'Y'.
    """
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "y"


def auto_confirm(_prompt: str) -> bool:
    """Non-interactive confirm that always returns True.

    Use only in tests or with --yes flag. Never use in production paths
    without explicit user opt-in.

    Args:
        _prompt: Ignored.

    Returns:
        True always.
    """
    return True


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------

def _is_url_install(command: str) -> bool:
    """Return True when command contains an HTTP/HTTPS URL.

    Detects any shell pattern that downloads content from the network
    (``curl … | sh``, ``bash <(curl …)``, ``sh -c "$(curl …)"``,
    two-step downloaders, etc.) by looking for the URL scheme rather than
    specific shell syntax, which cannot be reliably pattern-matched.

    Args:
        command: Shell command string.

    Returns:
        True when ``http://`` or ``https://`` appears in the command.
    """
    return "http://" in command or "https://" in command


def run_step(
    label: str,
    command: str,
    *,
    confirm: Callable[[str], bool],
    runner: object = subprocess,
) -> tuple[bool, str]:
    """Print, confirm, and optionally run a shell command.

    Confirmation is mandatory before execution. If the user declines,
    the command is NOT run and (False, "") is returned.

    Args:
        label: Human-readable step name shown in output.
        command: Shell command to execute.
        confirm: Callable(prompt) -> bool; called before any execution.
        runner: Object with a ``run`` method matching subprocess.run API.
                Injected for tests.

    Returns:
        Tuple (ok, output) — ok=True and output=stdout on success;
        ok=False and output="" on declined confirm or non-zero exit.
    """
    print(f"\n[{label}] {command}")

    if _is_url_install(command):
        domain = _extract_domain(command)
        prompt = (
            f"This downloads and runs content from {domain}. "
            f"Confirm? "
        )
    else:
        prompt = "Run the above command?"

    if not confirm(prompt):
        print("  skipped (user declined)")
        return False, ""

    result = runner.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  FAILED (exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
        return False, result.stderr.strip()

    output = result.stdout.strip()
    if output:
        print(f"  ok: {output[:120]}")
    else:
        print("  ok")
    return True, output


def _extract_domain(command: str) -> str:
    """Extract a URL domain from a shell command string for display.

    Args:
        command: Shell command text.

    Returns:
        Domain string, or "<unknown>" if not found.
    """
    import re
    match = re.search(r"https?://([^/\s]+)", command)
    return match.group(1) if match else "<unknown>"


# ---------------------------------------------------------------------------
# Detect helper
# ---------------------------------------------------------------------------

def detect(recipe: Recipe, *, runner: object = subprocess) -> bool:
    """Check whether the agent binary is already installed.

    Args:
        recipe: Loaded recipe.
        runner: Subprocess-compatible runner (injectable for tests).

    Returns:
        True when detect command exits 0.
    """
    result = runner.run(
        recipe.detect,
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Config writer
# ---------------------------------------------------------------------------

def register_provider(config_path: Path, recipe: Recipe) -> tuple[str, str]:
    """Idempotently add provider + role into config.yaml.

    Uses ruamel.yaml round-trip mode to preserve all comments, ordering,
    and flow-style formatting in the user's hand-tuned config.  If the
    provider or role already exists, the existing definition is NOT
    overwritten (idempotent).  The role is also added to every project's
    ``allowed_roles`` list if not already present.

    Writes atomically via a temporary file + os.replace to avoid partial
    writes on failure.

    Args:
        config_path: Absolute path to config.yaml.
        recipe: Recipe whose provider block and default_role are registered.

    Returns:
        Tuple (provider_name, role_name) of the registered keys.

    Raises:
        OSError: If config_path cannot be read or written.
    """
    provider_name = f"{recipe.name}_cli"
    role_name = recipe.default_role

    rt_yaml = YAML(typ="rt")
    rt_yaml.preserve_quotes = True
    rt_yaml.default_flow_style = False

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            raw = rt_yaml.load(f) or {}
    else:
        raw = {}

    # --- providers ---
    if "providers" not in raw:
        raw["providers"] = {}
    providers = raw["providers"]
    if provider_name not in providers:
        providers[provider_name] = {
            "type": recipe.provider.type,
            "executable": recipe.provider.executable,
            "args": list(recipe.provider.args),
            "parser": recipe.provider.parser,
        }

    # --- roles ---
    if "roles" not in raw:
        raw["roles"] = {}
    roles = raw["roles"]
    if role_name not in roles:
        roles[role_name] = {"provider": provider_name, "permissions": "read_only"}

    # --- allowed_roles in every project ---
    projects = raw.get("projects", {}) or {}
    for _proj_name, proj in projects.items():
        if isinstance(proj, dict):
            if "allowed_roles" not in proj:
                proj["allowed_roles"] = []
            allowed = proj["allowed_roles"]
            if role_name not in allowed:
                allowed.append(role_name)

    # Atomic write: write to a sibling temp file, then os.replace
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent,
        prefix=".config_tmp_",
        suffix=".yaml",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            rt_yaml.dump(raw, f)
        os.replace(tmp_path, config_path)
    except Exception:
        # Clean up temp file if replace failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return provider_name, role_name


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def connect(
    recipe: Recipe,
    *,
    config_path: Path,
    brain_files: list[Path],
    confirm: Callable[[str], bool] = _interactive_confirm,
    runner: object = subprocess,
) -> ConnectResult:
    """Orchestrate detect → install → login → verify → register.

    Steps are gated by ``confirm`` (install and login only; verify is a
    read-only probe and runs un-gated).  Any declined step or failed command
    aborts the flow; registration is NOT written on partial success.

    Pre-flight: before any write, validates all brain file targets for
    marker integrity.  If any target is malformed the entire operation is
    aborted with a typed error and no files are modified (all-or-nothing).

    Args:
        recipe: Loaded and validated recipe.
        config_path: Path to config.yaml to update on registration.
        brain_files: List of markdown files (CLAUDE.md, AGENTS.md) to update
                     with a cheat-sheet line.
        confirm: Callable(prompt) -> bool used for install and login steps.
        runner: Subprocess-compatible runner (injectable for tests).

    Returns:
        ConnectResult describing outcome.

    Raises:
        MalformedCheatsheetError: If any brain file has malformed block
            markers. No file is written when this is raised.
    """
    # --- Pre-flight: validate all brain targets BEFORE any write ---
    for brain_file in brain_files:
        if brain_file.exists():
            content = brain_file.read_text(encoding="utf-8")
            validate_block(content)  # raises MalformedCheatsheetError if bad

    # --- Detect / install ---
    already_installed = detect(recipe, runner=runner)

    if already_installed:
        print(f"[detect] {recipe.name} already installed — skipping install")
    else:
        ok, _ = run_step("install", recipe.install, confirm=confirm, runner=runner)
        if not ok:
            return ConnectResult(
                success=False,
                abort_reason="install step declined or failed",
            )

    # --- Login (confirm-gated) ---
    ok, _ = run_step("login", recipe.login, confirm=confirm, runner=runner)
    if not ok:
        return ConnectResult(
            success=False,
            abort_reason="login step declined or failed",
        )

    # --- Verify (read-only probe — NOT confirm-gated) ---
    result = runner.run(  # type: ignore[union-attr]
        recipe.verify,
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  [verify] FAILED (exit {result.returncode}): {result.stderr.strip()}",
            file=sys.stderr,
        )
        return ConnectResult(
            success=False,
            abort_reason="verify step failed",
        )
    verify_out = result.stdout.strip()
    print(f"[verify] {verify_out[:120]}" if verify_out else "[verify] ok")

    # --- All checks passed — commit writes ---
    provider_name, role_name = register_provider(config_path, recipe)

    usage_line = f"{recipe.provider.executable} {' '.join(recipe.provider.args)}"
    updated: list[Path] = []
    for brain_file in brain_files:
        write_cheatsheet(brain_file, recipe.name, usage_line)
        updated.append(brain_file)

    return ConnectResult(
        success=True,
        provider_name=provider_name,
        role_name=role_name,
        files_updated=updated,
        skipped_install=already_installed,
    )


# ---------------------------------------------------------------------------
# Recipe loader
# ---------------------------------------------------------------------------

def load_recipe(path: str | Path) -> Recipe:
    """Load and validate a recipe YAML file.

    Args:
        path: Absolute or relative path to the recipe YAML (str or Path).

    Returns:
        Validated Recipe instance.

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If the file is missing required fields or is malformed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"recipe not found: {path}")

    with open(path) as f:
        try:
            data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"malformed YAML in {path}: {exc}") from exc

    try:
        return Recipe(**data)
    except ValidationError as exc:
        raise ValueError(f"invalid recipe {path}:\n{exc}") from exc
