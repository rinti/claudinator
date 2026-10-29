"""The archive: the only owner of the SQLite file.

Discovers transcript files, ingests their bytes as append-only zstd segments,
folds the projection, answers queries, exports bytes, and rebuilds the
projection. The call chain is cli.py -> archive.py -> record.py; nothing
reaches past this module into the database.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from importlib import resources
from pathlib import Path
from typing import BinaryIO

from compression import zstd

from claudinator.record import (
    CONVERSATION,
    PROJECTION_VERSION,
    TOOLS,
    AgentId,
    Facts,
    Kind,
    Millis,
    Record,
    SessionId,
    Turn,
    Unparsed,
    desktop_session,
    parse,
    project,
    subagent_meta,
    text_of,
)

_ARCHIVE_DDL, _MARKER, _PROJECTION_DDL = (
    resources.files("claudinator").joinpath("schema.sql").read_text().partition("-- @projection")
)
assert _MARKER, "schema.sql lost its -- @projection marker"

ARCHIVE_SCHEMA = 2
"""The shape of the archive layer. A new database is stamped with it, and a
database stamped with any other value is not written. There are no
migrations: none has been needed, and an untested one would be worse."""

SEGMENT_BYTES = 1 << 20
"""Target uncompressed segment size. Measured on 82.9 MB of this Mac's
transcripts with zstd level 3: per-line 2.9x, 128 KiB 4.2x, 1 MiB 4.8x,
whole-file 5.0x. 1 MiB keeps random access to one line at one ~1 ms
decompress while losing 4% versus whole files."""

LOCK_WAIT_MS = 60_000
"""How long a write that may wait (sweep(wait=True), rebuild) waits for
another process's write lock before it gives up with Locked."""

BUSY_MS = 5000
"""How long a statement waits on another process's lock. Only BEGIN
IMMEDIATE in _write sets its own wait, and only for that one statement."""

TAIL_BYTES = 4096
"""Each segment stores a sha256 of the file's last TAIL_BYTES bytes before
its end. Before treating a file as unchanged or as an append, a sweep reads
those bytes back from the file and compares, which catches a rewrite that
kept the head and did not shrink the file."""

SETTLE_SECONDS = 3600
"""A file untouched this long with bytes after its last newline has a torn
tail from a crashed writer. The sweep archives that tail so nothing on disk
is left unarchived. A live writer never leaves a partial line for an hour."""


class DocRole(enum.StrEnum):
    SUBAGENT_META = "subagent-meta"  # <sid>/subagents/agent-<aid>.meta.json
    SESSION_FILE = "session-file"  # any other non-.jsonl file under <slug>/<sid>/, except tool-results/
    DESKTOP = "desktop"  # <desktop>/claude-code-sessions/*/*/local_*.json


@dataclass(frozen=True, slots=True)
class Config:
    db: Path  # $CLAUDINATOR_DB or ~/.claudinator/claudinator.db
    claude_dir: Path  # $CLAUDE_CONFIG_DIR or ~/.claude
    desktop_dir: Path  # ~/Library/Application Support/Claude

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
    agent_id: AgentId | None  # None = main transcript


@dataclass(frozen=True, slots=True)
class SideFile:
    path: Path
    role: DocRole
    session_id: SessionId | None  # DESKTOP: None until the body is parsed
    agent_id: AgentId | None


Location = TranscriptFile | SideFile

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOOL_RESULTS = re.compile(rf"[^/]+/{_UUID}/tool-results")

