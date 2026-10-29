import time

import pytest

from claudinator import cli, launchd


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test runs against a fresh HOME, so Config.from_env() points at
    a temp ~/.claude, desktop dir, and database, never the real ones."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDINATOR_DB", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    return home


class FakeLaunchctl:
    """Stands in for launchctl: remembers whether the agent is loaded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False

    def __call__(self, *args: str) -> int:
        self.calls.append(args)
        match args[0]:
            case "print":
                return 0 if self.loaded else 113
            case "bootstrap":
                self.loaded = True
                return 0
            case "bootout":
                was, self.loaded = self.loaded, False
                return 0 if was else 3
        raise AssertionError(f"unexpected launchctl {args}")


@pytest.fixture(autouse=True)
def launchctl(monkeypatch):
    fake = FakeLaunchctl()
    monkeypatch.setattr(launchd, "launchctl", fake)
    return fake


@pytest.fixture(autouse=True)
def execs(monkeypatch):
    """What `resume` would have exec'd, as (cwd, argv)."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "_exec", lambda cwd, argv: calls.append((cwd, argv)))
    return calls
