"""Data models for subprocess invocation results and status."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

ErrorType = Literal[
    "timeout",
    "rate_limit",
    "auth",
    "bad_prompt",
    "permission",
    "unavailable",
    "unknown",
]


@dataclass
class Result:
    """Result of subprocess execution.

    Attributes:
        status: Execution outcome (done, failed, timeout, cancelled).
        exit_code: Process exit code.
        output: Captured stdout.
        error_type: Classification of the failure (if any).
        stdout_path: Path to saved stdout file (if captured).
        stderr_path: Path to saved stderr file (if captured).
        output_path: Path to saved combined output file (if any).
        usage: Metadata about usage (tokens, duration, etc.).
        provider_metadata: Provider-specific metadata (model, version, etc.).
        started_at: UTC timestamp when execution began.
        finished_at: UTC timestamp when execution completed.
    """

    status: Literal["done", "failed", "timeout", "cancelled"]
    exit_code: int
    output: str = ""
    stderr: str = ""
    error_type: ErrorType | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    output_path: Path | None = None
    usage: dict | None = None
    provider_metadata: dict = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
