"""Tests for cost estimation."""
from __future__ import annotations

import pytest

from herder.cost import estimate_cost


class TestEstimateCost:
    """Cost estimation from usage metrics."""

    def test_local_rate_is_zero(self) -> None:
        """Local/free providers return zero cost."""
        usage = {"eval_count": 1000, "prompt_eval_count": 500}
        assert estimate_cost(usage, "local") == 0.0
        assert estimate_cost(usage, "free") == 0.0

    def test_unknown_cost_key_returns_none(self) -> None:
        """Unknown cost_key returns None."""
        usage = {"eval_count": 1000, "prompt_eval_count": 500}
        assert estimate_cost(usage, "mystery_key") is None
        assert estimate_cost(usage, None) is None

    def test_no_usage_returns_none(self) -> None:
        """None or empty usage returns None."""
        assert estimate_cost(None, "local") is None
        assert estimate_cost({}, "local") is None  # empty dict → falsy → None

    def test_api_token_naming(self) -> None:
        """API format (input_tokens, output_tokens) supported."""
        usage = {"input_tokens": 1000, "output_tokens": 500}
        # local rate is 0, so should be 0 regardless of token format
        assert estimate_cost(usage, "local") == 0.0

    def test_fallback_token_keys(self) -> None:
        """Fallback: missing keys treated as 0 tokens."""
        usage_partial = {"eval_count": 500}
        assert estimate_cost(usage_partial, "local") == 0.0
        usage_other = {"unknown_key": 999}
        assert estimate_cost(usage_other, "local") == 0.0
