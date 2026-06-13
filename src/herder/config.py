from __future__ import annotations
from typing import Literal
from pathlib import Path
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    """Configuration loading or validation error."""
    pass


# Single source of truth for valid permission levels.
# registry.py's _PERM dict is keyed by exactly these values.
PERMISSION_LEVELS: frozenset[str] = frozenset(
    {"read_only", "worktree_write", "inplace_write", "untrusted"}
)


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["cli", "api", "ollama", "acp"]
    executable: str | None = None
    args: list[str] = Field(default_factory=list)
    input: Literal["stdin", "arg", "file", "arg_or_stdin"] = "stdin"
    env_profile: str | None = None
    sdk: str | None = None
    model: str | None = None
    base_url: str | None = None
    # Only meaningful for type="ollama" with reasoning models (e.g. gpt-oss).
    # None = omit from request (let ollama default); False = disable thinking;
    # True = enable thinking explicitly.
    think: bool | None = None
    timeout: int = 1800
    max_concurrency: int = 1
    parser: str = "text"
    cost_key: str | None = None
    # --- Capability manifest (Tier 1) ---
    output_format: Literal["text", "json", "stream-json"] = "text"
    supports: list[str] = Field(default_factory=list)
    cost_hint: str | None = None
    auth_env: str | None = None


