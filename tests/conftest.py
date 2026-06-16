import pytest


@pytest.fixture
def herder_home(tmp_path, monkeypatch):
    """Isolate all state under a tmp dir — never touch real ~/.herder."""
    home = tmp_path / "herder_home"
    home.mkdir()
    monkeypatch.setenv("HERDER_HOME", str(home))
    return home
