"""Eval scorer — pure, no I/O.

Implements deterministic assertion scoring against provider output text.
Assertion vocabulary is defined in evals/cases/pilot.yaml and this module
must stay in sync with that vocabulary.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScoreResult:
    """Scoring result for a single output against a list of assertions.

    Attributes:
        passed: True iff all assertions passed (no failures).
        failures: Taxonomy labels for every failed assertion.
        notes: Human-readable notes (one per assertion, pass or fail).
    """

    passed: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ─── JSON helpers ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)

# Matches a body that is EXACTLY a single code fence (whitespace-padded only).
# Used by the strict whole-body parser: prose before/after the fence = no match.
_FENCE_WHOLE_RE = re.compile(r"\A\s*```(?:json)?\s*\n?(.*?)\n?```\s*\Z", re.DOTALL)


def _strip_single_fence(text: str) -> str:
    """Strip at most one code fence (lenient — fence may appear anywhere in text).

    Used only by the lenient extract_json helper.

    Args:
        text: Text that may contain a code fence.

    Returns:
        Inner content of the first fence found, or original text unchanged.
    """
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text


def _try_parse(s: str) -> Any | None:
    """Attempt json.loads, return None on failure."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _find_balanced_span(text: str, open_ch: str, close_ch: str) -> str | None:
    """Find and return the first balanced {…} or […] span in text.

    String-aware: characters inside double-quoted JSON strings are not counted
    toward bracket depth, so a ``}`` or ``]`` inside a string value does not
    prematurely close the span.  Backslash escapes inside strings are handled
    so that ``\\"`` does not end the string.
    """
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return None


def extract_json(text: str) -> Any | None:
    """Extract the first parseable JSON value from text (lenient).

    Tries in order:
    1. Full body as-is.
    2. Fenced block (```json…``` or ```…```).
    3. First balanced ``{…}`` span.
    4. First balanced ``[…]`` span.

    Args:
        text: Raw text that may contain a JSON value.

    Returns:
        Parsed JSON value (dict, list, str, int, etc.) or None if none found.
    """
    stripped = text.strip()

    # 1. Full body
    v = _try_parse(stripped)
    if v is not None:
        return v

    # 2. Fenced block
    inner = _strip_single_fence(stripped)
    if inner != stripped:
        v = _try_parse(inner)
        if v is not None:
            return v

    # 3. First balanced {...}
    span = _find_balanced_span(stripped, "{", "}")
    if span:
        v = _try_parse(span)
        if v is not None:
            return v

    # 4. First balanced [...]
    span = _find_balanced_span(stripped, "[", "]")
    if span:
        v = _try_parse(span)
        if v is not None:
            return v

    return None


def _parse_strict_whole_body(text: str) -> Any | None:
    """Parse JSON from a body that is exactly JSON (plus optional single fence).

    The body must be ENTIRELY JSON or ENTIRELY a single code fence wrapping
    JSON.  Any prose before or after the JSON (or fence) causes a None return.

    Steps:
    1. Strip surrounding whitespace from the whole body.
    2. Try to parse the result as JSON directly.
    3. If that fails, check whether the whole body is exactly one fence block
       (using the anchored regex) — if so, try parsing its inner content.
    4. Anything else → None.

    Args:
        text: Raw output text.

    Returns:
        Parsed JSON value or None.
    """
    candidate = text.strip()

    # 1. Direct parse (no fence)
    v = _try_parse(candidate)
    if v is not None:
        return v

    # 2. Whole body is exactly a single fence
    m = _FENCE_WHOLE_RE.match(candidate)
    if m:
        return _try_parse(m.group(1).strip())

    return None


# ─── Assertion implementations ────────────────────────────────────────────────


def _assert_json_object(
    output: str,
    _assertion: dict,
    _ctx: dict,
) -> tuple[bool, str, Any]:
    """STRICT: entire body (fence-tolerant) must be a JSON object.

    Returns:
        (passed, note, parsed_object_or_None)
    """
    parsed = _parse_strict_whole_body(output)
    if isinstance(parsed, dict):
        return True, "body is a valid JSON object", parsed
    if parsed is None:
        return False, "body does not parse as JSON after fence stripping", None
    return (
        False,
        f"body parses as JSON but is {type(parsed).__name__}, not object",
        None,
    )


