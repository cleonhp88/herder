"""Tests for launchd daemon artifacts (no personal paths embedded)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plist_template_exists_with_placeholders():
    """Plist has placeholders instead of personal paths."""
    plist_path = ROOT / "launchd" / "ai.herder.worker.plist"
    assert plist_path.exists(), "plist template must exist"
    text = plist_path.read_text()
    assert "__HERDER_DIR__" in text, "plist must have __HERDER_DIR__ placeholder"
    assert "__HERDER_HOME__" in text, "plist must have __HERDER_HOME__ placeholder"
    assert "KeepAlive" in text, "plist must have KeepAlive"
    assert "RunAtLoad" in text, "plist must have RunAtLoad"
    assert "ThrottleInterval" in text, "plist must have ThrottleInterval"
    # Verify no personal paths
    for forbidden in ("/Volumes/", "/Users/", "/home/", "C:\\\\Users"):
        assert forbidden not in text, f"plist must not contain {forbidden}"


def test_wrapper_script_is_executable_and_clean():
    """Wrapper script is executable, shebang correct, no personal paths."""
    script_path = ROOT / "launchd" / "herder-worker-launch.sh"
    assert script_path.exists(), "wrapper script must exist"
    text = script_path.read_text()
    assert text.startswith("#!/bin/zsh"), "wrapper must have zsh shebang"
    assert "exec uv run herder" in text, "wrapper must exec herder worker"
    assert "--worker-id herder-daemon" in text, "wrapper must pin daemon worker-id"
    assert "config.yaml missing" in text, "wrapper must check for config.yaml"
    assert os.access(script_path, os.X_OK), "wrapper script must be executable"
    # Verify no personal paths
    for forbidden in ("/Volumes/", "/Users/", "/home/", "C:\\\\Users"):
        assert forbidden not in text, f"wrapper must not contain {forbidden}"


def test_readme_exists():
    """README.md explains installation and usage."""
    readme_path = ROOT / "launchd" / "README.md"
    assert readme_path.exists(), "README.md must exist"
    text = readme_path.read_text()
    assert "Install" in text, "README must have Install section"
    assert "Uninstall" in text, "README must have Uninstall section"
    assert "__HERDER_DIR__" in text, "README must reference placeholder"
