"""Seatbelt sandbox enforcement for untrusted jobs on macOS.

Provides fail-closed macOS seatbelt (sandbox-exec) profile generation and execution wrapping
for jobs with network denied or filesystem constrained. On unsupported platforms, raises an error
to refuse silent unconfined execution.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def is_supported() -> bool:
    """Check if seatbelt (sandbox-exec) is available on this platform.

    Returns True only on macOS with sandbox-exec in PATH.

    Returns:
        True if macOS and sandbox-exec is available; False otherwise.
    """
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def build_profile(*, allow_write: list[Path], deny_network: bool) -> str:
    """Generate a seatbelt (SBPL) profile for job confinement.

    Base policy: allow default (all operations allowed).
    Restrictions applied:
    - If deny_network: add (deny network*)
    - Always: deny file-write* except in allow_write subpaths and /dev (for ttys)

    Args:
        allow_write: List of Path objects where file writes are allowed (will be resolved to absolute paths).
        deny_network: If True, deny all network operations via (deny network*).

    Returns:
        A valid SBPL (Sandbox Profile Language) string.
    """
    lines = ["(version 1)", "(allow default)"]

    if deny_network:
        lines.append("(deny network*)")

    # Deny all writes except in allow_write and /dev
    lines.append("(deny file-write*)")
    for path in allow_write:
        resolved = str(Path(path).resolve())
        lines.append(f'(allow file-write* (subpath "{resolved}"))')

    # Allow writes to /dev for ttys, etc.
    lines.append('(allow file-write* (subpath "/dev"))')

    return "\n".join(lines)


def wrap(argv: list[str], profile: str) -> list[str]:
    """Wrap a command argv with sandbox-exec to run under the given profile.

    Args:
        argv: Original command argv.
        profile: SBPL profile string (inline, passed with -p flag).

    Returns:
        Modified argv with ["sandbox-exec", "-p", profile, *argv].
    """
    return ["sandbox-exec", "-p", profile, *argv]
