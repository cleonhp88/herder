"""Tests for safe subprocess execution with shell-injection protection."""
from pathlib import Path
from herder.providers.base import run_subprocess


def test_prompt_via_stdin_no_shell_injection(tmp_path: Path) -> None:
    """Verify that prompt is passed via stdin, not shell expansion."""
    res = run_subprocess(
        ["cat"],
        prompt="hello $(whoami) `id`",
        cwd=tmp_path,
        timeout=10,
        env={},
    )
    assert res.exit_code == 0
    assert res.output.strip() == "hello $(whoami) `id`"  # metachars untouched
    assert res.status == "done"


def test_timeout_classified(tmp_path: Path) -> None:
    """Verify timeout is classified correctly."""
    res = run_subprocess(
        ["sleep", "5"],
        prompt="",
        cwd=tmp_path,
        timeout=1,
        env={},
    )
    assert res.status == "timeout"
    assert res.error_type == "timeout"


def test_missing_binary_classified(tmp_path: Path) -> None:
    """Verify missing binary is classified as unavailable."""
    res = run_subprocess(
        ["no-such-bin-xyz"],
        prompt="",
        cwd=tmp_path,
        timeout=5,
        env={},
    )
    assert res.status == "failed"
    assert res.error_type == "unavailable"


def test_subprocess_output_redacted(tmp_path: Path) -> None:
    """FIX 2: Subprocess output is redacted for secrets before returning."""
    # Use a pattern that matches sk- style keys (redacted by redact module)
    token_pattern = "sk-AaAaAaAaAaAaAaAaAaAa"
    res = run_subprocess(
        ["/bin/sh", "-c", f"echo 'API key: {token_pattern}'"],
        prompt="",
        cwd=tmp_path,
        timeout=10,
        env={},
    )
    assert res.status == "done"
    # Verify token is not in output
    assert token_pattern not in res.output
    # Verify redaction marker present
    assert "***REDACTED***" in res.output
