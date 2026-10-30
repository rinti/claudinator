import os
import time

import pytest

from claudinator import cli, launchd


@pytest.fixture(autouse=True, scope="session")
def libc_zone_left_as_found():
    before = os.environ.get("TZ"), time.tzname
    yield
    assert (os.environ.get("TZ"), time.tzname) == before, "a test changed the libc time zone without restoring it"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDINATOR_DB", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture(autouse=True)
def zone():
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
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded = False
        self.bootstrap_exits: list[int] = []

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
    slept: list[float] = []
    monkeypatch.setattr(launchd, "sleep", slept.append)
    return slept


@pytest.fixture(autouse=True)
def execs(monkeypatch):
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli, "_exec", lambda cwd, argv: calls.append((cwd, argv)))
    return calls


@pytest.fixture(autouse=True)
def claude_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return claude


@pytest.fixture(autouse=True)
def executable(tmp_path, monkeypatch):
    script = tmp_path / "installed" / "claudinator"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli, "_executable", lambda: script)
    return script