def _assert_json_keys(
    _output: str,
    assertion: dict,
    ctx: dict,
) -> tuple[bool, str]:
    """Parsed object's key set must equal exactly the given set."""
    parsed = ctx.get("parsed_object")
    if not isinstance(parsed, dict):
        return False, "json_keys requires a parsed JSON object (run json_object first)"
    expected = set(assertion["keys"])
    actual = set(parsed.keys())
    if actual == expected:
        return True, f"keys match: {sorted(expected)}"
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts = []
    if missing:
        parts.append(f"missing: {missing}")
    if extra:
        parts.append(f"extra: {extra}")
    return False, "key mismatch — " + "; ".join(parts)


def _assert_equals_field(
    _output: str,
    assertion: dict,
    ctx: dict,
) -> tuple[bool, str]:
    """Parsed object[field] == expected value."""
    parsed = ctx.get("parsed_object")
    if not isinstance(parsed, dict):
        # Fall back to lenient extract_json
        parsed = ctx.get("lenient_parse")
        if not isinstance(parsed, dict):
            return False, "equals_field: no parsed JSON object available"

    field_name = assertion["field"]
    expected = assertion["expected"]

    if field_name not in parsed:
        return False, f"field '{field_name}' not found in object"

    actual = parsed[field_name]

    # Bool guard: isinstance(True, int) is True in Python, so booleans must be
    # handled before the numeric branch to prevent 1==True or 0==False matches.
    if isinstance(expected, bool) or isinstance(actual, bool):
        if actual == expected and type(actual) is type(expected):
            return True, f"{field_name}={actual!r} matches expected {expected!r}"
        return False, f"{field_name}={actual!r} != expected {expected!r} (bool mismatch)"

    # Numeric equality: int/float equal by value
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if expected == actual:
            return True, f"{field_name}={actual!r} matches expected {expected!r}"
        return False, f"{field_name}={actual!r} != expected {expected!r}"

    if actual == expected:
        return True, f"{field_name}={actual!r} matches expected {expected!r}"
    return False, f"{field_name}={actual!r} != expected {expected!r}"


def _assert_json_array_len(
    output: str,
    assertion: dict,
    _ctx: dict,
) -> tuple[bool, str, Any]:
    """STRICT: entire body (fence-tolerant) must be a JSON array of exactly n items."""
    parsed = _parse_strict_whole_body(output)
    expected_n = assertion["n"]
    if not isinstance(parsed, list):
        type_name = type(parsed).__name__ if parsed is not None else "null/unparseable"
        return (
            False,
            f"body must be a JSON array, got {type_name}",
            None,
        )
    actual_n = len(parsed)
    if actual_n == expected_n:
        return True, f"array has exactly {expected_n} items", parsed
    return (
        False,
        f"array length {actual_n} != expected {expected_n}",
        None,
    )


def _assert_json_item_keys(
    _output: str,
    assertion: dict,
    ctx: dict,
) -> tuple[bool, str]:
    """Every array item's key set must equal exactly the given set."""
    parsed_array = ctx.get("parsed_array")
    if not isinstance(parsed_array, list):
        return (
            False,
            "json_item_keys requires a parsed JSON array (run json_array_len first)",
        )
    expected = set(assertion["keys"])
    failures_detail = []
    for i, item in enumerate(parsed_array):
        if not isinstance(item, dict):
            failures_detail.append(f"item[{i}] is not an object")
            continue
        actual = set(item.keys())
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            parts = []
            if missing:
                parts.append(f"missing: {missing}")
            if extra:
                parts.append(f"extra: {extra}")
            failures_detail.append(f"item[{i}] key mismatch — " + "; ".join(parts))
    if failures_detail:
        return False, "; ".join(failures_detail[:3])  # cap note length
    return True, f"all {len(parsed_array)} items have keys {sorted(expected)}"


