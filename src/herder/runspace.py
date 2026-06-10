"""Run directory and prompt snapshot management."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from herder import paths


# FIX 3: Git safe configuration — disable hooks, fsmonitor, and dangerous configs
# to prevent trojan/attack via untrusted repos
_GIT_SAFE = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",           # Disable filesystem monitor
    "-c", "core.fsmonitor=false",      # Explicit disable (belt & suspenders)
    "-c", "core.sshCommand=",          # Disable custom ssh (potential RCE)
    "-c", "core.pager=cat",            # Safe pager
    "-c", "core.editor=true",          # Safe editor (no-op)
    "-c", "core.askpass=",             # Disable credential prompt
    "-c", "protocol.ext.allow=never",  # Disable external protocol handlers
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git command with hooks disabled, system config isolation, and hardening.

    Disables dangerous git features (fsmonitor, ssh, external protocols) to
    prevent trojan/RCE attacks via untrusted repositories or malicious config.

    Args:
        repo: Repository root path.
        args: Git command arguments.

    Returns:
        Completed process result.

    Raises:
        subprocess.CalledProcessError: If git command fails.
    """
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",  # Ignore system git config
        "GIT_CONFIG_GLOBAL": "/dev/null",  # Ignore user git config
        "GIT_TERMINAL_PROMPT": "0",  # Disable credential prompt
        "GIT_ALLOW_PROTOCOL": "file:https:ssh",  # Allowlist safe protocols
    }
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_SAFE, *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def create_run_dir(job_id: str) -> Path:
    """Create a run directory with standard layout.

    Args:
        job_id: Identifier for the run (e.g., "job_X").

    Returns:
        Path to the created run directory.
    """
    rd = paths.runs_dir() / job_id
    (rd / "artifacts").mkdir(parents=True, exist_ok=True)
    # FIX 4: Explicit mode (owner-only) on run directory
    try:
        os.chmod(rd, 0o700)
    except OSError:
        pass
    return rd


def snapshot_prompt(run_dir: Path, prompt: str) -> tuple[Path, str]:
    """Write prompt to file and return path with SHA256 digest.

    Args:
        run_dir: Path to the run directory.
        prompt: Prompt text to snapshot.

    Returns:
        Tuple of (prompt_file_path, sha256_digest).
    """
    path = run_dir / "prompt.md"
    path.write_text(prompt, encoding="utf-8")
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return path, digest


def write_result_md(run_dir: Path, frontmatter: dict, body: str) -> Path:
    """Write result.md with a simple YAML frontmatter block + body (UTF-8, Obsidian-ready).

    Args:
        run_dir: Path to the run directory.
        frontmatter: Dictionary of frontmatter key-value pairs.
        body: Body text content.

    Returns:
        Path to the created result.md file.
    """
    lines = ["---"]
    for k, v in frontmatter.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path = run_dir / "result.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_worktree(repo_root: Path, job_id: str) -> Path:
    """Create (or reuse) an isolated git worktree for a job (branch herder/<job_id>).

    The agent works on this copy; the real repo's checked-out branch is untouched.
    Idempotent: on retry attempts the existing worktree is reused so the second
    attempt does not collide on the path/branch.

    Git hooks are disabled (core.hooksPath=/dev/null) to prevent trojan/attack
    via malicious hooks in untrusted repositories.

    NOTE (deferred, tracked): worktrees + herder/* branches are never garbage-
    collected in v1 — litter accumulates in the real repo's refs until a future
    `herder gc` implements cleanup.

    Args:
        repo_root: Root of the git repository.
        job_id: Unique job identifier (used in branch name).

    Returns:
        Path to the created (or reused) worktree.

    Raises:
        subprocess.CalledProcessError: If git worktree creation fails.
    """
    wt = paths.worktrees_dir() / job_id
    if wt.exists():
        probe = _git(wt, "rev-parse", "--is-inside-work-tree")
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            return wt  # attempt ≥2: reuse the existing worktree
        # stale/broken dir: let git prune bookkeeping, then recreate below
        _git(repo_root, "worktree", "prune")
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "worktree", "add", str(wt), "-B", f"herder/{job_id}")
    return wt


def capture_worktree_diff(worktree: Path, out_path: Path) -> Path | None:
    """Stage everything in the worktree and write the cumulative diff to out_path.

    Includes both modified files and new files. Returns out_path, or None if the
    worktree is clean. Git hooks are disabled during this operation.

    Args:
        worktree: Path to the git worktree.
        out_path: Path where the diff file will be written.

    Returns:
        out_path if there are changes; None if the worktree is clean.

    Raises:
        subprocess.CalledProcessError: If git commands fail.
    """
    _git(worktree, "add", "-A")
    r = _git(worktree, "diff", "--cached")
    if not r.stdout.strip():
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(r.stdout, encoding="utf-8")
    return out_path
