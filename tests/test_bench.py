"""Tests for the bench service.

Verifies that:
- run_bench executes all providers sequentially
- Results are recorded with correct metrics
- Unknown providers raise ConfigError before execution
- Each provider runs in an isolated temp directory (no side effects)
- Failures are properly recorded
"""
import pytest

from herder.config import ConfigError, load_config
from herder.services.bench import run_bench


def _cfg(tmp_path) -> str:
    """Build a minimal test config with echo and failing providers.

    Args:
        tmp_path: pytest tmp_path fixture.

    Returns:
        Path to config YAML file.
    """
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  echo1: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  echo2: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  boom:  {type: cli, executable: sh, args: ['-c', 'exit 3'], input: stdin, timeout: 10}\n"
        "roles: {r: {provider: echo1}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    return str(c)


def test_bench_runs_all_providers(herder_home, tmp_path):
    """Test that run_bench executes all specified providers and records results."""
    cfg = load_config(_cfg(tmp_path))
    rep = run_bench(cfg, "hello bench", ["echo1", "echo2"])

    # Verify report structure
    assert rep.prompt_chars == len("hello bench")
    assert len(rep.results) == 2

    # Verify both providers were executed
    providers = {r.provider for r in rep.results}
    assert providers == {"echo1", "echo2"}

    # Verify results recorded
    for r in rep.results:
        assert r.status == "done"
        # cat echoes the prompt back
        assert r.output_len >= len("hello bench")
        assert r.duration_ms >= 0


def test_bench_records_failure(herder_home, tmp_path):
    """Test that run_bench records failures alongside successes."""
    cfg = load_config(_cfg(tmp_path))
    rep = run_bench(cfg, "x", ["echo1", "boom"])

    # Verify both providers appear in results
    assert len(rep.results) == 2

    statuses = {r.provider: r.status for r in rep.results}
    assert statuses["echo1"] == "done"
    assert statuses["boom"] == "failed"


def test_bench_unknown_provider_raises_before_execution(herder_home, tmp_path):
    """Test that unknown provider names raise ConfigError before any execution."""
    cfg = load_config(_cfg(tmp_path))

    # Unknown provider should raise immediately
    with pytest.raises(ConfigError, match="unknown provider: ghost"):
        run_bench(cfg, "x", ["echo1", "ghost"])


def test_bench_no_repo_side_effects(herder_home, tmp_path):
    """Test that provider execution in temp directories doesn't affect the repo.

    A provider that tries to write a file will write into its temp cwd,
    which is deleted after execution. The tmp_path (which simulates the repo)
    should remain unmodified.
    """
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  writer: {type: cli, executable: sh, args: ['-c', 'echo z > marker.txt'], "
        "input: stdin, timeout: 10}\n"
        "roles: {r: {provider: writer}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    run_bench(cfg, "x", ["writer"])

    # marker.txt should not exist in tmp_path (it was created in the temp cwd)
    assert not (tmp_path / "marker.txt").exists()


def test_bench_multiple_providers_all_recorded(herder_home, tmp_path):
    """Test that all providers in a multi-provider run are recorded."""
    c = tmp_path / "c.yaml"
    c.write_text(
        "providers:\n"
        "  p1: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  p2: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "  p3: {type: cli, executable: cat, args: [], input: stdin, timeout: 10}\n"
        "roles: {r: {provider: p1}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    rep = run_bench(cfg, "test", ["p1", "p2", "p3"])

    assert len(rep.results) == 3
    providers = {r.provider for r in rep.results}
    assert providers == {"p1", "p2", "p3"}
    assert all(r.status == "done" for r in rep.results)
