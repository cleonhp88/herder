"""Tests for the execute() dispatcher routing by provider type."""
from pathlib import Path

import pytest

from herder.config import Provider
from herder.models import Result
from herder.providers import ollama_http
from herder.providers.run import execute


def test_execute_routes_cli(tmp_path: Path) -> None:
    """Verify that execute() routes CLI providers to cli_generic."""
    p = Provider(type="cli", executable="cat", args=[], input="stdin", parser="text")
    res = execute(p, "ping", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=10)
    assert res.status == "done"
    assert res.output.strip() == "ping"


def test_execute_routes_ollama(monkeypatch, tmp_path: Path) -> None:
    """Verify that execute() routes ollama providers to ollama_http."""
    # Mock ollama_http.run to return a fixed result
    def mock_run(provider: Provider, prompt: str, *, timeout: int) -> Result:
        return Result("done", 0, output="OLLAMA_OK")

    monkeypatch.setattr(ollama_http, "run", mock_run)

    p = Provider(type="ollama", base_url="http://x:11434", model="m")
    res = execute(p, "ping", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=10)
    assert res.status == "done"
    assert res.output == "OLLAMA_OK"


def test_execute_unknown_type_raises(tmp_path: Path) -> None:
    """Verify that unknown provider types raise ValueError."""
    p = Provider(type="api", sdk="anthropic", model="claude")
    with pytest.raises(ValueError, match="unsupported provider type"):
        execute(p, "ping", cwd=tmp_path, run_dir=tmp_path, env={}, timeout=10)