_PROJECTS_LAYOUT: tuple[tuple[re.Pattern[str], Callable[[Path, re.Match[str]], Location | None]], ...] = (
    (
        re.compile(rf"[^/]+/({_UUID})\.jsonl"),
        lambda p, m: TranscriptFile(p, SessionId(m[1]), None),
    ),
    (
        re.compile(rf"[^/]+/({_UUID})/subagents/agent-([^/]+)\.jsonl"),
        lambda p, m: TranscriptFile(p, SessionId(m[1]), AgentId(m[2])),
    ),
    (
        re.compile(rf"[^/]+/({_UUID})/subagents/agent-([^/]+)\.meta\.json"),
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
    """The only code that knows Claude Code's on-disk layout.

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
    """One walk of projects/ and the desktop sessions directory."""

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
            dirnames[:] = sorted(
                d for d in dirnames if not (root == projects and _TOOL_RESULTS.fullmatch((here / d).relative_to(root).as_posix()))
            )
            for name in sorted(filenames):
                path = here / name
                if (loc := classify(path, cfg)) is not None:
                    found[type(loc)].append(loc)
                elif root == projects and name.endswith(".jsonl"):
                    unclassified.append(path)
    return Discovery(tuple(found[TranscriptFile]), tuple(found[SideFile]), tuple(unclassified), tuple(errors))


def chunk_lines(body: bytes, base: int, limit: int = SEGMENT_BYTES) -> Iterator[tuple[int, bytes]]:
    """Split body (file bytes starting at offset `base`) into segment payloads.

    Each chunk ends just after the first newline at or past `limit` bytes, or
    at the end of body. So every segment ends with a newline except a settled
    tail, and no line is split across segments except a settled tail and its
    later continuation."""
    start = 0
    while start < len(body):
        newline = body.find(b"\n", start + limit - 1)
        end = len(body) if newline < 0 else newline + 1
        yield base + start, body[start:end]
        start = end


def lines(chunk: bytes, base: int) -> Iterator[tuple[int, bytes]]:
    """(file offset, line without newline) for each line of one segment
    payload. An unterminated tail is yielded as a line. Empty lines skipped."""
    start = 0
    while start < len(chunk):
        end = chunk.find(b"\n", start)
        if end < 0:
            end = len(chunk)
        if end > start:
            yield base + start, chunk[start:end]
        start = end + 1


def _records(segments: Iterable[tuple[int, bytes]], after_newline: bool = True) -> Iterator[tuple[int, Record]]:
    """Every line of consecutive segments as (file offset, Record).

    A segment that follows one without a trailing newline starts with the
    torn continuation of a settled tail. That line is Unparsed whatever its
    bytes say, because a later writer may have glued a whole record onto the
    torn bytes. Ingest and rebuild both read lines through here, so they
    agree on every line."""
    for start, chunk in segments:
        for off, line in lines(chunk, start):
            yield off, parse(line) if after_newline or off != start else Unparsed()
        after_newline = chunk.endswith(b"\n")


@dataclass(frozen=True, slots=True)
class Filters:
    project: str | None = None  # case-insensitive substring of the session's cwd
    since: Millis | None = None  # inclusive
    until: Millis | None = None  # exclusive


@dataclass(frozen=True, slots=True)
class Query:
    terms: tuple[str, ...]  # as typed; a term with spaces is a phrase; trailing * is a prefix
    filters: Filters = Filters()
    tools: bool = False  # also search tool calls and tool output
    limit: int = 20  # sessions, not hits


@dataclass(frozen=True, slots=True)
class SessionSummary:
    id: SessionId
    cwd: str | None
    title: str | None  # custom-title, else desktop-app title, else latest ai-title, else first prompt
    first_ts: Millis | None
    last_ts: Millis | None
    prompts: int
    subagents: int
    transcript: Path  # main transcript path; may no longer exist
    on_disk: bool  # stat at read time; derived, never stored


@dataclass(frozen=True, slots=True)
class Subagent:
    id: AgentId
    description: str | None  # from its .meta.json doc
    agent_type: str | None
    first_ts: Millis | None


@dataclass(frozen=True, slots=True)
class Session:
    summary: SessionSummary
    agents: tuple[Subagent, ...]


@dataclass(frozen=True, slots=True)
class Excerpt:
    kind: Kind
    ts: Millis | None
    agent_id: AgentId | None  # set when the hit is inside a subagent transcript
    text: str  # ~160 chars around the first matched term, single line
    marks: tuple[tuple[int, int], ...]  # [start, end) spans of matched terms in text


@dataclass(frozen=True, slots=True)
class SessionHit:
    session: SessionSummary
    hits: int  # matching units in this session (main + subagents)
    best: Excerpt  # highest bm25-ranked unit


@dataclass(frozen=True, slots=True)
class Message:
    ts: Millis | None
    kind: Kind  # PROMPT | REPLY | TOOL_CALL | TOOL_OUTPUT
    text: str  # full text; display truncation is the caller's choice
    tool: str | None = None
    spawned: AgentId | None = None  # TOOL_CALL that started this subagent


@dataclass(frozen=True, slots=True)
class SweepReport:
    started: Millis
    finished: Millis
    transcripts_seen: int = 0
    transcripts_changed: int = 0
    bytes_archived: int = 0
    lines_archived: int = 0
    new_generations: int = 0
    docs_changed: int = 0
    errors: tuple[str, ...] = ()
    busy: bool = False  # wait=False and another writer held the lock; stopped early
    refused: str | None = None  # archive belongs to a newer claudinator


@dataclass(frozen=True, slots=True)
class Pending:
    path: Path
    unarchived_bytes: int
    mtime: Millis


@dataclass(frozen=True, slots=True)
class Status:
    db_path: Path
    db_bytes: int
    archived_bytes: int  # sum of byte_end - byte_start
    stored_bytes: int  # sum of length(zstd)
    sessions: int
    transcripts: int
    gone: int  # main transcripts no longer on disk (still in the archive)
    multi_generation: int  # files that were rewritten at least once
    unparsed_lines: int
    pending: tuple[Pending, ...]  # on-disk bytes not yet archived
    unclassified: tuple[Path, ...]  # *.jsonl under projects/ in no known layout, so never archived
    last_sweep: SweepReport | None  # the last sweep that waited for the lock


class ArchiveError(Exception):
    """Base for errors the CLI turns into a message and exit code 1."""


class UnknownSession(ArchiveError):
    pass


class AmbiguousSession(ArchiveError):
    def __init__(self, ref: str, candidates: list[SessionSummary]) -> None:
        super().__init__(f"{ref} matches {len(candidates)} sessions")
        self.candidates = candidates


class NewerArchive(ArchiveError):
    """The file was written by a newer claudinator (higher archive_schema or
    projection_version). Reads proceed; writes are refused."""


class Locked(ArchiveError):
    """Another process held the write lock for LOCK_WAIT_MS."""


class _Busy(Exception):
    """Internal: BEGIN IMMEDIATE failed with SQLITE_BUSY under wait=False."""


@dataclass(frozen=True, slots=True)
class _Archived:
    """What the archive holds of one file: its latest generation."""

    source_id: int
    generation: int
    head: bytes  # file bytes [0, len(head)) when the generation began
    cursor: int  # archived end, from the last segment
    tail: bytes  # sha256 of file bytes [cursor - TAIL_BYTES, cursor), from 0 in a short file


class Plan(enum.Enum):
    UNCHANGED = "unchanged"  # the file is exactly what is archived
    APPEND = "append"  # the archived bytes are a prefix of the file, and more follow
    NEW_GENERATION = "new generation"  # the file was rewritten
    WAIT = "wait"  # shorter but with the same head: a restore still being written


def plan(head: bytes, tail: bytes, cursor: int, size: int, settled: bool, file_head: bytes, file_window: bytes) -> Plan:
    """How to ingest a file whose generation is archived up to cursor.

    file_head is the file's bytes [0, len(head)), fewer if the file is
    shorter. file_window is its bytes [cursor - TAIL_BYTES, cursor), read
    only when size >= cursor. A shorter file with the same head waits until
    it settles, because that is what a restore looks like while it is
    written. Any other shorter file, a changed head, or a changed window is
    a rewrite. A rewrite that keeps both the head and the window is not
    detected."""
    head_kept = file_head == head[: len(file_head)]
    if size < cursor:
        return Plan.NEW_GENERATION if settled or not head_kept else Plan.WAIT
    if not head_kept or hashlib.sha256(file_window).digest() != tail:
        return Plan.NEW_GENERATION
    return Plan.UNCHANGED if size == cursor else Plan.APPEND


def _look(fd: int, a: _Archived, size: int, settled: bool) -> tuple[Plan, bytes]:
    """plan() for an open file, and the window it compared."""
    n = min(TAIL_BYTES, a.cursor)
    window = _pread(fd, n, a.cursor - n) if size >= a.cursor else b""
    return plan(a.head, a.tail, a.cursor, size, settled, _pread(fd, len(a.head), 0), window), window


def _worth_locking(path: Path, a: _Archived | None) -> bool:
    """Outside any lock: would _ingest archive anything now? False when the
    file is unchanged, is a restore still being written, or has no complete
    new line and has not settled, which is every live session mid-write. So
    a read command takes the write lock only for files with work to do.
    _ingest decides again under the lock."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        st = os.fstat(fd)
        settled = _settled(st)
        start = 0
        if a is not None:
            match _look(fd, a, st.st_size, settled)[0]:
                case Plan.UNCHANGED | Plan.WAIT:
                    return False
                case Plan.APPEND:
                    start = a.cursor
        return settled or _has_newline(fd, start, st.st_size)
    finally:
        os.close(fd)


def _has_newline(fd: int, start: int, end: int, step: int = 1 << 16) -> bool:
    for offset in range(start, end, step):
        if b"\n" in os.pread(fd, min(step, end - offset), offset):
            return True
    return False


def _settled(st: os.stat_result) -> bool:
    return _now() - st.st_mtime_ns // 1_000_000 >= SETTLE_SECONDS * 1000


@dataclass(frozen=True, slots=True)
class _Delta:
    bytes: int
    lines: int = 0
    rewritten: bool = False


def _now() -> Millis:
    return Millis(time.time_ns() // 1_000_000)


def _projection_stamp() -> dict[str, str]:
    """What meta records about the projection it holds: the version of the
    rules and a digest of the DDL, so a DDL edit rebuilds without anyone
    remembering to bump the version."""
    return {
        "projection_version": str(PROJECTION_VERSION),
        "projection_ddl": hashlib.sha256(_PROJECTION_DDL.encode()).hexdigest(),
    }


def _create(path: Path) -> None:
    """Build a new database in a private file and publish it whole.

    The file is made with mode 0600, so its -wal and -shm files are too, and
    gets the schema, WAL mode, and its version stamps before any other
    process can see it. os.link then publishes it only if the path is still
    free. A process that loses that race uses the winner's database, and no
    process ever opens a half-made archive or switches its journal mode."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = path.with_name(f".{path.name}.new-{os.getpid()}")
    for leftover in (private, Path(f"{private}-wal"), Path(f"{private}-shm")):
        leftover.unlink(missing_ok=True)  # only a dead process with this pid can have left these
    os.close(os.open(private, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        db = sqlite3.connect(private, autocommit=True)
        try:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(f"BEGIN; {_ARCHIVE_DDL} {_PROJECTION_DDL} COMMIT;")
            db.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("archive_schema", str(ARCHIVE_SCHEMA)), *_projection_stamp().items()],
            )
        finally:
            db.close()
        try:
            os.link(private, path)
        except FileExistsError:
            pass
    finally:
        private.unlink(missing_ok=True)


def _pread(fd: int, n: int, offset: int) -> bytes:
    """Exactly n bytes at offset, or fewer only at end of file."""
    parts = []
    while n > 0:
        part = os.pread(fd, min(n, 1 << 30), offset)
        if not part:
            break
        parts.append(part)
        n -= len(part)
        offset += len(part)
    return b"".join(parts)


@dataclass
class Archive:
    """The archive. Every public method is safe to call from many processes at
    once: SQLite's write lock is the only serialization, held per transcript
    delta, and every write transaction re-reads the state it depends on after
    taking the lock.

    Hides: segments, zstd, derived cursors, generations, the projection and
    its version, FTS syntax, path layouts, desktop/meta side files."""

    cfg: Config
    _db: sqlite3.Connection = field(repr=False)

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, cfg: Config) -> Archive:
        """Open the database, creating it if it does not exist, and make the
        projection current. When both versions are current this takes no
        write lock, so a reader never waits for a sweep in another process."""
        if not cfg.db.exists():
            _create(cfg.db)
        db = sqlite3.connect(cfg.db.as_uri() + "?mode=rw", uri=True, autocommit=True)
        try:
            db.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute("PRAGMA foreign_keys = ON")
            archive = cls(cfg, db)
            archive._converge()
        except BaseException:
            db.close()
            raise
        return archive

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _converge(self) -> None:
        schema, projection = self._meta_int("archive_schema"), self._meta_int("projection_version")
        if schema is None or projection is None:
            raise ArchiveError(f"{self.cfg.db} is not a claudinator archive")
        if schema < ARCHIVE_SCHEMA:
            raise ArchiveError(
                f"{self.cfg.db} has archive schema {schema}, and this claudinator reads schema {ARCHIVE_SCHEMA} only"
            )
        if schema == ARCHIVE_SCHEMA and projection <= PROJECTION_VERSION and not self._projection_current():
            try:
                self.rebuild()
            except NewerArchive:
                pass

    # -- writing ------------------------------------------------------------

    def sweep(self, *, wait: bool = True, progress: Callable[[Path, int], None] | None = None) -> SweepReport:
        """Archive every byte on disk that is not archived yet. Idempotent:
        a second run with no file changes archives nothing.

        wait=True (launchd, install, `claudinator sweep`): block on the write
        lock up to LOCK_WAIT_MS per file, and record the report as the last
        sweep. wait=False (every read command): give up at the first busy
        lock and report busy=True; the reader proceeds on what is already
        archived.

        A file that fails for any reason becomes one entry in errors and the
        sweep moves on, so one bad file never keeps the rest unarchived."""
        started = _now()
        known = self._archived()
        found = discover(self.cfg)
        seen = len(found.transcripts)
        changed = archived = archived_lines = generations = docs_changed = 0
        errors = list(found.errors)
        busy = False
        refused = None
        for loc in found.transcripts:
            try:
                if not _worth_locking(loc.path, known.get(loc.path)):
                    continue
                delta = self._ingest(loc, wait)
            except FileNotFoundError:
                continue
            except _Busy:
                busy = True
                break
            except NewerArchive as e:
                refused = str(e)
                break
            except Locked as e:
                errors.append(str(e))
                break
            except Exception as e:
                errors.append(f"{loc.path}: {e!r}")
                continue
            if delta.bytes:
                changed += 1
                archived += delta.bytes
                archived_lines += delta.lines
                generations += delta.rewritten
            if progress:
                progress(loc.path, delta.bytes)
        else:
            try:
                docs_changed, doc_errors = self._store_docs(found.side_files, wait)
                errors += doc_errors
            except _Busy:
                busy = True
            except NewerArchive as e:
                refused = str(e)
            except Exception as e:
                errors.append(f"side files: {e!r}")
        report = SweepReport(
            started=started,
            finished=_now(),
            transcripts_seen=seen,
            transcripts_changed=changed,
            bytes_archived=archived,
            lines_archived=archived_lines,
            new_generations=generations,
            docs_changed=docs_changed,
            errors=tuple(errors),
            busy=busy,
            refused=refused,
        )
        if wait and not refused:
            try:
                with self._write(wait=True):
                    self._set_meta("last_sweep", json.dumps(asdict(report)))
            except NewerArchive as e:
                report = replace(report, refused=str(e))
            except Exception as e:
                report = replace(report, errors=(*report.errors, f"recording this sweep: {e!r}"))
        return report

    def rebuild(self, *, force: bool = False) -> None:
        """Drop and re-derive the projection from the archive in one write
        transaction. open() runs it after an upgrade; `claudinator rebuild`
        runs it with force=True. Readers in other processes keep seeing the
        old projection (WAL snapshot) until commit."""
        with self._write(wait=True):
            if not force and self._projection_current():
                return
            self._db.executescript(_PROJECTION_DDL)
            for (source_id,) in self._db.execute("SELECT id FROM source ORDER BY id").fetchall():
                self._project(source_id, _records(self._segments(source_id)))
            for key, value in _projection_stamp().items():
                self._set_meta(key, value)

    # -- reading ------------------------------------------------------------

    def search(self, q: Query) -> list[SessionHit]:
        """Sessions containing every term, best first, with the best-ranked
        unit of each rendered as an excerpt from its raw line."""
        if not q.terms:
            return []
        kinds = CONVERSATION | (TOOLS if q.tools else frozenset())
        rows = self._db.execute(
            _SEARCH_SQL,
            {
                "expr": _fts_expr(q.terms),
                "kinds": json.dumps(sorted(kinds)),
                "since": q.filters.since,
                "until": q.filters.until,
            },
        )
        sessions = {row[0]: row for row in self._session_rows(Filters(project=q.filters.project), None)}
        groups: dict[str, list[tuple]] = {}
        for row in rows:
            if row[0] in sessions:
                groups.setdefault(row[0], []).append(row)
        hits = []
        for session_id, group in list(groups.items())[: q.limit]:
            _, source_id, line_off, block, kind, ts, agent_id = group[0]
            text, marks = _excerpt(self._entry_text(source_id, line_off, block), q.terms)
            excerpt = Excerpt(Kind(kind), ts, agent_id, text, marks)
            hits.append(SessionHit(self._summary(sessions[session_id]), len(group), excerpt))
        return hits

    def sessions(self, filters: Filters = Filters(), limit: int | None = 30) -> list[SessionSummary]:
        """Sessions newest-activity first. since/until select sessions whose
        [first_ts, last_ts] overlaps the window."""
        return [self._summary(row) for row in self._session_rows(filters, limit)]

    def session(self, ref: str) -> Session:
        """Resolve a unique session-id prefix and return the session with its
        subagents. Raises UnknownSession or AmbiguousSession."""
        rows = self._session_rows(Filters(), None, prefix=ref)
        if not rows:
            raise UnknownSession(f"no archived session starts with {ref}")
        if len(rows) > 1:
            raise AmbiguousSession(ref, [self._summary(row) for row in rows])
        summary = self._summary(rows[0])
        metas = {aid: subagent_meta(body) for aid, body in self._docs(summary.id, DocRole.SUBAGENT_META)}
        agents = []
        for aid, first_ts in self._db.execute(
            "SELECT s.agent_id, MIN(t.first_ts) FROM source s JOIN transcript t ON t.source_id = s.id"
            " WHERE s.session_id = ? AND s.agent_id IS NOT NULL GROUP BY s.agent_id ORDER BY 2, 1",
            (summary.id,),
        ):
            meta = metas.get(aid)
            agents.append(Subagent(aid, meta and meta.description, meta and meta.agent_type, first_ts))
        return Session(summary, tuple(agents))

    def conversation(self, session_id: SessionId, agent_id: AgentId | None = None) -> Iterator[Message]:
        """The transcript in file order, from the archive, whether or not the
        file still exists on disk."""
        source_id = self._source(session_id, agent_id)
        spawned = {}
        for aid, body in self._docs(session_id, DocRole.SUBAGENT_META):
            if tool_use_id := subagent_meta(body).tool_use_id:
                spawned[tool_use_id] = aid
        for _, rec in _records(self._segments(source_id)):
            if isinstance(rec, Turn):
                for _, b in rec.blocks:
                    yield Message(rec.ts, b.kind, b.text, b.tool, spawned.get(b.tool_use_id))

    def export(
        self,
        session_id: SessionId,
        out: BinaryIO,
        agent_id: AgentId | None = None,
        generation: int | None = None,
    ) -> int:
        """Write the archived bytes of a transcript, byte for byte as Claude
        Code wrote them: the latest generation, or the one named. Returns
        bytes written. This is what makes `claude --resume` possible after
        Claude Code's cleanup deleted the file."""
        written = 0
        for _, chunk in self._segments(self._source(session_id, agent_id, generation)):
            out.write(chunk)
            written += len(chunk)
        return written

    def status(self) -> Status:
        archived, stored = self._db.execute(
            "SELECT COALESCE(SUM(byte_end - byte_start), 0), COALESCE(SUM(length(zstd)), 0) FROM segment"
        ).fetchone()
        sessions, transcripts, rewritten = self._db.execute(
            "SELECT COUNT(DISTINCT session_id), COUNT(DISTINCT path),"
            " COUNT(DISTINCT CASE WHEN generation > 0 THEN path END) FROM source"
        ).fetchone()
        (unparsed,) = self._db.execute("SELECT COALESCE(SUM(unparsed), 0) FROM transcript").fetchone()
        mains = [Path(p) for (p,) in self._db.execute("SELECT DISTINCT path FROM source WHERE agent_id IS NULL")]
        known = self._archived()
        found = discover(self.cfg)
        pending = []
        for loc in found.transcripts:
            try:
                st = loc.path.stat()
            except FileNotFoundError:
                continue
            if (unarchived := st.st_size - (a.cursor if (a := known.get(loc.path)) else 0)) > 0:
                pending.append(Pending(loc.path, unarchived, Millis(st.st_mtime_ns // 1_000_000)))
        last = self._meta("last_sweep")
        return Status(
            db_path=self.cfg.db,
            db_bytes=sum(p.stat().st_size for p in (self.cfg.db, Path(f"{self.cfg.db}-wal")) if p.exists()),
            archived_bytes=archived,
            stored_bytes=stored,
            sessions=sessions,
            transcripts=transcripts,
            gone=sum(not p.exists() for p in mains),
            multi_generation=rewritten,
            unparsed_lines=unparsed,
            pending=tuple(pending),
            unclassified=found.unclassified,
            last_sweep=_report(json.loads(last)) if last else None,
        )

    # -- private -----------------------------------------------------------

    @contextmanager
    def _write(self, *, wait: bool) -> Iterator[None]:
        """One write transaction. The connection is in autocommit mode, so
        every statement inside, DDL included, joins this transaction.

        Raises NewerArchive when a newer claudinator has stamped the file,
        checked under the lock because one may upgrade it while this
        process has it open."""
        self._db.execute(f"PRAGMA busy_timeout = {LOCK_WAIT_MS if wait else 0}")
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if e.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            if not wait:
                raise _Busy from e
            raise Locked(
                f"another claudinator process is writing to the archive and held its lock for"
                f" {LOCK_WAIT_MS / 1000:g} s; try again when it finishes"
            ) from e
        finally:
            self._db.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        try:
            if (self._meta_int("archive_schema") or 0) > ARCHIVE_SCHEMA or (
                self._meta_int("projection_version") or 0
            ) > PROJECTION_VERSION:
                raise NewerArchive("this archive was written by a newer claudinator")
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:  # SQLite rolls back by itself on some errors (disk full, I/O)
                self._db.execute("ROLLBACK")
            raise

    def _ingest(self, f: TranscriptFile, wait: bool) -> _Delta:
        """Archive and project the new complete lines of one transcript.

        Concurrency: the read of the archived state, the read of the file,
        and the writes all happen inside one BEGIN IMMEDIATE transaction. A
        second process ingesting the same file blocks on the lock, then
        re-reads a cursor that already covers the first one's bytes and
        archives only what is newer, usually nothing. The segment primary key
        and the contiguity trigger turn any bug that would double- or
        skip-archive into a failed transaction.

        Crash safety: segment rows, entry and FTS rows, and transcript facts
        commit together. A crash before COMMIT leaves no trace; the next
        sweep redoes the same delta from the same cursor."""
        with self._write(wait=wait):
            a = self._archived(f.path).get(f.path)
            try:
                fd = os.open(f.path, os.O_RDONLY)
            except FileNotFoundError:
                return _Delta(0)
            try:
                st = os.fstat(fd)
                settled = _settled(st)
                generation, cursor, window = 0, 0, b""
                if a is not None:
                    match _look(fd, a, st.st_size, settled):
                        case (Plan.UNCHANGED | Plan.WAIT, _):
                            return _Delta(0)
                        case (Plan.APPEND, window):
                            cursor = a.cursor
                        case (Plan.NEW_GENERATION, _):
                            generation = a.generation + 1
                buf = _pread(fd, st.st_size - cursor, cursor)
            finally:
                os.close(fd)
            take = len(buf) if settled else buf.rfind(b"\n") + 1
            if take == 0:
                return _Delta(0)
            body = buf[:take]
            now = _now()
            if cursor == 0:
                (source_id,) = self._db.execute(
                    "INSERT INTO source (path, generation, session_id, agent_id, head, first_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (str(f.path), generation, f.session_id, f.agent_id, body[:1024], now),
                ).fetchone()
            else:
                source_id = a.source_id
            prior = window  # the file's last TAIL_BYTES before each segment
            for start, chunk in chunk_lines(body, cursor):
                prior = (prior + chunk[-TAIL_BYTES:])[-TAIL_BYTES:]
                self._db.execute(
                    "INSERT INTO segment (source_id, byte_start, byte_end, tail, zstd, archived_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (source_id, start, start + len(chunk), hashlib.sha256(prior).digest(), zstd.compress(chunk), now),
                )
                self._project(source_id, _records([(start, chunk)], not window or window.endswith(b"\n")))
                window = chunk
            return _Delta(take, body.count(b"\n") + (not body.endswith(b"\n")), generation > 0 and cursor == 0)

    def _project(self, source_id: int, records: Iterable[tuple[int, Record]]) -> None:
        """Fold records into the projection. Shared by _ingest and rebuild;
        must run inside a write transaction.

        Total: a record whose projection raises, or yields a str SQLite
        cannot store, counts as unparsed. The projection is disposable and
        must never keep a segment from committing."""
        row = self._db.execute(
            "SELECT cwd, first_ts, last_ts, ai_title, custom_title, unparsed FROM transcript WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        facts = Facts(*row) if row else Facts()
        for off, rec in records:
            try:
                folded, drafts = project(facts, rec)
                rows = [(d, text_of(rec, d.block)) for d in drafts]
                for text in (folded.cwd, folded.ai_title, folded.custom_title, *(text for _, text in rows)):
                    if text is not None:
                        text.encode()
            except Exception:
                folded, rows = project(facts, Unparsed())
            facts = folded
            for d, text in rows:
                (entry_id,) = self._db.execute(
                    "INSERT INTO entry (source_id, line_off, block, kind, ts) VALUES (?, ?, ?, ?, ?) RETURNING id",
                    (source_id, off, d.block, d.kind.value, d.ts),
                ).fetchone()
                self._db.execute("INSERT INTO entry_fts (rowid, text) VALUES (?, ?)", (entry_id, text))
        self._db.execute(
            "INSERT INTO transcript (source_id, cwd, first_ts, last_ts, ai_title, custom_title, unparsed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source_id) DO UPDATE SET cwd = excluded.cwd, first_ts = excluded.first_ts,"
            " last_ts = excluded.last_ts, ai_title = excluded.ai_title, custom_title = excluded.custom_title,"
            " unparsed = excluded.unparsed",
            (source_id, facts.cwd, facts.first_ts, facts.last_ts, facts.ai_title, facts.custom_title, facts.unparsed),
        )

    def _archived(self, path: Path | None = None) -> dict[Path, _Archived]:
        """The latest generation of every archived file, or of one. Outside a
        lock this is a hint for filtering; _ingest reads it again under the
        lock. The cursor and tail come from the last segment through the
        primary key; nothing about them is stored separately."""
        rows = self._db.execute(
            "SELECT s.path, s.id, s.generation, s.head, g.byte_end, g.tail"
            "  FROM source s"
            "  JOIN segment g ON g.source_id = s.id"
            "   AND g.byte_start = (SELECT MAX(byte_start) FROM segment WHERE source_id = s.id)"
            " WHERE (?1 IS NULL OR s.path = ?1)"
            "   AND s.generation = (SELECT MAX(generation) FROM source t WHERE t.path = s.path)",
            (None if path is None else str(path),),
        )
        return {Path(row[0]): _Archived(*row[1:]) for row in rows}

    def _segments(self, source_id: int) -> Iterator[tuple[int, bytes]]:
        """(byte_start, payload) of every segment of a source in file order."""
        for start, blob in self._db.execute(
            "SELECT byte_start, zstd FROM segment WHERE source_id = ? ORDER BY byte_start", (source_id,)
        ):
            yield start, zstd.decompress(blob)

    def _session_rows(self, filters: Filters, limit: int | None, prefix: str | None = None) -> list[tuple]:
        return self._db.execute(
            _SESSIONS_SQL,
            {
                "prompt": Kind.PROMPT.value,
                "desktop": DocRole.DESKTOP.value,
                "project": filters.project,
                "since": filters.since,
                "until": filters.until,
                "prefix": prefix,
                "limit": -1 if limit is None else limit,
            },
        ).fetchall()

    def _summary(self, row: tuple) -> SessionSummary:
        sid, cwd, custom, desktop_body, ai, first_prompt, first_ts, last_ts, prompts, subagents, path = row
        desktop = desktop_session(desktop_body) if desktop_body else None
        title = custom or (desktop and desktop.title) or ai
        if title is None and first_prompt is not None:
            entry = self._db.execute("SELECT source_id, line_off, block FROM entry WHERE id = ?", (first_prompt,))
            title = " ".join(self._entry_text(*entry.fetchone()).split())[:200]
        return SessionSummary(sid, cwd, title, first_ts, last_ts, prompts, subagents, Path(path), os.path.exists(path))

    def _entry_text(self, source_id: int, line_off: int, block: int) -> str:
        return text_of(parse(self._line_at(source_id, line_off)), block)

    def _line_at(self, source_id: int, offset: int) -> bytes:
        """One line by file offset: the segment with the greatest byte_start
        <= offset, decompressed, sliced to the next newline."""
        start, blob = self._db.execute(
            "SELECT byte_start, zstd FROM segment WHERE source_id = ? AND byte_start <= ?"
            " ORDER BY byte_start DESC LIMIT 1",
            (source_id, offset),
        ).fetchone()
        chunk = zstd.decompress(blob)
        end = chunk.find(b"\n", offset - start)
        return chunk[offset - start : end if end >= 0 else len(chunk)]

    def _docs(self, session_id: SessionId, role: DocRole) -> list[tuple[AgentId | None, bytes]]:
        return self._db.execute(
            "SELECT agent_id, body FROM doc WHERE session_id = ? AND role = ? ORDER BY path", (session_id, role.value)
        ).fetchall()

    def _source(self, session_id: SessionId, agent_id: AgentId | None, generation: int | None = None) -> int:
        """The latest generation of a transcript, or the one named."""
        row = self._db.execute(
            "SELECT id FROM source WHERE session_id = ? AND agent_id IS ? AND (?3 IS NULL OR generation = ?3)"
            " ORDER BY id DESC LIMIT 1",
            (session_id, agent_id, generation),
        ).fetchone()
        if row is None:
            what = f"agent {agent_id} of session {session_id}" if agent_id else f"session {session_id}"
            raise UnknownSession(f"no archived transcript for {what}" + (f" generation {generation}" if generation is not None else ""))
        return row[0]

    def _store_docs(self, docs: Iterable[SideFile], wait: bool) -> tuple[int, list[str]]:
        """Store the latest bytes of each side file whose sha256 changed, in
        one transaction. A file whose size and mtime match what is stored is
        not read. DESKTOP docs get their session id from the body. Returns
        how many bodies changed and what could not be read."""
        rows = self._db.execute("SELECT path, size, mtime_ns, sha256 FROM doc")
        known = {path: (size, mtime_ns, sha) for path, size, mtime_ns, sha in rows}
        changed, restat, errors = [], [], []
        now = _now()
        for d in docs:
            stored = known.get(str(d.path))
            try:
                st = d.path.stat()  # before the read, so a write during it shows up as a change next time
                if stored is not None and stored[:2] == (st.st_size, st.st_mtime_ns):
                    continue
                body = d.path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError as e:
                errors.append(f"{d.path}: {e!r}")
                continue
            sha = hashlib.sha256(body).digest()
            if stored is not None and stored[2] == sha:
                restat.append((st.st_size, st.st_mtime_ns, str(d.path)))
                continue
            session_id = d.session_id
            if d.role is DocRole.DESKTOP and (desktop := desktop_session(body)) is not None:
                session_id = desktop.cli_session_id
            changed.append((str(d.path), d.role.value, session_id, d.agent_id, sha, body, st.st_size, st.st_mtime_ns, now))
        if not changed and not restat:
            return 0, errors
        with self._write(wait=wait):
            upserted = self._db.executemany(
                "INSERT INTO doc (path, role, session_id, agent_id, sha256, body, size, mtime_ns, archived_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (path) DO UPDATE SET role = excluded.role, session_id = excluded.session_id,"
                " agent_id = excluded.agent_id, sha256 = excluded.sha256, body = excluded.body,"
                " size = excluded.size, mtime_ns = excluded.mtime_ns, archived_at = excluded.archived_at"
                " WHERE excluded.sha256 IS NOT doc.sha256",
                changed,
            ).rowcount
            self._db.executemany("UPDATE doc SET size = ?, mtime_ns = ? WHERE path = ?", restat)
        return max(upserted, 0), errors

    def _projection_current(self) -> bool:
        return all(self._meta(key) == value for key, value in _projection_stamp().items())

    def _meta(self, key: str) -> str | None:
        if not self._db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'meta'").fetchone():
            return None
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _meta_int(self, key: str) -> int | None:
        value = self._meta(key)
        return int(value) if value is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


_SESSIONS_SQL = """
WITH main AS (
  SELECT MAX(id) AS id, session_id FROM source WHERE agent_id IS NULL GROUP BY session_id
),
span AS (
  SELECT s.session_id,
         MIN(t.first_ts) AS first_ts,
         MAX(t.last_ts)  AS last_ts,
         COUNT(DISTINCT s.agent_id) AS subagents
    FROM source s JOIN transcript t ON t.source_id = s.id
   GROUP BY s.session_id
)
SELECT m.session_id, t.cwd, t.custom_title,
       (SELECT d.body FROM doc d
         WHERE d.session_id = m.session_id AND d.role = :desktop
         ORDER BY d.mtime_ns DESC LIMIT 1) AS desktop,
       t.ai_title,
       (SELECT e.id FROM entry e
         WHERE e.source_id = m.id AND e.kind = :prompt
         ORDER BY e.line_off, e.block LIMIT 1) AS first_prompt,
       span.first_ts, span.last_ts,
       (SELECT COUNT(*) FROM entry e WHERE e.source_id = m.id AND e.kind = :prompt) AS prompts,
       span.subagents, s.path
  FROM main m
  JOIN source s ON s.id = m.id
  JOIN transcript t ON t.source_id = m.id
  JOIN span ON span.session_id = m.session_id
 WHERE (:project IS NULL OR instr(lower(t.cwd), lower(:project)) > 0)
   AND (:since IS NULL OR span.last_ts >= :since)
   AND (:until IS NULL OR span.first_ts < :until)
   AND (:prefix IS NULL OR m.session_id GLOB :prefix || '*')
 ORDER BY span.last_ts DESC, m.session_id
 LIMIT :limit
"""

_SEARCH_SQL = """
SELECT s.session_id, e.source_id, e.line_off, e.block, e.kind, e.ts, s.agent_id
  FROM entry_fts f
  JOIN entry e ON e.id = f.rowid
  JOIN source s ON s.id = e.source_id
 WHERE entry_fts MATCH :expr
   AND e.kind IN (SELECT value FROM json_each(:kinds))
   AND (:since IS NULL OR e.ts >= :since)
   AND (:until IS NULL OR e.ts < :until)
 ORDER BY f.rank
"""


def _fts_expr(terms: tuple[str, ...]) -> str:
    """Terms -> FTS5 MATCH expression that cannot be a syntax error.

    Each term becomes a quoted string ("" escapes "), a trailing * stays
    outside the quotes as a prefix query, and terms are joined with AND. So
    `claudinator search "launchd agent" sync*` -> '"launchd agent" AND "sync"*'.
    FTS5 operators are not exposed; nobody has to learn them."""
    quoted = []
    for term in terms:
        word = term.rstrip("*")
        quoted.append('"' + word.replace('"', '""') + '"' + ("*" if word != term else ""))
    return " AND ".join(quoted)


class _FoldTable(dict[int, str]):
    """str.translate table that folds case and diacritics one character at a
    time, so offsets in the folded text are offsets in the original. Agrees
    with unicode61 remove_diacritics 2 on the scripts this Mac's transcripts
    use. Filled on first sight of each character."""

    def __missing__(self, codepoint: int) -> str:
        folded = unicodedata.normalize("NFKD", chr(codepoint))[:1].lower()[:1]
        self[codepoint] = folded
        return folded


_FOLD = _FoldTable()
_TOKEN = re.compile(r"[^\W_]+")


def _term_pattern(term: str) -> re.Pattern[str] | None:
    """A typed term as a regex over folded text that matches what FTS5
    matches: consecutive whole tokens, the last one a prefix if the term ends
    with *, which then marks the whole token. So "launchd agent" also matches
    "launchd-agent"."""
    tokens = _TOKEN.findall(term.rstrip("*").translate(_FOLD))
    if not tokens:
        return None
    end = r"[^\W_]*" if term.endswith("*") else r"(?![^\W_])"
    return re.compile(r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, tokens)) + end)


def _excerpt(text: str, terms: tuple[str, ...], width: int = 160) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Window of `width` chars around the first match, whitespace collapsed,
    with the [start, end) spans of every match inside the window."""
    flat = " ".join(text.split())
    folded = flat.translate(_FOLD)
    spans = sorted(m.span() for t in terms if (p := _term_pattern(t)) for m in p.finditer(folded))
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    first = merged[0][0] if merged else 0
    start = max(0, min(first - width // 4, len(flat) - width))
    end = min(len(flat), start + width)
    lead = "..." if start > 0 else ""
    shift = len(lead) - start
    marks = tuple((s + shift, e + shift) for s, e in merged if start <= s and e <= end)
    return lead + flat[start:end] + ("..." if end < len(flat) else ""), marks


def _report(obj: dict) -> SweepReport:
    """The stored last-sweep JSON as a SweepReport. Keys this version does
    not know come from a newer claudinator and are dropped."""
    known = {f.name for f in fields(SweepReport)}
    return SweepReport(**{k: v for k, v in obj.items() if k in known} | {"errors": tuple(obj.get("errors", ()))})
