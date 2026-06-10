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
