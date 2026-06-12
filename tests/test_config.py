from pathlib import Path
from herder.config import load_config, ConfigError
import pytest

EXAMPLE = "config.example.yaml"


def test_loads_dev_safe_example():
    cfg = load_config(EXAMPLE)
    assert "echo_cli" in cfg.providers
    assert cfg.providers["echo_cli"].executable == "cat"
    assert cfg.roles["planner"].provider == "echo_cli"
    assert cfg.worker.global_concurrency == 3
    assert cfg.doctor.min_ok_providers == 1


def test_resolve_role_returns_provider():
    assert load_config(EXAMPLE).resolve_provider_for_role("cheap") == "echo_cli"


def test_role_referencing_unknown_provider_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers: {}\nroles: {x: {provider: nope}}\nworker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(bad))


def test_example_config_has_no_personal_paths():
    for f in ("config.example.yaml", "config.real.example.yaml"):
        text = Path(f).read_text()
        for forbidden in ("/Volumes/", "/Users/", "/home/", "C:\\\\Users"):
            assert forbidden not in text, f"{forbidden} leaked into {f}"


def test_project_referencing_unknown_role_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo}}\n"
        "projects: {p: {root: '/path/to/x', allowed_roles: [planner, ghost]}}\n"
        "worker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(bad))


