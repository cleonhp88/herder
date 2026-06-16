"""Output parsers for transforming provider stdout into final output."""
from __future__ import annotations

import json


def parse(parser: str, stdout: str) -> str:
    """Turn raw provider stdout into the final output string.

    Args:
        parser: Parser specification (format: "text" or "json:<key>").
                - "text" (default): passthrough stdout unchanged.
                - "json:<key>": parse stdout as JSON, return str(obj[key]);
                  fall back to raw stdout on any error.
        stdout: Raw output from the provider.

    Returns:
        Parsed output string.
    """
    if not parser or parser == "text":
        return stdout

    if parser.startswith("json:"):
        key = parser.split(":", 1)[1]
        try:
            obj = json.loads(stdout)
            value = obj.get(key, "")
            return str(value)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return stdout

    return stdout
