"""The hourly sweeper: plist rendering, bootstrap, bootout."""

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.claudinator.sweep"
SWEEP_INTERVAL_SECONDS = 3600


class LaunchdError(Exception):
    pass


def launchctl(*args: str) -> int:
    """Run launchctl and return its exit status. The tests replace this."""
    return subprocess.run(["launchctl", *args], capture_output=True).returncode


def plist(executable: Path, log: Path, env: dict[str, str]) -> bytes:
    """The agent's plist. env holds the variables the sweeper needs to see
    the same paths as the claudinator that installed it."""
    agent = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "sweep"],
        "StartInterval": SWEEP_INTERVAL_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    if env:
        agent["EnvironmentVariables"] = env
    return plistlib.dumps(agent)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install(executable: Path, log: Path, env: dict[str, str]) -> bool:
    """Write the plist if its bytes differ, and load the agent if it changed
    or is not loaded. Idempotent. Returns True if the plist changed."""
    path, body = plist_path(), plist(executable, log, env)
    changed = not path.exists() or path.read_bytes() != body
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix(".plist.tmp")
        staged.write_bytes(body)
        staged.replace(path)
    if changed or not loaded():
        launchctl("bootout", f"{_domain()}/{LABEL}")
        if (code := launchctl("bootstrap", _domain(), str(path))) != 0:
            raise LaunchdError(f"launchctl bootstrap {_domain()} {path} exited {code}")
    return changed


def uninstall() -> None:
    """Unload the agent if loaded and remove its plist. Keeps the database."""
    launchctl("bootout", f"{_domain()}/{LABEL}")
    plist_path().unlink(missing_ok=True)


def loaded() -> bool:
    return launchctl("print", f"{_domain()}/{LABEL}") == 0


def _domain() -> str:
    return f"gui/{os.getuid()}"
