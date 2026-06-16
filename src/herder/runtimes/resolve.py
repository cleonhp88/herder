"""Runtime resolution — layered fallback from job → provider → project → local.

Provides two functions:
- resolve_runtime_name: pure function operating on plain strings (unit-testable)
- resolve_runtime: builds the concrete Runtime from cfg + db row + model objects

CRITICAL: Never use shell=True. argv-only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from herder.config import Config, Provider, Project
    from herder.runtimes.base import Runtime


def resolve_runtime_name(
    *,
    job_runtime: str | None,
    provider_runtime: str | None,
    project_default: str | None,
) -> str:
    """Resolve the effective runtime name using the layered fallback chain.

    Precedence (first non-None wins): job → provider → project → "local".

    Args:
        job_runtime: Runtime name stored on the job row, or None.
        provider_runtime: Runtime name declared on the Provider config, or None.
        project_default: Default runtime declared on the Project config, or None.

    Returns:
        Effective runtime name — always a non-empty string.

    Examples:
        >>> resolve_runtime_name(job_runtime="a", provider_runtime="b", project_default="c")
        'a'
        >>> resolve_runtime_name(job_runtime=None, provider_runtime=None, project_default=None)
        'local'
    """
    return job_runtime or provider_runtime or project_default or "local"


def resolve_runtime(
    cfg: "Config",
    job: object,
    provider: "Provider",
    project: "Project | None",
) -> "Runtime":
    """Build a Runtime from config + job row using the layered fallback chain.

    Reads ``job["runtime"]`` defensively (the column may be absent on rows from
    a pre-v6 read, though migration should always populate it).

    Args:
        cfg: Loaded configuration (carries runtimes dict + specs).
        job: A sqlite3.Row (or dict-like) representing the claimed job.
        provider: The resolved Provider config for this job.
        project: The resolved Project config, or None if job has no project.

    Returns:
        A Runtime instance ready to accept ``.run(...)`` calls.

    Raises:
        KeyError: If the resolved name is not "local" and not in cfg.runtimes.
        NotImplementedError: If the resolved spec type is not yet implemented.
    """
    # Guard: sqlite3.Row.keys() works post-v6; fall back to None for pre-v6 rows.
    try:
        job_runtime: str | None = job["runtime"]  # type: ignore[index]
    except (IndexError, KeyError):
        job_runtime = None

    name = resolve_runtime_name(
        job_runtime=job_runtime,
        provider_runtime=provider.runtime,
        project_default=project.default_runtime if project is not None else None,
    )

    from herder.config import LocalRuntimeSpec
    spec = LocalRuntimeSpec() if name == "local" else cfg.runtimes[name]

    from herder.runtimes.registry import build_runtime
    return build_runtime(spec)
