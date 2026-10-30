import os
import time

import pytest

from claudinator import cli, launchd


@pytest.fixture(autouse=True, scope="session")
def libc_zone_left_as_found():
    """The suite leaves the process's libc time zone as it found it, so no
    test sees a zone another test chose."""
    before = os.environ.get("TZ"), time.tzname
    yield
    assert (os.environ.get("TZ"), time.tzname) == before, "a test changed the libc time zone without restoring it"


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


@pytest.fixture(autouse=True)
def zone():
    """Sets the libc time zone, UTC unless a test asks for another, and puts
    the old one back afterwards. TZ alone changes nothing until tzset()
    runs, so restoring the variable is not enough."""
    before = os.environ.get("TZ")

    def set_zone(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    set_zone("UTC")
    yield set_zone
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    time.tzset()


class FakeLaunchctl:
    """Stands in for launchctl: remembers whether the agent is loaded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False
        self.bootstrap_exits: list[int] = []  # exit codes of the next bootstraps, before one succeeds

    def __call__(self, *args: str) -> int:
        self.calls.append(args)
        match args[0]:
            case "print":
                return 0 if self.loaded else 113
            case "bootstrap":
                if self.bootstrap_exits:
                    return self.bootstrap_exits.pop(0)
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
def sleeps(monkeypatch):
    """The seconds launchd.py slept, without sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(launchd, "sleep", slept.append)
    return slept


@pytest.fixture(autouse=True)
def execs(monkeypatch):
    """What `resume` would have exec'd, as (cwd, argv)."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "_exec", lambda cwd, argv: calls.append((cwd, argv)))
    return calls


@pytest.fixture(autouse=True)
def claude_on_path(tmp_path, monkeypatch):
    """A `claude` executable on PATH, so resume's check finds one. Tests of
    a missing claude set PATH themselves."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return claude


@pytest.fixture(autouse=True)
def executable(tmp_path, monkeypatch):
    """The claudinator install writes into the plist, so the suite needs no
    claudinator on PATH."""
    script = tmp_path / "installed" / "claudinator"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli, "_executable", lambda: script)
    return script
