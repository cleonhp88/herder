"""Tests for output parsers."""
from herder.providers.parsers import parse


def test_text_passthrough():
    """Text parser should pass through input unchanged."""
    assert parse("text", "hello") == "hello"
    assert parse("", "hello") == "hello"


def test_json_key_extraction():
    """JSON parser should extract specified key from JSON object."""
    assert parse("json:response", '{"response":"OK","x":1}') == "OK"
    assert parse("json:message", '{"message":"hello world"}') == "hello world"


def test_json_key_numeric_value():
    """JSON parser should convert numeric values to string."""
    assert parse("json:count", '{"count":42}') == "42"


def test_json_key_bad_input_falls_back():
    """JSON parser should fall back to raw output on invalid JSON."""
    assert parse("json:response", "not json") == "not json"
    assert parse("json:key", "{broken json}") == "{broken json}"


def test_json_key_missing_key_falls_back():
    """JSON parser should fall back when key is missing."""
    result = parse("json:missing", '{"response":"OK"}')
    assert result == ""  # get() returns empty string for missing key


def test_json_key_nested_not_supported():
    """JSON parser doesn't support nested keys - returns empty string."""
    # get("data.value") returns "" since nested keys are not supported
    assert parse("json:data.value", '{"data":{"value":"x"}}') == ""


def test_empty_string_passthrough():
    """Empty string input should be handled gracefully."""
    assert parse("text", "") == ""
    assert parse("json:x", "") == ""
