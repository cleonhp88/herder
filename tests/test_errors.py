"""Tests for error classification and retry policy."""
from herder.errors import classify_error, is_retryable


def test_auth_patterns():
    """Test detection of authentication errors."""
    assert classify_error("401 Unauthorized: token expired", 1) == "auth"
    assert classify_error("Please sign in / not authenticated", 1) == "auth"
    assert classify_error("Failed to refresh token", 1) == "auth"


def test_rate_limit_patterns():
    """Test detection of rate limit errors."""
    assert classify_error("429 Too Many Requests", 1) == "rate_limit"
    assert classify_error("rate limit exceeded, retry later", 1) == "rate_limit"
    assert classify_error("quota exceeded for this billing period", 1) == "rate_limit"


def test_permission_patterns():
    """Test detection of permission errors."""
    assert classify_error("permission denied", 1) == "permission"
    assert classify_error("403 Forbidden", 1) == "permission"


def test_bad_prompt_patterns():
    """Test detection of bad prompt errors."""
    assert (
        classify_error(
            "prompt is too long: maximum context length exceeded", 1
        )
        == "bad_prompt"
    )
    assert (
        classify_error("context length exceeded, too many tokens", 1)
        == "bad_prompt"
    )


def test_unknown_default():
    """Test that unknown errors default to 'unknown'."""
    assert classify_error("segfault something weird", 1) == "unknown"
    assert classify_error("", 1) == "unknown"
    assert classify_error("random error message", 1) == "unknown"


def test_retryable_policy():
    """Test retry policy for different error types."""
    # Retryable errors
    assert is_retryable("timeout")
    assert is_retryable("rate_limit")
    assert is_retryable("unavailable")
    assert is_retryable("unknown")

    # Non-retryable errors
    assert not is_retryable("auth")
    assert not is_retryable("bad_prompt")
    assert not is_retryable("permission")

    # None is not retryable
    assert not is_retryable(None)


def test_numeric_codes_need_boundaries():
    """Test that numeric error codes require word boundaries."""
    # Should NOT match: 401 inside 4030 (no boundary)
    assert classify_error("input is 4030 tokens", 1) == "unknown"
    # Should NOT match: 401 inside 3401 (no boundary)
    assert classify_error("latency 3401ms", 1) == "unknown"
    # Should match: 403 with boundaries
    assert classify_error("HTTP 403 returned", 1) == "permission"
    # Should match: 429 with boundaries
    assert classify_error("error 429 rate limit", 1) == "rate_limit"
    # Should match: 401 with boundaries
    assert classify_error("got 401 unauthorized", 1) == "auth"
