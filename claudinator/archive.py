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
from dataclasses import asdict, dataclass, field
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

_MIGRATIONS: tuple[str, ...] = ()
"""In-place archive-layer migrations, oldest first. Entry i takes a database
from archive schema i + 1 to i + 2. The archive layer is never dropped."""

ARCHIVE_SCHEMA = 1 + len(_MIGRATIONS)

SEGMENT_BYTES = 1 << 20
"""Target uncompressed segment size. Measured on 82.9 MB of this Mac's
transcripts with zstd level 3: per-line 2.9x, 128 KiB 4.2x, 1 MiB 4.8x,
whole-file 5.0x. 1 MiB keeps random access to one line at one ~1 ms
decompress while losing 4% versus whole files."""

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
    (re.compile(rf"[^/]+/{_UUID}/tool-results/.+"), lambda p, m: None),
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


def discover(cfg: Config) -> Iterator[Location]:
    """Walk projects/ and the desktop sessions dir; yield classify() hits.
    A missing directory yields nothing."""
    for root in (cfg.claude_dir / "projects", cfg.desktop_dir / "claude-code-sessions"):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if (loc := classify(Path(dirpath, name), cfg)) is not None:
                    yield loc


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


class _Busy(Exception):
    """Internal: BEGIN IMMEDIATE failed with SQLITE_BUSY under wait=False."""


@dataclass(frozen=True, slots=True)
class _Delta:
    bytes: int
    lines: int = 0
    rewritten: bool = False


