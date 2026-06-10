"""Tests for CLI generic provider execution."""
from pathlib import Path

from herder.config import Provider
from herder.providers import cli_generic


def test_cli_generic_text_echoes_stdin(tmp_path: Path) -> None:
    """Verify that text parser passes through stdout unchanged."""
    p = Provider(
        type="cli", executable="cat", args=[], input="stdin", parser="text"
    )
    res = cli_generic.run(
        p, "hello world", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=10
    )
    assert res.status == "done"
    assert res.output.strip() == "hello world"


def test_cli_generic_json_parser_extracts_key(tmp_path: Path) -> None:
    """Verify that json:response parser extracts the named field."""
    p = Provider(
        type="cli",
        executable="cat",
        args=[],
        input="stdin",
        parser="json:response",
    )
    res = cli_generic.run(
        p,
        '{"response":"hi there"}',
        cwd=tmp_path,
        run_dir=tmp_path,
        env={},
        timeout=10,
    )
    assert res.status == "done"
    assert res.output == "hi there"


def test_cli_generic_writes_logs(tmp_path: Path) -> None:
    """Verify that stdout_path captures output to a file."""
    p = Provider(
        type="cli", executable="cat", args=[], input="stdin", parser="text"
    )
    out = tmp_path / "stdout.log"
    res = cli_generic.run(
        p,
        "abc",
        cwd=tmp_path,
        run_dir=tmp_path,
        env={},
        timeout=10,
        stdout_path=out,
    )
    assert res.status == "done"
    assert out.exists()
    assert "abc" in out.read_text()


def test_cli_generic_parser_only_on_success(tmp_path: Path) -> None:
    """Verify that parser is only applied when status is 'done'."""
    p = Provider(
        type="cli",
        executable="sh",
        args=["-c", "echo bad_json && exit 1"],
        input="stdin",
        parser="json:response",
    )
    res = cli_generic.run(
        p, "prompt", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=10
    )
    assert res.status == "failed"
    # Parser should NOT have run, so output should be raw
    assert "bad_json" in res.output


def test_cli_generic_timeout(tmp_path: Path) -> None:
    """Verify that timeout is respected."""
    p = Provider(
        type="cli",
        executable="sleep",
        args=["10"],
        input="stdin",
        parser="text",
    )
    res = cli_generic.run(
        p, "prompt", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=1
    )
    assert res.status == "timeout"
    assert res.error_type == "timeout"