class Cooldown(BaseModel):
    """Cooldown policy for a provider within a role.

    Controls how many consecutive failures within a time window cause a provider
    to be skipped in favour of the next provider in the role's providers list.

    Failure counts are GLOBAL per provider: a provider failing for one role is
    considered failing for all roles — intentional, because a broken backend is
    broken for everyone. Cooldown only has effect for roles with ≥2 providers.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_fails: int = Field(default=3, gt=0)
    window_seconds: int = Field(default=300, gt=0)


class Role(BaseModel):
    """Role configuration mapping a name to one or more providers.

    Supports both the canonical ``providers`` list form and the legacy single
    ``provider`` string form (normalised to a one-element list at load time).

    Cooldown failure counts are GLOBAL per provider (a provider failing for one
    role is considered failing for all roles — intentional: a broken backend is
    broken for everyone). Cooldown only has effect for roles with ≥2 providers.
    """

    model_config = ConfigDict(extra="forbid")

    providers: list[str] = Field(default_factory=list)
    system_prompt_file: str | None = None
    default_timeout: int | None = None
    permissions: str = "read_only"
    output_format: str = "report"
    max_concurrency: int | None = None
    retry_policy: str = "standard"
    cooldown: Cooldown = Field(default_factory=Cooldown)

    @model_validator(mode="before")
    @classmethod
    def _normalise_provider(cls, data: object) -> object:
        """Normalise legacy ``provider`` string into ``providers`` list.

        Accepts:
        - ``providers: [...]`` list form (canonical)
        - ``provider: "name"`` string form (legacy, normalised to list)

        Raises:
            ValueError: If both are given, neither is given, provider is not
                        a non-empty str, or providers is not a non-empty list.
        """
        if not isinstance(data, dict):
            return data
        has_provider = "provider" in data
        has_providers = "providers" in data
        if has_provider and has_providers:
            raise ValueError("specify either 'provider' or 'providers', not both")
        if has_provider:
            val = data.pop("provider")
            if not isinstance(val, str) or not val:
                raise ValueError("'provider' must be a non-empty string")
            data["providers"] = [val]
        if not data.get("providers"):
            raise ValueError(
                "role requires either 'provider' (string) or 'providers' (list)"
            )
        providers_val = data["providers"]
        if not isinstance(providers_val, list) or len(providers_val) == 0:
            raise ValueError("'providers' must be a non-empty list")
        return data


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    default_workspace_mode: Literal["readonly", "worktree", "inplace"] = "readonly"
    allowed_roles: list[str] = Field(default_factory=list)
    allow_inplace: bool = False
    result_dir: str | None = None


class Worker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_concurrency: int = 3
    heartbeat_interval: int = 15
    lease_seconds: int = 3600
    timezone: str = "UTC"


class EnvProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_env: list[str] = Field(default_factory=list)


class Doctor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_ok_providers: int = 1


class Budget(BaseModel):
    """Runaway guards applied atomically at enqueue (BEGIN IMMEDIATE).

    Enforces two caps:
    - max_active_jobs: Hard cap on non-terminal jobs (pending, approved, running, etc.)
    - max_jobs_per_day: Hard cap on jobs created in the trailing 24-hour window (not calendar-day reset).

    Dedup collapses identical still-running submissions by (role, project, kind, prompt_hash).
    """
    model_config = ConfigDict(extra="forbid")

    max_active_jobs: int = 100
    max_jobs_per_day: int = 500
    dedup_active: bool = True


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    cron: str
    project: str
    role: str
    kind: str = "automation"
    prompt_file: str
    enabled: bool = True


class Retention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_done_days: int = 30
    keep_failed_days: int = 90
    keep_logs_days: int = 30
    archive_results: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, Provider] = Field(default_factory=dict)
    roles: dict[str, Role] = Field(default_factory=dict)
    projects: dict[str, Project] = Field(default_factory=dict)
    worker: Worker = Field(default_factory=Worker)
    doctor: Doctor = Field(default_factory=Doctor)
    schedules: list[Schedule] = Field(default_factory=list)
    env_profiles: dict[str, EnvProfile] = Field(default_factory=dict)
    retention: Retention = Field(default_factory=Retention)
    budget: Budget = Field(default_factory=Budget)

    def resolve_provider_for_role(self, role: str) -> str:
        """Resolve the primary (first) provider name for a given role.

        This is a backward-compatible shim. New code should call
        ``resolve_providers_for_role`` to obtain the full ordered list.

        Args:
            role: Role name.

        Returns:
            The primary provider name (first entry in providers list).

        Raises:
            ConfigError: If role is unknown.
        """
        if role not in self.roles:
            raise ConfigError(f"unknown role: {role}")
        return self.roles[role].providers[0]

    def resolve_providers_for_role(self, role: str) -> list[str]:
        """Resolve the full ordered providers list for a given role.

        Args:
            role: Role name.

        Returns:
            Ordered list of provider names for the role.

        Raises:
            ConfigError: If role is unknown.
        """
        if role not in self.roles:
            raise ConfigError(f"unknown role: {role}")
        return self.roles[role].providers

    def validate_refs(self) -> None:
        """Validate cross-references between roles, providers, and projects.

        Checks:
        - All providers in each role's providers list exist.
        - Role permissions value is valid.
        - All roles referenced by projects exist.
        - Provider type-specific required fields are present.
        - Provider supports list contains only valid permission levels.
        - auth_env is reachable via env_profile (when both are set).
        - For roles with non-empty provider supports, role permissions are covered.
        - Schedule ids are unique and reference existing projects/roles.
        """
        for rname, role in self.roles.items():
            # Validate permissions field against the canonical set.
            if role.permissions not in PERMISSION_LEVELS:
                raise ConfigError(
                    f"role '{rname}' has invalid permissions '{role.permissions}'; "
                    f"valid values: {sorted(PERMISSION_LEVELS)}"
                )
            for pname in role.providers:
                if pname not in self.providers:
                    raise ConfigError(
                        f"role '{rname}' references unknown provider '{pname}'"
                    )
            # Capability check at load time: for each provider in the role's list
            # that declares a non-empty supports list, the role's permissions must
            # be covered.
            for pname in role.providers:
                prov = self.providers[pname]
                if prov.supports and role.permissions not in prov.supports:
                    supports_str = format_supports(prov.supports)
                    raise ConfigError(
                        f"provider '{pname}' does not support permission "
                        f"'{role.permissions}' required by role '{rname}' "
                        f"(supports: {supports_str})"
                    )
        for pname, project in self.projects.items():
            for r in project.allowed_roles:
                if r not in self.roles:
                    raise ConfigError(
                        f"project '{pname}' references unknown role '{r}'"
                    )
        # Guard: ACP providers must not be assigned to untrusted roles (no sandbox support)
        for rname, role in self.roles.items():
            if role.permissions == "untrusted":
                for pname in role.providers:
                    prov = self.providers.get(pname)
                    if prov is not None and prov.type == "acp":
                        raise ConfigError(
                            f"ACP providers cannot run untrusted jobs (no sandbox support): "
                            f"role '{rname}' has permissions='untrusted' but includes "
                            f"ACP provider '{pname}'"
                        )
        for pname, prov in self.providers.items():
            if prov.type == "cli" and not prov.executable:
                raise ConfigError(f"provider '{pname}' (cli) missing executable")
            if prov.type == "ollama" and (not prov.base_url or not prov.model):
                raise ConfigError(f"provider '{pname}' (ollama) needs base_url and model")
            if prov.type == "acp" and not prov.executable:
                raise ConfigError(f"provider '{pname}' (acp) missing executable")
            # Validate supports list against the canonical permission levels.
            for level in prov.supports:
                if level not in PERMISSION_LEVELS:
                    raise ConfigError(
                        f"provider '{pname}' has invalid supports value '{level}'; "
                        f"valid levels: {sorted(PERMISSION_LEVELS)}"
                    )
            # Validate auth_env reachability when an env_profile is set.
            # Rule fires only when BOTH auth_env and env_profile are declared.
            # auth_env without env_profile is informational only (doctor display).
            if prov.auth_env is not None and prov.env_profile is not None:
                profile = self.env_profiles.get(prov.env_profile)
                if profile is not None and prov.auth_env not in profile.allow_env:
                    raise ConfigError(
                        f"provider '{pname}': auth_env '{prov.auth_env}' is not in "
                        f"env_profile '{prov.env_profile}'.allow_env — the credential "
                        f"would be stripped by env minimization"
                    )
        seen_ids: set[str] = set()
        for sch in self.schedules:
            if sch.id in seen_ids:
                raise ConfigError(f"duplicate schedule id '{sch.id}'")
            seen_ids.add(sch.id)
            if sch.project not in self.projects:
                raise ConfigError(f"schedule '{sch.id}' references unknown project '{sch.project}'")
            if sch.role not in self.roles:
                raise ConfigError(f"schedule '{sch.id}' references unknown role '{sch.role}'")


def format_supports(supports: list[str]) -> str:
    """Return a human-readable, sorted representation of a provider's supports list.

    Args:
        supports: List of permission level strings.

    Returns:
        Comma-separated sorted values, or "*" when the list is empty.
    """
    return ", ".join(sorted(supports)) if supports else "*"


def load_config(path: str) -> Config:
    """Load and validate configuration from a YAML file.

    Args:
        path: Path to the config YAML file.

    Returns:
        Loaded and validated Config object.

    Raises:
        ConfigError: If the file cannot be loaded or is invalid.
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        cfg = Config(**data)
    except (ValidationError, yaml.YAMLError) as e:
        raise ConfigError(str(e)) from e
    cfg.validate_refs()

    # Resolve relative schedule prompt_file paths against the config file's directory
    base_dir = Path(path).resolve().parent
    for sch in cfg.schedules:
        p = Path(sch.prompt_file)
        if not p.is_absolute():
            sch.prompt_file = str(base_dir / p)

    return cfg
