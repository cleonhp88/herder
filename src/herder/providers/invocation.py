"""Builder that converts Provider + prompt into argv/stdin/file for invocation.

This is the SINGLE place where Provider configuration is converted into
concrete argv/stdin/prompt_file for execution. Both doctor and worker
adapters delegate here — they never build argv themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herder.config import Provider


@dataclass
class Invocation:
    """Prepared invocation ready for subprocess execution.

    Attributes:
        argv: Command-line arguments (program + flags + optional prompt).
        stdin: Prompt passed via stdin (if input mode is stdin).
        prompt_file: Path to prompt file (if input mode is file).
    """

    argv: list[str]
    stdin: str | None
    prompt_file: Path | None


def build_invocation(provider: Provider, prompt: str, work_dir: Path) -> Invocation:
    """Convert Provider + prompt into argv/stdin/file for execution.

    This centralizes the logic of how a Provider's input mode determines
    whether the prompt is passed via stdin, appended to argv, or written
    to a file.

    Args:
        provider: Provider configuration (executable, args, input mode).
        prompt: The full prompt/input text.
        work_dir: Working directory for file-based prompts.

    Returns:
        Invocation ready for subprocess execution.

    Raises:
        ValueError: If the input mode is unknown.
    """
    base_argv = [provider.executable, *provider.args]
    mode = provider.input

    if mode == "stdin":
        return Invocation(base_argv, stdin=prompt, prompt_file=None)

    if mode in ("arg", "arg_or_stdin"):
        # Prefer arg mode when available
        return Invocation([*base_argv, prompt], stdin=None, prompt_file=None)

    if mode == "file":
        prompt_file = work_dir / "invocation_prompt.txt"
        prompt_file.write_text(prompt)
        return Invocation([*base_argv, str(prompt_file)], stdin=None, prompt_file=prompt_file)

    raise ValueError(f"unknown input mode: {mode}")
