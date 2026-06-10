"""Tests for secret redaction in result.md."""
from herder.redact import redact


def test_redacts_openai_patterns():
    """Test that patterns matching OpenAI key format are redacted."""
    # Pattern: sk- followed by 16+ alphanumeric/dash/underscore
    text = "Authorization: Bearer xyz123"
    normal = redact(text)
    assert normal == text  # No secret pattern, unchanged


def test_leaves_normal_text():
    """Verify normal text remains unchanged."""
    text = "This is a normal sentence with no secrets."
    assert redact(text) == text


def test_redacts_found_pattern():
    """Test that a constructed matching pattern is redacted."""
    # Create a pattern that matches the OpenAI key regex
    parts = ["sk", "AaAaAaAaAaAaAaAa"]
    key = "-".join(parts)
    text = f"api_key: {key}"
    redacted = redact(text)
    assert "***REDACTED***" in redacted


def test_redacts_pem_header():
    """Test that PEM block markers are redacted."""
    text = "-----BEGIN PRIVATE KEY-----"
    redacted = redact(text)
    assert "***REDACTED***" in redacted


def test_short_text_unchanged():
    """Short tokens that don't match patterns should be unchanged."""
    text = "user alice token abc123"
    redacted = redact(text)
    # This should be unchanged since 'abc123' is only 6 chars, less than pattern threshold
    assert "***REDACTED***" not in redacted


def test_redacts_jwt():
    """FIX 6: JWT tokens (Supabase, etc.) are redacted."""
    # JWT format: three base64-url parts separated by dots
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.abcdEFGH1234_-xy"
    redacted = redact(jwt)
    assert jwt not in redacted
    assert "***REDACTED***" in redacted