def _assert_regex_must(
    output: str,
    assertion: dict,
    _ctx: dict,
) -> tuple[bool, str]:
    """output must match the pattern (re.search, IGNORECASE|UNICODE)."""
    pattern = assertion["pattern"]
    if re.search(pattern, output, re.IGNORECASE | re.UNICODE):
        return True, f"pattern matched: {pattern!r}"
    return False, f"pattern not found: {pattern!r}"


def _assert_regex_must_not(
    output: str,
    assertion: dict,
    _ctx: dict,
) -> tuple[bool, str]:
    """output must NOT match the pattern (re.search, IGNORECASE|UNICODE)."""
    pattern = assertion["pattern"]
    if re.search(pattern, output, re.IGNORECASE | re.UNICODE):
        return False, f"forbidden pattern found: {pattern!r}"
    return True, f"forbidden pattern absent: {pattern!r}"


# ─── Main scorer ─────────────────────────────────────────────────────────────

_ASSERTION_TYPES = frozenset(
    {
        "json_object",
        "json_keys",
        "equals_field",
        "json_array_len",
        "json_item_keys",
        "regex_must",
        "regex_must_not",
    }
)


def score(output: str, assertions: list[dict]) -> ScoreResult:
    """Score provider output against a list of assertions.

    Empty or whitespace-only output short-circuits with NO_OUTPUT failure.

    Assertion execution order matters: json_object/json_array_len populate
    ctx so that dependent assertions (json_keys, json_item_keys, equals_field)
    can operate on the parsed value.

    Args:
        output: Raw text output from the provider.
        assertions: List of assertion dicts from the eval case YAML.

    Returns:
        ScoreResult with passed flag, failure taxonomy labels, and notes.
    """
    if not output or not output.strip():
        return ScoreResult(passed=False, failures=["NO_OUTPUT"], notes=["empty output"])

    failures: list[str] = []
    notes: list[str] = []

    # Shared parse context — populated by structural assertions
    ctx: dict = {}

    for assertion in assertions:
        atype = assertion.get("type", "")
        on_fail = assertion.get("on_fail", "UNKNOWN")

        if atype not in _ASSERTION_TYPES:
            notes.append(f"[SKIP] unknown assertion type: {atype!r}")
            continue

        passed_this = False

        if atype == "json_object":
            passed_this, note, parsed = _assert_json_object(output, assertion, ctx)
            if passed_this:
                ctx["parsed_object"] = parsed
                # Also store lenient parse as fallback
                ctx["lenient_parse"] = parsed
            else:
                # Try lenient extract for subsequent equals_field fallback
                lp = extract_json(output)
                if isinstance(lp, dict):
                    ctx["lenient_parse"] = lp

        elif atype == "json_keys":
            passed_this, note = _assert_json_keys(output, assertion, ctx)

        elif atype == "equals_field":
            # If no strict parse yet, try lenient
            if "lenient_parse" not in ctx:
                lp = extract_json(output)
                if isinstance(lp, dict):
                    ctx["lenient_parse"] = lp
            passed_this, note = _assert_equals_field(output, assertion, ctx)

        elif atype == "json_array_len":
            passed_this, note, parsed_array = _assert_json_array_len(
                output, assertion, ctx
            )
            if passed_this:
                ctx["parsed_array"] = parsed_array

        elif atype == "json_item_keys":
            passed_this, note = _assert_json_item_keys(output, assertion, ctx)

        elif atype == "regex_must":
            passed_this, note = _assert_regex_must(output, assertion, ctx)

        elif atype == "regex_must_not":
            passed_this, note = _assert_regex_must_not(output, assertion, ctx)

        else:
            note = f"[SKIP] unhandled type: {atype!r}"
            passed_this = True  # don't penalise unknown types

        notes.append(note)
        if not passed_this:
            failures.append(on_fail)

    return ScoreResult(passed=len(failures) == 0, failures=failures, notes=notes)
