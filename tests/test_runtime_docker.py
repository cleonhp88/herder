"""Unit tests for DockerRuntime — Phase 5.

Tests cover:
- build_docker_argv: pure function; no subprocess.
- DockerRuntime._make_terminate: calls docker stop then docker kill.
- _derive_container_name: collision-safe per run-dir and attempt.
- registry.build_runtime: returns DockerRuntime for DockerRuntimeSpec.
"""
from pathlib import Path


def test_docker_argv_basic():
    from herder.runtimes.docker import build_docker_argv

    argv = build_docker_argv(
        inner_argv=["mytool", "--flag"],
        image="img",
        network="none",
        cwd=Path("/work"),
        env={},
        name="job-123",
        extra_args=[],
    )
    assert argv[0] == "docker"
    assert "run" in argv and "--rm" in argv and "-i" in argv
    assert "--name" in argv and "job-123" in argv
    assert "--network" in argv and "none" in argv
    assert "-v" in argv and "/work:/work" in argv
    assert "-w" in argv and "/work" in argv
    # image must appear; inner_argv must be the tail
    img_idx = argv.index("img")
    assert argv[img_idx + 1 :] == ["mytool", "--flag"]


def test_docker_argv_passes_env_as_e_flags():
    from herder.runtimes.docker import build_docker_argv

    argv = build_docker_argv(
        inner_argv=["t"],
        image="i",
        network="none",
        cwd=Path("/w"),
        env={"FOO": "bar", "BAZ": "q"},
        name="n",
        extra_args=[],
    )
    joined = " ".join(argv)
    assert "-e FOO=bar" in joined and "-e BAZ=q" in joined


def test_docker_argv_network_bridge_and_extra_args():
    from herder.runtimes.docker import build_docker_argv

    argv = build_docker_argv(
        inner_argv=["t"],
        image="i",
        network="bridge",
        cwd=Path("/w"),
        env={},
        name="n",
        extra_args=["--cpus", "2"],
    )
    assert "--network" in argv and "bridge" in argv
    assert "--cpus" in argv and "2" in argv


def test_docker_runtime_terminate_calls_docker_stop(monkeypatch):
    from herder.runtimes.docker import DockerRuntime

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "herder.runtimes.docker.subprocess.run",
        lambda argv, **kw: calls.append(argv),
    )
    rt = DockerRuntime(name="d", image="img", network="none", extra_args=[])

    class FakeProc:
        pass

    rt._make_terminate("job-123")(FakeProc())
    assert any("stop" in c for c in calls) and any("job-123" in c for c in calls)


def test_registry_builds_docker():
    from herder.config import DockerRuntimeSpec
    from herder.runtimes.docker import DockerRuntime
    from herder.runtimes.registry import build_runtime

    rt = build_runtime(DockerRuntimeSpec(type="docker", image="img"))
    assert isinstance(rt, DockerRuntime)
    assert rt.image == "img"


def test_derive_container_name_different_run_dirs_yield_different_names(tmp_path):
    """Two concurrent jobs at the same attempt but different run dirs must not collide.

    Regression guard for the HIGH finding: stem-only naming maps both
    run-AAAA/stdout.1.log and run-BBBB/stdout.1.log to 'herder-stdout.1',
    causing docker run --name conflict under the multi-worker pool.
    """
    from herder.runtimes.docker import _derive_container_name

    run_dir_a = tmp_path / "run-AAAA"
    run_dir_b = tmp_path / "run-BBBB"
    run_dir_a.mkdir()
    run_dir_b.mkdir()

    # Simulate supervisor.py:56 — same attempt number, different run dirs
    stdout_a = run_dir_a / "stdout.1.log"
    stdout_b = run_dir_b / "stdout.1.log"

    name_a = _derive_container_name(stdout_a)
    name_b = _derive_container_name(stdout_b)

    assert name_a != name_b, (
        f"Container names must differ for different run dirs: {name_a!r} == {name_b!r}"
    )
    # Both must contain the unique run-dir segment
    assert "run-AAAA" in name_a or "run--AAAA" in name_a or "AAAA" in name_a
    assert "run-BBBB" in name_b or "run--BBBB" in name_b or "BBBB" in name_b


def test_derive_container_name_none_returns_uuid_format():
    """None stdout_path must return a unique herder-<hex> name."""
    from herder.runtimes.docker import _derive_container_name

    name1 = _derive_container_name(None)
    name2 = _derive_container_name(None)

    assert name1.startswith("herder-")
    assert name2.startswith("herder-")
    # Two calls must produce distinct names (UUID-based)
    assert name1 != name2


def test_derive_container_name_same_run_dir_same_attempt_stable():
    """Same stdout path must always produce the same container name (stable within a run)."""
    from pathlib import Path

    from herder.runtimes.docker import _derive_container_name

    stdout = Path("/tmp/run-XYZ/stdout.2.log")
    assert _derive_container_name(stdout) == _derive_container_name(stdout)
