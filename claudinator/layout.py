from __future__ import annotations

import enum
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claudinator.record import AgentId, SessionId


class DocRole(enum.StrEnum):
    SUBAGENT_META = "subagent-meta"
    SESSION_FILE = "session-file"
    DESKTOP = "desktop"


@dataclass(frozen=True, slots=True)
class Config:
    db: Path
    claude_dir: Path
    desktop_dir: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        home = Path(env.get("HOME") or Path.home())

        def path(var: str, default: Path) -> Path:
            return Path(env.get(var) or default).expanduser().absolute()

        return cls(
            db=path("CLAUDINATOR_DB", home / ".claudinator" / "claudinator.db"),
            claude_dir=path("CLAUDE_CONFIG_DIR", home / ".claude"),
            desktop_dir=(home / "Library" / "Application Support" / "Claude").absolute(),
        )


@dataclass(frozen=True, slots=True)
class TranscriptFile:
    path: Path
    session_id: SessionId
    agent_id: AgentId | None


@dataclass(frozen=True, slots=True)
class SideFile:
    path: Path
    role: DocRole
    session_id: SessionId | None
    agent_id: AgentId | None


Location = TranscriptFile | SideFile

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
AGENT_ID = re.compile(r"[0-9A-Za-z_-]+")
_TOOL_RESULTS = re.compile(rf"[^/]+/{_UUID}/tool-results")

_PROJECTS_LAYOUT: tuple[tuple[re.Pattern[str], Callable[[Path, re.Match[str]], Location | None]], ...] = (
    (
        re.compile(rf"[^/]+/({_UUID})\.jsonl"),
        lambda p, m: TranscriptFile(p, SessionId(m[1]), None),
    ),
    (
        re.compile(rf"[^/]+/({_UUID})/subagents/agent-({AGENT_ID.pattern})\.jsonl"),
        lambda p, m: TranscriptFile(p, SessionId(m[1]), AgentId(m[2])),
    ),
    (
        re.compile(rf"[^/]+/({_UUID})/subagents/agent-({AGENT_ID.pattern})\.meta\.json"),
        lambda p, m: SideFile(p, DocRole.SUBAGENT_META, SessionId(m[1]), AgentId(m[2])),
    ),
    (re.compile(_TOOL_RESULTS.pattern + "/.+"), lambda p, m: None),
    (
        re.compile(rf"[^/]+/({_UUID})/.+(?<!\.jsonl)"),
        lambda p, m: SideFile(p, DocRole.SESSION_FILE, SessionId(m[1]), None),
    ),
)
_DESKTOP_LAYOUT = re.compile(r"[^/]+/[^/]+/local_[^/]+\.json")


def classify(path: Path, cfg: Config) -> Location | None:
    """
        <claude>/projects/<slug>/<sid>.jsonl                           -> TranscriptFile(sid, None)
        <claude>/projects/<slug>/<sid>/subagents/agent-<aid>.jsonl     -> TranscriptFile(sid, aid)
        <claude>/projects/<slug>/<sid>/subagents/agent-<aid>.meta.json -> SideFile(SUBAGENT_META)
        <claude>/projects/<slug>/<sid>/tool-results/*                  -> None
        <claude>/projects/<slug>/<sid>/**/<other non-.jsonl file>      -> SideFile(SESSION_FILE)
        <desktop>/claude-code-sessions/<a>/<b>/local_*.json            -> SideFile(DESKTOP)
        anything else (memory/, sessions-index.json, ...)              -> None

    session_id comes from the path, never from record content: forked copies
    carry rewritten sessionIds, and the file is the unit Claude resumes."""
    projects = cfg.claude_dir / "projects"
    if path.is_relative_to(projects):
        rel = path.relative_to(projects).as_posix()
        for pattern, make in _PROJECTS_LAYOUT:
            if m := pattern.fullmatch(rel):
                return make(path, m)
        return None
    sessions = cfg.desktop_dir / "claude-code-sessions"
    if path.is_relative_to(sessions) and _DESKTOP_LAYOUT.fullmatch(path.relative_to(sessions).as_posix()):
        return SideFile(path, DocRole.DESKTOP, None, None)
    return None


@dataclass(frozen=True, slots=True)
class Discovery:
    transcripts: tuple[TranscriptFile, ...]
    side_files: tuple[SideFile, ...]
    unclassified: tuple[Path, ...]  # *.jsonl under projects/ that classify() does not know
    errors: tuple[str, ...]  # directories that could not be listed


def discover(cfg: Config) -> Discovery:
    """Walk projects/ and the desktop sessions dir and classify every file.
    A directory that vanishes mid-walk, or was never there, is not an error.
    tool-results/ directories hold no archived file and are not entered."""
    found: dict[type, list] = {TranscriptFile: [], SideFile: []}
    unclassified, errors = [], []

    def failed(e: OSError) -> None:
        if not isinstance(e, FileNotFoundError):
            errors.append(f"{e.filename}: {e!r}")

    projects = cfg.claude_dir / "projects"
    for root in (projects, cfg.desktop_dir / "claude-code-sessions"):
        for dirpath, dirnames, filenames in os.walk(root, onerror=failed):
            here = Path(dirpath)
            if root == projects:
                dirnames[:] = [d for d in dirnames if not _TOOL_RESULTS.fullmatch((here / d).relative_to(root).as_posix())]
            dirnames.sort()
            for name in sorted(filenames):
                path = here / name
                if (loc := classify(path, cfg)) is not None:
                    found[type(loc)].append(loc)
                elif root == projects and name.endswith(".jsonl"):
                    unclassified.append(path)
    return Discovery(tuple(found[TranscriptFile]), tuple(found[SideFile]), tuple(unclassified), tuple(errors))
