"""E2E tests for secret_access enforcement at execution time.

Tests verify that jobs receive provider secrets ONLY if their permissions
grant secret_access=true. The secret is exposed via an env var that the
test provider echoes to stdout.
"""
from __future__ import annotations

from pathlib import Path

from herder import env as env_mod
from herder.config import load_config
from herder.db.store import Store
from herder.loops.queue_claim import run_pending_once
from herder.services.enqueue import enqueue_job, EnqueueRequest


def _cfg(tmp_path: Path, permissions: str) -> str:
    """Create a test config with a provider that echoes MY_SECRET env var.

    Args:
        tmp_path: Temporary directory for the project root.
        permissions: Permission preset name (e.g., "read_only" or "untrusted").

    Returns:
        Path to the config YAML file.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echoer:\n"
        "    type: cli\n"
        "    executable: sh\n"
        "    args: ['-c', 'printf \"<%s>\" \"$MY_SECRET\"']\n"
        "    input: stdin\n"
        "    env_profile: ep\n"
        "    timeout: 10\n"
        f"roles:\n"
        f"  r:\n"
        f"    provider: echoer\n"
        f"    permissions: {permissions}\n"
        f"projects:\n"
        f"  p:\n"
        f"    root: '{proj}'\n"
        f"    default_workspace_mode: readonly\n"
        f"    allowed_roles: [r]\n"
        "env_profiles:\n"
        "  ep:\n"
        "    allow_env: [MY_SECRET]\n"
        "worker:\n"
        "  global_concurrency: 1\n"
        "  lease_seconds: 3600\n"
    )
    return str(c)


def _run(
    cfg_path: str, herder_home: Path, monkeypatch  # type: ignore
) -> str:
    """Enqueue a job and execute it synchronously, returning stdout.

    Forces a deterministic login env with MY_SECRET present.

    Args:
        cfg_path: Path to config YAML.
        herder_home: Herder home directory.
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The echoed output from the provider.
    """
    # Force a deterministic login env (no real shell), with the secret present
    monkeypatch.setattr(
        env_mod,
        "_login_shell_env",
        lambda: {
            "PATH": "/usr/bin:/bin",
            "MY_SECRET": "s3cr3t",
            "HOME": str(herder_home),
            "SHELL": "/bin/sh",
            "TERM": "xterm",
        },
    )
    cfg = load_config(cfg_path)
    store = Store.open()
    r = enqueue_job(
        cfg,
        store,
        EnqueueRequest(
            project="p", role="r", kind="automation", prompt="x"
        ),
    )
    run_pending_once(cfg, store, "w1", 3600)
    j = store.get_job(r.job_id)
    # Read the stdout from the first attempt
    stdout_path = Path(j["run_dir"]) / "stdout.1.log"
    return stdout_path.read_text(encoding="utf-8")


def test_secret_passed_when_secret_access_true(
    herder_home: Path, tmp_path: Path, monkeypatch  # type: ignore
) -> None:
    """Secret is passed when secret_access=true (read_only preset)."""
    out = _run(_cfg(tmp_path, "read_only"), herder_home, monkeypatch)
    assert "<s3cr3t>" in out, f"Expected secret in output, got: {out}"


def test_secret_denied_when_untrusted(
    herder_home: Path, tmp_path: Path, monkeypatch  # type: ignore
) -> None:
    """Secret is NOT passed when secret_access=false (untrusted preset)."""
    out = _run(_cfg(tmp_path, "untrusted"), herder_home, monkeypatch)
    # The provider echoes "<%s>" where %s is the env var (empty if not passed)
    assert "<>" in out, f"Expected empty var in output, got: {out}"
    assert "s3cr3t" not in out, f"Secret leaked in output: {out}"
