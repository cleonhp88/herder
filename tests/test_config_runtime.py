"""Tests for RuntimeSpec config models and Provider/Project runtime fields."""
from __future__ import annotations

import pytest
from herder.config import Config, ConfigError, LocalRuntimeSpec, DockerRuntimeSpec, SSHRuntimeSpec


def _cfg(**kw) -> Config:
    base = dict(providers={}, roles={}, projects={})
    base.update(kw)
    return Config(**base)


def test_runtimes_default_empty():
    assert _cfg().runtimes == {}


def test_docker_runtime_spec_parsed_with_defaults():
    cfg = Config(runtimes={"d": {"type": "docker", "image": "img"}})
    spec = cfg.runtimes["d"]
    assert isinstance(spec, DockerRuntimeSpec)
    assert spec.image == "img"
    assert spec.network == "none"           # default
    assert spec.extra_args == []


def test_ssh_runtime_spec_defaults():
    cfg = Config(runtimes={"m": {"type": "ssh", "host": "u@h"}})
    spec = cfg.runtimes["m"]
    assert isinstance(spec, SSHRuntimeSpec)
    assert spec.host == "u@h"
    assert spec.remote_root == "/tmp/herder"
    assert spec.allow_remote_secrets is False
    assert spec.ssh_opts == []


def test_local_runtime_spec():
    cfg = Config(runtimes={"local": {"type": "local"}})
    assert isinstance(cfg.runtimes["local"], LocalRuntimeSpec)


def test_unknown_runtime_field_forbidden():
    with pytest.raises(Exception):
        Config(runtimes={"d": {"type": "docker", "image": "i", "bogus": 1}})


def test_docker_requires_image():
    with pytest.raises(Exception):
        Config(runtimes={"d": {"type": "docker"}})


def test_ssh_requires_host():
    with pytest.raises(Exception):
        Config(runtimes={"m": {"type": "ssh"}})


def test_provider_runtime_field_optional():
    cfg = Config(providers={"p": {"type": "ollama", "base_url": "http://x", "model": "m",
                                   "runtime": "d"}},
                 runtimes={"d": {"type": "docker", "image": "i"}},
                 roles={"r": {"provider": "p"}})
    cfg.validate_refs()
    assert cfg.providers["p"].runtime == "d"


def test_project_default_runtime_field():
    cfg = Config(projects={"proj": {"root": "/tmp", "default_runtime": "d"}},
                 runtimes={"d": {"type": "docker", "image": "i"}})
    cfg.validate_refs()
    assert cfg.projects["proj"].default_runtime == "d"


def test_provider_runtime_unknown_name_rejected():
    cfg = Config(providers={"p": {"type": "ollama", "base_url": "http://x", "model": "m",
                                   "runtime": "ghost"}},
                 roles={"r": {"provider": "p"}})
    with pytest.raises(ConfigError, match="unknown runtime 'ghost'"):
        cfg.validate_refs()


def test_runtime_literal_local_always_valid():
    # "local" needs no runtimes: block declared.
    cfg = Config(projects={"proj": {"root": "/tmp", "default_runtime": "local"}})
    cfg.validate_refs()  # must not raise
