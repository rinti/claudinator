import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test runs against a fresh HOME, so Config.from_env() points at
    a temp ~/.claude, desktop dir, and database, never the real ones."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDINATOR_DB", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home
