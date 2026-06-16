"""Secret pattern redaction — prevents credential leakage in result.md."""
from __future__ import annotations

import re

# Known credential shapes; conservative to avoid mangling normal prose
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),  # OpenAI/Anthropic-style
    re.compile(r"user_[A-Za-z0-9]{40,}"),  # command-code style key
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # FIX 6: JWT (Supabase, etc.)
]

_REPL = "***REDACTED***"


def redact(text: str) -> str:
    """Redact known secret/credential patterns from text.

    Args:
        text: Input text potentially containing secrets.

    Returns:
        Text with secrets replaced by ***REDACTED***.
    """
    for pat in _PATTERNS:
        text = pat.sub(_REPL, text)
    return text
