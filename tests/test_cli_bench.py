"""Tests for the bench CLI command.

Verifies that:
- CLI argument parsing works correctly
- Prompt file is read and passed to the service
- Provider names are parsed correctly
- Output is printed in the expected format
"""
from herder.cli import main


def test_bench_cli_executes_providers(herder_home, tmp_path, capsys):
    """Test that bench CLI reads prompt file and prints results."""
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo1: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles: {r: {provider: echo1}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    pf = tmp_path / "p.md"
    pf.write_text("benchmark this")

    rc = main(
        [
            "--config", str(c),
            "bench",
            "--prompt-file", str(pf),
            "--providers", "echo1"
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "echo1" in out
    assert "done" in out
    assert "benchmark this" not in out  # prompt should not be printed


def test_bench_cli_multiple_providers(herder_home, tmp_path, capsys):
    """Test bench CLI with multiple providers."""
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  p1: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  p2: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles: {r: {provider: p1}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    pf = tmp_path / "p.md"
    pf.write_text("test")

    rc = main(
        [
            "--config", str(c),
            "bench",
            "--prompt-file", str(pf),
            "--providers", "p1, p2"
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "p1" in out
    assert "p2" in out


def test_bench_cli_prompt_file_required(herder_home, tmp_path):
    """Test that --prompt-file is required."""
    c = tmp_path / "c.yaml"
    c.write_text("providers: {}\nworker: {global_concurrency: 1}\n")

    # argparse exits on missing required argument
    try:
        main(["--config", str(c), "bench", "--providers", "p1"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code != 0


def test_bench_cli_providers_required(herder_home, tmp_path):
    """Test that --providers is required."""
    c = tmp_path / "c.yaml"
    c.write_text("providers: {}\nworker: {global_concurrency: 1}\n")
    pf = tmp_path / "p.md"
    pf.write_text("test")

    # argparse exits on missing required argument
    try:
        main(["--config", str(c), "bench", "--prompt-file", str(pf)])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code != 0


def test_bench_cli_json_output(herder_home, tmp_path, capsys):
    """--json emits a single parseable JSON object with the documented schema, sorted fastest-first."""
    import json

    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo1: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  echo2: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles: {r: {provider: echo1}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    pf = tmp_path / "p.md"
    pf.write_text("benchmark this")

    rc = main(
        ["--config", str(c), "bench", "--prompt-file", str(pf), "--providers", "echo1,echo2", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prompt_chars"] == len("benchmark this")
    assert len(payload["results"]) == 2
    keys = {"provider", "status", "duration_ms", "output_len", "tokens", "error_type"}
    assert set(payload["results"][0].keys()) == keys
    assert payload["results"][0]["status"] == "done"
    durations = [r["duration_ms"] for r in payload["results"]]
    assert durations == sorted(durations)  # fastest-first


def test_bench_missing_prompt_file_clean_error(herder_home, tmp_path, capsys):
    """Test that missing prompt file produces a clean error message."""
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  e: {type: cli, executable: cat, input: stdin}\n"
        "roles: {r: {provider: e}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    rc = main(["--config", str(c), "bench", "--prompt-file", str(tmp_path / "nope.md"), "--providers", "e"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