def _now() -> Millis:
    return Millis(time.time_ns() // 1_000_000)


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
    _writable: bool = True

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, cfg: Config) -> Archive:
        """Open or create the database, converge its schema, and make the
        projection current. When both versions are current this takes no
        write lock, so a reader never waits for a sweep in another process."""
        cfg.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(cfg.db, autocommit=True)
        try:
            db.execute("PRAGMA busy_timeout = 5000")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode = WAL")
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
        if (schema or 0) > ARCHIVE_SCHEMA or (projection or 0) > PROJECTION_VERSION:
            self._writable = False
            return
        if schema == ARCHIVE_SCHEMA and projection == PROJECTION_VERSION:
            return
        with self._write(wait=True):
            self._db.executescript(_ARCHIVE_DDL)
            schema = self._meta_int("archive_schema")
            if schema is not None and schema > ARCHIVE_SCHEMA:
                self._writable = False
                return
            for script in _MIGRATIONS[(schema or ARCHIVE_SCHEMA) - 1 :]:
                self._db.executescript(script)
            self._set_meta("archive_schema", str(ARCHIVE_SCHEMA))
        try:
            self.rebuild()
        except NewerArchive:
            pass

    # -- writing ------------------------------------------------------------

    def sweep(self, *, wait: bool = True, progress: Callable[[Path, int], None] | None = None) -> SweepReport:
        """Archive every byte on disk that is not archived yet. Idempotent:
        a second run with no file changes archives nothing.

        wait=True (launchd, install, `claudinator sweep`): block on the write
        lock up to 60 s per file, and record the report as the last sweep.
        wait=False (every read command): give up at the first busy lock and
        report busy=True; the reader proceeds on what is already archived."""
        started = _now()
        if not self._writable:
            return SweepReport(started, _now(), refused="this archive was written by a newer claudinator")
        known = self._cursors()
        seen = changed = archived = archived_lines = generations = docs_changed = 0
        errors: list[str] = []
        docs: list[SideFile] = []
        busy = False
        for loc in discover(self.cfg):
            if isinstance(loc, SideFile):
                docs.append(loc)
                continue
            seen += 1
            try:
                size = loc.path.stat().st_size
            except FileNotFoundError:
                continue
            if known.get(loc.path) == size:
                continue
            try:
                delta = self._ingest(loc, wait)
            except _Busy:
                busy = True
                break
            except (OSError, sqlite3.DatabaseError) as e:
                errors.append(f"{loc.path}: {e}")
                continue
            if delta.bytes:
                changed += 1
                archived += delta.bytes
                archived_lines += delta.lines
                generations += delta.rewritten
            if progress:
                progress(loc.path, delta.bytes)
        if not busy:
            try:
                docs_changed, doc_errors = self._store_docs(docs, wait)
                errors += doc_errors
            except _Busy:
                busy = True
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
        )
        if wait:
            with self._write(wait=True):
                self._set_meta("last_sweep", json.dumps(asdict(report)))
        return report

    def rebuild(self, *, force: bool = False) -> None:
        """Drop and re-derive the projection from the archive in one write
        transaction. open() runs it after an upgrade; `claudinator rebuild`
        runs it with force=True. Readers in other processes keep seeing the
        old projection (WAL snapshot) until commit."""
        if not self._writable:
            raise NewerArchive("this archive was written by a newer claudinator")
        with self._write(wait=True):
            stored = self._meta_int("projection_version")
            if stored is not None and stored > PROJECTION_VERSION:
                self._writable = False
                raise NewerArchive("this archive was written by a newer claudinator")
            if not force and stored == PROJECTION_VERSION:
                return
            self._db.executescript(_PROJECTION_DDL)
            for (source_id,) in self._db.execute("SELECT id FROM source ORDER BY id").fetchall():
                self._project(source_id, _records(self._segments(source_id)))
            self._set_meta("projection_version", str(PROJECTION_VERSION))

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
        known = self._cursors()
        pending = []
        for loc in discover(self.cfg):
            if not isinstance(loc, TranscriptFile):
                continue
            try:
                st = loc.path.stat()
            except FileNotFoundError:
                continue
            if (unarchived := st.st_size - known.get(loc.path, 0)) > 0:
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
            last_sweep=_report(json.loads(last)) if last else None,
        )

    # -- private -----------------------------------------------------------

    @contextmanager
    def _write(self, *, wait: bool) -> Iterator[None]:
        """One write transaction. The connection is in autocommit mode, so
        every statement inside, DDL included, joins this transaction."""
        self._db.execute(f"PRAGMA busy_timeout = {60_000 if wait else 0}")
        try:
            self._db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if not wait and e.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                raise _Busy from e
            raise
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _ingest(self, f: TranscriptFile, wait: bool) -> _Delta:
        """Archive and project the new complete lines of one transcript.

        Concurrency: the read of the cursor, the read of the file, and the
        writes all happen inside one BEGIN IMMEDIATE transaction. A second
        process ingesting the same file blocks on the lock, then re-reads a
        cursor that already covers the first one's bytes and archives only
        what is newer, usually nothing. The segment primary key and the
        contiguity trigger turn any bug that would double- or skip-archive
        into a failed transaction.

        Crash safety: segment rows, entry and FTS rows, and transcript facts
        commit together. A crash before COMMIT leaves no trace; the next
        sweep redoes the same delta from the same cursor."""
        with self._write(wait=wait):
            src = self._db.execute(
                "SELECT id, generation, head FROM source WHERE path = ? ORDER BY generation DESC LIMIT 1",
                (str(f.path),),
            ).fetchone()
            try:
                fd = os.open(f.path, os.O_RDONLY)
            except FileNotFoundError:
                return _Delta(0)
            try:
                st = os.fstat(fd)
                settled = _now() - st.st_mtime_ns // 1_000_000 >= SETTLE_SECONDS * 1000
                cursor = self._cursor(src[0]) if src else 0
                generation, rewritten = 0, False
                if src is not None:
                    n = min(len(src[2]), st.st_size)
                    head_changed = _pread(fd, n, 0) != src[2][:n]
                    shrank = st.st_size < cursor
                    if shrank and not head_changed and not settled:
                        # Same head but shorter: a restore through export may be
                        # mid-write. Decide once the file has settled.
                        return _Delta(0)
                    if head_changed or shrank:
                        generation, rewritten = src[1] + 1, True
                        src, cursor = None, 0
                buf = _pread(fd, st.st_size - cursor, cursor)
            finally:
                os.close(fd)
            take = len(buf) if settled else buf.rfind(b"\n") + 1
            if take == 0:
                return _Delta(0)
            body = buf[:take]
            now = _now()
            if src is None:
                (source_id,) = self._db.execute(
                    "INSERT INTO source (path, generation, session_id, agent_id, head, first_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (str(f.path), generation, f.session_id, f.agent_id, body[:1024], now),
                ).fetchone()
                after_newline = True
            else:
                source_id = src[0]
                after_newline = self._ends_with_newline(source_id)
            for start, chunk in chunk_lines(body, cursor):
                self._db.execute(
                    "INSERT INTO segment (source_id, byte_start, byte_end, zstd, archived_at) VALUES (?, ?, ?, ?, ?)",
                    (source_id, start, start + len(chunk), zstd.compress(chunk), now),
                )
                self._project(source_id, _records([(start, chunk)], after_newline))
                after_newline = chunk.endswith(b"\n")
            return _Delta(take, body.count(b"\n") + (not body.endswith(b"\n")), rewritten)

    def _project(self, source_id: int, records: Iterable[tuple[int, Record]]) -> None:
        """Fold records into the projection. Shared by _ingest and rebuild;
        must run inside a write transaction."""
        row = self._db.execute(
            "SELECT cwd, first_ts, last_ts, ai_title, custom_title, unparsed FROM transcript WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        facts = Facts(*row) if row else Facts()
        for off, rec in records:
            facts, drafts = project(facts, rec)
            for d in drafts:
                (entry_id,) = self._db.execute(
                    "INSERT INTO entry (source_id, line_off, block, kind, ts) VALUES (?, ?, ?, ?, ?) RETURNING id",
                    (source_id, off, d.block, d.kind.value, d.ts),
                ).fetchone()
                self._db.execute("INSERT INTO entry_fts (rowid, text) VALUES (?, ?)", (entry_id, text_of(rec, d.block)))
        self._db.execute(
            "INSERT INTO transcript (source_id, cwd, first_ts, last_ts, ai_title, custom_title, unparsed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (source_id) DO UPDATE SET cwd = excluded.cwd, first_ts = excluded.first_ts,"
            " last_ts = excluded.last_ts, ai_title = excluded.ai_title, custom_title = excluded.custom_title,"
            " unparsed = excluded.unparsed",
            (source_id, facts.cwd, facts.first_ts, facts.last_ts, facts.ai_title, facts.custom_title, facts.unparsed),
        )

    def _cursor(self, source_id: int) -> int:
        """The archived end of a source, served by the segment primary key.
        Derived, never stored."""
        row = self._db.execute(
            "SELECT byte_end FROM segment WHERE source_id = ? ORDER BY byte_start DESC LIMIT 1", (source_id,)
        ).fetchone()
        return row[0] if row else 0

    def _cursors(self) -> dict[Path, int]:
        """{path: cursor} for the latest generation of every source. Used
        outside any lock as a fast-path filter only; _ingest re-reads."""
        rows = self._db.execute(
            "SELECT s.path, (SELECT byte_end FROM segment WHERE source_id = s.id ORDER BY byte_start DESC LIMIT 1)"
            "  FROM source s"
            " WHERE s.generation = (SELECT MAX(generation) FROM source g WHERE g.path = s.path)"
        )
        return {Path(path): cursor for path, cursor in rows}

    def _ends_with_newline(self, source_id: int) -> bool:
        (blob,) = self._db.execute(
            "SELECT zstd FROM segment WHERE source_id = ? ORDER BY byte_start DESC LIMIT 1", (source_id,)
        ).fetchone()
        return zstd.decompress(blob).endswith(b"\n")

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

    def _docs(self, session_id: SessionId, role: DocRole) -> list[tuple[AgentId | None, str]]:
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

    def _store_docs(self, docs: list[SideFile], wait: bool) -> tuple[int, list[str]]:
        """Upsert the latest body of each side file whose sha256 changed, in
        one transaction. DESKTOP docs get their session id from the body.
        Returns how many changed and what could not be read."""
        known = {path: sha for path, sha in self._db.execute("SELECT path, sha256 FROM doc")}
        rows, errors = [], []
        now = _now()
        for d in docs:
            try:
                data = d.path.read_bytes()
                mtime = d.path.stat().st_mtime_ns // 1_000_000
            except FileNotFoundError:
                continue
            except OSError as e:
                errors.append(f"{d.path}: {e}")
                continue
            sha = hashlib.sha256(data).digest()
            if known.get(str(d.path)) == sha:
                continue
            try:
                body = data.decode()
            except UnicodeDecodeError:
                errors.append(f"{d.path}: not UTF-8 text")
                continue
            session_id = d.session_id
            if d.role is DocRole.DESKTOP and (desktop := desktop_session(body)) is not None:
                session_id = desktop.cli_session_id
            rows.append((str(d.path), d.role.value, session_id, d.agent_id, sha, body, mtime, now))
        if rows:
            with self._write(wait=wait):
                self._db.executemany(
                    "INSERT INTO doc (path, role, session_id, agent_id, sha256, body, mtime, archived_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (path) DO UPDATE SET role = excluded.role, session_id = excluded.session_id,"
                    " agent_id = excluded.agent_id, sha256 = excluded.sha256, body = excluded.body,"
                    " mtime = excluded.mtime, archived_at = excluded.archived_at"
                    " WHERE excluded.mtime >= doc.mtime",
                    rows,
                )
        return len(rows), errors

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
         ORDER BY d.mtime DESC LIMIT 1) AS desktop,
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
    return SweepReport(**{**obj, "errors": tuple(obj.get("errors", ()))})
