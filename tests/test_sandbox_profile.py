"""Unit tests for seatbelt sandbox profile generation."""
from pathlib import Path

from herder.providers.sandbox import build_profile, wrap


def test_profile_denies_network_and_confines_writes(tmp_path: Path) -> None:
    """Verify profile denies network and confines writes to allow_write paths."""
    prof = build_profile(allow_write=[tmp_path], deny_network=True)
    assert "(deny network*)" in prof
    assert "(deny file-write*)" in prof
    assert str(tmp_path.resolve()) in prof


def test_profile_no_network_deny_when_allowed(tmp_path: Path) -> None:
    """Verify (deny network*) is omitted when deny_network=False."""
    prof = build_profile(allow_write=[tmp_path], deny_network=False)
    assert "(deny network*)" not in prof
    assert "(deny file-write*)" in prof


def test_profile_multiple_allow_write_paths(tmp_path: Path) -> None:
    """Verify multiple allow_write paths are all included."""
    path1 = tmp_path / "dir1"
    path2 = tmp_path / "dir2"
    path1.mkdir()
    path2.mkdir()
    prof = build_profile(allow_write=[path1, path2], deny_network=True)
    assert str(path1.resolve()) in prof
    assert str(path2.resolve()) in prof


def test_profile_includes_dev_for_tty(tmp_path: Path) -> None:
    """Verify /dev is always allowed for writes (ttys, etc.)."""
    prof = build_profile(allow_write=[tmp_path], deny_network=False)
    assert '(allow file-write* (subpath "/dev"))' in prof


def test_wrap_prepends_sandbox_exec(tmp_path: Path) -> None:
    """Verify wrap() prepends sandbox-exec with -p flag."""
    profile_str = "(version 1)"
    wrapped = wrap(["echo", "hello"], profile_str)
    assert wrapped == ["sandbox-exec", "-p", "(version 1)", "echo", "hello"]


def test_wrap_preserves_argv_order() -> None:
    """Verify wrap() preserves command and arguments in order."""
    profile_str = "(version 1)"
    wrapped = wrap(["sh", "-c", "ls -la"], profile_str)
    assert wrapped[:3] == ["sandbox-exec", "-p", "(version 1)"]
    assert wrapped[3:] == ["sh", "-c", "ls -la"]