def test_ollama_provider_requires_base_url(tmp_path):
    bad = tmp_path / "o.yaml"
    bad.write_text(
        "providers: {ol: {type: ollama, model: qwen}}\n"
        "worker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(bad))


def test_ollama_provider_requires_model(tmp_path):
    bad = tmp_path / "o.yaml"
    bad.write_text(
        "providers: {ol: {type: ollama, base_url: http://localhost:11434}}\n"
        "worker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(bad))


def test_cli_provider_requires_executable(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text(
        "providers: {cl: {type: cli}}\n"
        "worker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(bad))


def test_ollama_provider_valid(tmp_path):
    good = tmp_path / "o.yaml"
    good.write_text(
        "providers: {ol: {type: ollama, base_url: http://localhost:11434, model: qwen}}\n"
        "worker: {global_concurrency: 1}\n")
    cfg = load_config(str(good))
    assert "ol" in cfg.providers
    assert cfg.providers["ol"].type == "ollama"
    assert cfg.providers["ol"].base_url == "http://localhost:11434"
    assert cfg.providers["ol"].model == "qwen"


def test_schedule_parsing_and_validation(tmp_path):
    c = tmp_path / "s.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo}}\n"
        "projects: {p: {root: '/tmp/x', allowed_roles: [planner]}}\n"
        "schedules:\n"
        "  - {id: daily, cron: '0 22 * * *', project: p, role: planner, kind: research, prompt_file: jobs/d.md}\n"
        "worker: {global_concurrency: 1}\n")
    cfg = load_config(str(c))
    assert cfg.schedules[0].id == "daily" and cfg.schedules[0].cron == "0 22 * * *"


def test_schedule_unknown_project_rejected(tmp_path):
    c = tmp_path / "s.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo}}\n"
        "projects: {}\n"
        "schedules:\n  - {id: d, cron: '* * * * *', project: ghost, role: planner, prompt_file: x.md}\n"
        "worker: {global_concurrency: 1}\n")
    with pytest.raises(ConfigError):
        load_config(str(c))


def test_schedule_prompt_file_resolved_relative_to_config(tmp_path):
    """Relative prompt_file paths should be resolved against config file's directory."""
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "d.md").write_text("x")
    c = tmp_path / "s.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo}}\n"
        "projects: {p: {root: '/tmp/x', allowed_roles: [planner]}}\n"
        "schedules:\n  - {id: d, cron: '* * * * *', project: p, role: planner, prompt_file: jobs/d.md}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.schedules[0].prompt_file == str(tmp_path / "jobs" / "d.md")


def test_env_profile_allow_env_parsed(tmp_path):
    c = tmp_path / "e.yaml"
    c.write_text(
        "providers: {echo: {type: cli, executable: cat, input: stdin}}\n"
        "roles: {planner: {provider: echo}}\n"
        "env_profiles: {cc: {allow_env: [COMMAND_CODE_API_KEY]}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.env_profiles["cc"].allow_env == ["COMMAND_CODE_API_KEY"]


# ---------------------------------------------------------------------------
# Tier 1: Capability manifest fields on Provider
# ---------------------------------------------------------------------------

def _minimal_cfg(tmp_path, provider_extra: str = "") -> str:
    """Write a minimal valid config and return its path.

    Args:
        tmp_path: Temporary directory to write the file in.
        provider_extra: Extra YAML lines appended to the provider block.

    Returns:
        Absolute path to the written YAML file.
    """
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        + provider_extra
        + "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    return str(c)


def test_provider_manifest_fields_defaults(tmp_path):
    """Provider manifest fields should default correctly when absent from YAML."""
    cfg = load_config(_minimal_cfg(tmp_path))
    p = cfg.providers["echo"]
    assert p.output_format == "text"
    assert p.supports == []
    assert p.cost_hint is None
    assert p.auth_env is None


def test_provider_manifest_fields_parsed(tmp_path):
    """Manifest fields are correctly parsed when present in YAML."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        "    output_format: json\n"
        "    supports: [read_only, worktree_write]\n"
        "    cost_hint: '$0'\n"
        "    auth_env: ECHO_API_KEY\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    p = cfg.providers["echo"]
    assert p.output_format == "json"
    assert p.supports == ["read_only", "worktree_write"]
    assert p.cost_hint == "$0"
    assert p.auth_env == "ECHO_API_KEY"


# ---------------------------------------------------------------------------
# Tier 1: extra="forbid" on all config models
# ---------------------------------------------------------------------------

def test_unknown_key_in_provider_raises_config_error(tmp_path):
    """Unknown keys inside a provider block must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo: {type: cli, executable: cat, input: stdin, typo_key: bad}\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    with pytest.raises(ConfigError):
        load_config(str(c))


def test_unknown_top_level_key_raises_config_error(tmp_path):
    """Unknown top-level keys in config must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo: {type: cli, executable: cat, input: stdin}\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
        "unknown_top_key: oops\n"
    )
    with pytest.raises(ConfigError):
        load_config(str(c))


def test_unknown_key_in_role_raises_config_error(tmp_path):
    """Unknown keys in a role block must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo: {type: cli, executable: cat, input: stdin}\n"
        "roles:\n"
        "  planner: {provider: echo, bad_field: true}\n"
        "worker: {global_concurrency: 1}\n"
    )
    with pytest.raises(ConfigError):
        load_config(str(c))


def test_unknown_key_in_project_raises_config_error(tmp_path):
    """Unknown keys in a project block must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo: {type: cli, executable: cat, input: stdin}\n"
        "roles: {planner: {provider: echo}}\n"
        "projects:\n"
        "  p: {root: '/tmp/x', mystery_field: true}\n"
        "worker: {global_concurrency: 1}\n"
    )
    with pytest.raises(ConfigError):
        load_config(str(c))


# ---------------------------------------------------------------------------
# Tier 1: validate_refs — supports list validation
# ---------------------------------------------------------------------------

def test_supports_invalid_value_raises_config_error(tmp_path):
    """Provider supports list with invalid permission level must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo: {type: cli, executable: cat, input: stdin, supports: [read_only, superpower]}\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    with pytest.raises(ConfigError, match="superpower"):
        load_config(str(c))


def test_supports_valid_values_accepted(tmp_path):
    """All valid permission levels in supports must be accepted without error."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        "    supports: [read_only, worktree_write, inplace_write, untrusted]\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.providers["echo"].supports == [
        "read_only", "worktree_write", "inplace_write", "untrusted"
    ]


# ---------------------------------------------------------------------------
# Tier 1: validate_refs — auth_env + env_profile cross-check
# ---------------------------------------------------------------------------

def test_auth_env_in_env_profile_allow_env_ok(tmp_path):
    """auth_env listed in env_profile.allow_env must not raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        "    env_profile: myprofile\n"
        "    auth_env: MY_KEY\n"
        "roles: {planner: {provider: echo}}\n"
        "env_profiles:\n"
        "  myprofile: {allow_env: [MY_KEY, OTHER_VAR]}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.providers["echo"].auth_env == "MY_KEY"


def test_auth_env_not_in_env_profile_allow_env_raises(tmp_path):
    """auth_env not listed in env_profile.allow_env must raise ConfigError."""
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        "    env_profile: myprofile\n"
        "    auth_env: SECRET_KEY\n"
        "roles: {planner: {provider: echo}}\n"
        "env_profiles:\n"
        "  myprofile: {allow_env: [OTHER_VAR]}\n"
        "worker: {global_concurrency: 1}\n"
    )
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_config(str(c))


def test_auth_env_without_env_profile_is_allowed(tmp_path):
    """auth_env set without env_profile is informational only and must not raise.

    Note: build_env passes nothing extra for this case, so the declaration
    is doctor-informational only (documents which env var the provider needs).
    """
    c = tmp_path / "cfg.yaml"
    c.write_text(
        "providers:\n"
        "  echo:\n"
        "    type: cli\n"
        "    executable: cat\n"
        "    input: stdin\n"
        "    auth_env: ON_DISK_OR_AMBIENT_KEY\n"
        "roles: {planner: {provider: echo}}\n"
        "worker: {global_concurrency: 1}\n"
    )
    cfg = load_config(str(c))
    assert cfg.providers["echo"].auth_env == "ON_DISK_OR_AMBIENT_KEY"
    assert cfg.providers["echo"].env_profile is None
