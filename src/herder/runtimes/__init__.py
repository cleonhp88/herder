"""Runtime abstraction package for Herder.

Defines the Runtime Protocol and provides concrete implementations:
- LocalRuntime: local subprocess (default, wraps providers/base.py)
- DockerRuntime: containerised execution (Phase 5)
- SSHRuntime: remote execution via SSH (Phase 6)
"""
from herder.runtimes.base import Runtime, run_with_terminate

__all__ = ["Runtime", "run_with_terminate"]
