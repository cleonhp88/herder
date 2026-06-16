"""Tests for runtime registry + layered resolution (Phase 4)."""
from herder.config import LocalRuntimeSpec
from herder.runtimes.local import LocalRuntime
from herder.runtimes.registry import build_runtime
from herder.runtimes.resolve import resolve_runtime_name


def test_build_local_runtime():
    rt = build_runtime(LocalRuntimeSpec())
    assert isinstance(rt, LocalRuntime)
    assert rt.name == "local"


def test_resolution_job_wins():
    assert resolve_runtime_name(job_runtime="a", provider_runtime="b",
                                project_default="c") == "a"


def test_resolution_provider_next():
    assert resolve_runtime_name(job_runtime=None, provider_runtime="b",
                                project_default="c") == "b"


def test_resolution_project_next():
    assert resolve_runtime_name(job_runtime=None, provider_runtime=None,
                                project_default="c") == "c"


def test_resolution_defaults_local():
    assert resolve_runtime_name(job_runtime=None, provider_runtime=None,
                                project_default=None) == "local"
