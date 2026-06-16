"""Tests for Ollama HTTP provider."""
import json
import socket
import urllib.error

from herder.config import Provider
from herder.providers import ollama_http


class FakeResponse:
    """Fake urllib response object for testing."""

    def __init__(self, raw_bytes):
        self._raw = raw_bytes

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_ollama_parses_response_success(monkeypatch):
    """Ollama provider should parse successful JSON response."""

    def fake_urlopen(req, timeout):
        return FakeResponse(b'{"response":"OK","eval_count":3}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(
        type="ollama",
        base_url="http://localhost:11434",
        model="qwen3.6:27b-coding-mxfp8",
    )
    r = ollama_http.run(p, "Reply OK", timeout=5)

    assert r.status == "done"
    assert r.exit_code == 0
    assert r.output == "OK"
    assert r.usage == {"eval_count": 3}
    assert r.error_type is None


def test_ollama_response_without_usage(monkeypatch):
    """Ollama provider should handle responses without usage metrics."""

    def fake_urlopen(req, timeout):
        return FakeResponse(b'{"response":"Hello"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(
        type="ollama",
        base_url="http://localhost:11434",
        model="llama2",
    )
    r = ollama_http.run(p, "test", timeout=5)

    assert r.status == "done"
    assert r.output == "Hello"
    assert r.usage is None


def test_ollama_response_with_prompt_eval_count(monkeypatch):
    """Ollama provider should capture both eval counts."""

    def fake_urlopen(req, timeout):
        return FakeResponse(
            b'{"response":"output","eval_count":10,"prompt_eval_count":5}'
        )

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="phi")
    r = ollama_http.run(p, "test", timeout=5)

    assert r.usage == {"eval_count": 10, "prompt_eval_count": 5}


def test_ollama_timeout(monkeypatch):
    """Ollama provider should handle socket timeout."""

    def fake_urlopen(req, timeout):
        raise socket.timeout("timeout")

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="m")
    r = ollama_http.run(p, "x", timeout=2)

    assert r.status == "timeout"
    assert r.exit_code == -1
    assert r.error_type == "timeout"
    assert r.started_at is not None
    assert r.finished_at is not None


def test_ollama_unreachable_is_unavailable(monkeypatch):
    """Ollama provider should classify connection errors as unavailable."""

    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://10.0.0.0:11434", model="m")
    r = ollama_http.run(p, "x", timeout=2)

    assert r.status == "failed"
    assert r.exit_code == -1
    assert r.error_type == "unavailable"
    assert r.output == "connection refused"


def test_ollama_json_decode_error(monkeypatch):
    """Ollama provider should handle JSON decode errors gracefully."""

    def fake_urlopen(req, timeout):
        return FakeResponse(b"not json")

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="m")
    r = ollama_http.run(p, "test", timeout=5)

    assert r.status == "failed"
    assert r.exit_code == -1
    assert r.error_type == "unknown"


def test_ollama_request_format(monkeypatch):
    """Ollama provider should format request correctly."""
    captured_req = []

    def fake_urlopen(req, timeout):
        captured_req.append(req)
        return FakeResponse(b'{"response":"OK"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(
        type="ollama",
        base_url="http://localhost:11434",
        model="testmodel",
    )
    ollama_http.run(p, "test prompt", timeout=5)

    req = captured_req[0]
    assert req.get_full_url() == "http://localhost:11434/api/generate"
    assert req.get_method() == "POST"
    assert req.headers["Content-type"] == "application/json"

    payload = json.loads(req.data.decode())
    assert payload["model"] == "testmodel"
    assert payload["prompt"] == "test prompt"
    assert payload["stream"] is False


def test_ollama_base_url_trailing_slash_handled(monkeypatch):
    """Ollama provider should handle base_url with trailing slash."""

    def fake_urlopen(req, timeout):
        return FakeResponse(b'{"response":"OK"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    captured_req = []

    def capturing_urlopen(req, timeout):
        captured_req.append(req)
        return fake_urlopen(req, timeout)

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", capturing_urlopen)

    p = Provider(
        type="ollama",
        base_url="http://localhost:11434/",
        model="m",
    )
    ollama_http.run(p, "test", timeout=5)

    # Should strip trailing slash
    assert captured_req[0].get_full_url() == "http://localhost:11434/api/generate"


def test_ollama_wrapped_timeout_classified_as_timeout(monkeypatch):
    """URLError wrapping TimeoutError should be classified as timeout, not unavailable."""

    def boom(req, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", boom)

    p = Provider(type="ollama", base_url="http://x:11434", model="m")
    r = ollama_http.run(p, "x", timeout=1)

    assert r.status == "timeout" and r.error_type == "timeout"


# --- think field tests ---


def test_ollama_think_false_included_in_payload(monkeypatch):
    """Payload must include think=False when provider.think is False."""
    captured = []

    def fake_urlopen(req, timeout):
        captured.append(json.loads(req.data.decode()))
        return FakeResponse(b'{"response":"OK"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="gpt-oss:20b", think=False)
    ollama_http.run(p, "test", timeout=5)

    assert "think" in captured[0]
    assert captured[0]["think"] is False


def test_ollama_think_true_included_in_payload(monkeypatch):
    """Payload must include think=True when provider.think is True."""
    captured = []

    def fake_urlopen(req, timeout):
        captured.append(json.loads(req.data.decode()))
        return FakeResponse(b'{"response":"OK"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="gpt-oss:20b", think=True)
    ollama_http.run(p, "test", timeout=5)

    assert "think" in captured[0]
    assert captured[0]["think"] is True


def test_ollama_think_none_omitted_from_payload(monkeypatch):
    """Payload must NOT contain 'think' key when provider.think is None (byte-compat guard)."""
    captured = []

    def fake_urlopen(req, timeout):
        captured.append(json.loads(req.data.decode()))
        return FakeResponse(b'{"response":"OK"}')

    monkeypatch.setattr(ollama_http.urllib.request, "urlopen", fake_urlopen)

    p = Provider(type="ollama", base_url="http://localhost:11434", model="qwen3.6:27b")
    # think defaults to None
    ollama_http.run(p, "test", timeout=5)

    assert "think" not in captured[0]
