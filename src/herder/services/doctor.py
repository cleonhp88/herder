"""Doctor service — probes provider health and persists results."""
from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path

from herder.config import Config
from herder.db.store import Store
from herder.doctor import probe_provider, ProviderHealth
from herder.env import build_env


@dataclass
class DoctorReport:
    """Report from running doctor probe on all providers.

    Attributes:
        rows: List of ProviderHealth records.
        ok_count: Number of providers with noninteractive_status == "ok".
        min_ok: Threshold for minimum ok providers.
        passed: Whether ok_count >= min_ok.
        warnings: List of integrity/security warnings (group/world-writable files).
    """

    rows: list[ProviderHealth]
    ok_count: int
    min_ok: int
    passed: bool
    warnings: list[str]


def _writable_by_others(p: Path) -> bool:
    """Check if a path is group/world-writable.

    Args:
        p: Path to check.

    Returns:
        True if path exists and is writable by group or others.
    """
    try:
        m = p.stat().st_mode
    except OSError:
        return False
    return bool(m & (stat.S_IWGRP | stat.S_IWOTH))


def integrity_warnings(cfg: Config, config_path: str | None = None) -> list[str]:
    """Return warnings for group/world-writable provider executables or config.

    Detects supply-chain / trojan risks where another process could swap a
    provider binary or config file for a malicious version.

    Args:
        cfg: Loaded configuration.
        config_path: Path to config file (None skips config check).

    Returns:
        List of warning messages.
    """
    warns: list[str] = []
    for name, prov in cfg.providers.items():
        if prov.type == "cli" and prov.executable:
            exe = Path(prov.executable)
            if exe.is_absolute() and exe.exists() and _writable_by_others(exe):
                warns.append(
                    f"provider '{name}': executable {exe} is group/world-writable"
                )
    if config_path and _writable_by_others(Path(config_path)):
        warns.append(f"config {config_path} is group/world-writable")
    return warns


def run_doctor(
    cfg: Config,
    store: Store,
    cwd: Path,
    min_ok: int | None = None,
    config_path: str | None = None,
) -> DoctorReport:
    """Run health probe on all configured providers and persist results.

    Args:
        cfg: Loaded configuration.
        store: SQLite store for persistence.
        cwd: Current working directory for provider execution.
        min_ok: Override for minimum ok providers threshold (None uses config).
        config_path: Path to config file for integrity checking.

    Returns:
        DoctorReport with all probe results, pass/fail status, and warnings.
    """
    # Determine threshold: explicit arg > config default
    threshold = min_ok if min_ok is not None else cfg.doctor.min_ok_providers

    rows: list[ProviderHealth] = []
    for name, prov in cfg.providers.items():
        # Resolve env allowlist from config
        prof = cfg.env_profiles.get(prov.env_profile) if prov.env_profile else None
        allow = prof.allow_env if prof else []
        # Probe each provider
        h = probe_provider(name, prov, env=build_env(allow), cwd=cwd)
        # Persist to database
        store.upsert_provider_health(h)
        rows.append(h)

    # Count ok providers
    ok = sum(1 for h in rows if h.noninteractive_status == "ok")

    # Check for integrity warnings
    warns = integrity_warnings(cfg, config_path)

    return DoctorReport(rows, ok, threshold, ok >= threshold, warns)
