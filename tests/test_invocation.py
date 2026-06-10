"""Tests for invocation builder that converts Provider + prompt into argv/stdin/file."""
from pathlib import Path
from herder.config import Provider
from herder.providers.invocation import build_invocation


def test_stdin_mode(tmp_path: Path) -> None:
    """Verify stdin mode passes prompt via stdin."""
    p = Provider(type="cli", executable="claude", args=["-p"], input="stdin")
    inv = build_invocation(p, "PROMPT", tmp_path)
    assert inv.argv == ["claude", "-p"]
    assert inv.stdin == "PROMPT"
    assert inv.prompt_file is None


def test_arg_mode(tmp_path: Path) -> None:
    """Verify arg mode appends prompt to argv."""
    p = Provider(type="cli", executable="gemini", args=["-p"], input="arg")
    inv = build_invocation(p, "PROMPT", tmp_path)
    assert inv.argv == ["gemini", "-p", "PROMPT"]
    assert inv.stdin is None
    assert inv.prompt_file is None


def test_arg_or_stdin_prefers_arg(tmp_path: Path) -> None:
    """Verify arg_or_stdin mode prefers arg."""
    p = Provider(type="cli", executable="gemini", args=["-p"], input="arg_or_stdin")
    inv = build_invocation(p, "PROMPT", tmp_path)
    assert inv.argv[-1] == "PROMPT"
    assert inv.stdin is None
    assert inv.prompt_file is None


def test_file_mode_writes_prompt(tmp_path: Path) -> None:
    """Verify file mode writes prompt to a file and includes it in argv."""
    p = Provider(type="cli", executable="tool", args=["--file"], input="file")
    inv = build_invocation(p, "PROMPT", tmp_path)
    assert inv.prompt_file is not None
    assert inv.prompt_file.read_text() == "PROMPT"
    assert str(inv.prompt_file) in inv.argv
    assert inv.stdin is None
