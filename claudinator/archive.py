"""The archive: the only owner of the SQLite file.

Ingests the bytes of the files layout.discover() finds as append-only zstd
segments, folds the projection, answers queries, exports bytes, and rebuilds
the projection. cli.py calls this module, which calls layout.py, record.py,
and search_text.py; nothing reaches past it into the database.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from importlib import resources
from pathlib import Path
from typing import BinaryIO

from compression import zstd

from claudinator.layout import Config, DocRole, SideFile, TranscriptFile, discover
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
from claudinator.search_text import excerpt, fts_expr, searchable

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


def _next_segment(fd: int, start: int, size: int, settled: bool) -> bytes:
    """The bytes of the next segment of a file, from start: through the
    first newline at or past SEGMENT_BYTES, else through the last newline
    before size, else, once the file has settled, through size. So every
    segment ends with a newline except a settled tail, and no line is split
    across segments except a settled tail and its later continuation. Reads
    at most one line past SEGMENT_BYTES, so memory follows the longest line,
    not the file."""
    buf = bytearray()
    while start + len(buf) < size:
        searched = len(buf)
        part = _pread(fd, min(SEGMENT_BYTES, size - start - len(buf)), start + len(buf))
        if not part:
            break
        buf += part
        if len(buf) >= SEGMENT_BYTES and (newline := buf.find(b"\n", max(searched, SEGMENT_BYTES - 1))) >= 0:
            return bytes(buf[: newline + 1])
    return bytes(buf if settled else buf[: buf.rfind(b"\n") + 1])


@dataclass(frozen=True, slots=True)
class _Open:
    """A line the segments so far end inside: a settled tail, archived
    before its writer finished the line."""

    off: int  # where the line's bytes so far begin
    data: bytes  # those bytes
    torn: bool  # projected as one Unparsed, not as a record


def _records(
    segments: Iterable[tuple[int, bytes]], before: Iterable[tuple[int, bytes]] = ()
) -> Iterator[tuple[int, Record, bool]]:
    """Every line of consecutive segments as (file offset, record, mends),
    in file order. Empty lines are skipped.

    A line lies inside one segment unless a settled tail was archived
    before its writer finished it; the line then goes on at the start of
    the next segment. Its bytes so far and the continuation are parsed as
    one line when that makes a record: a writer that paused for an hour.
    Else the continuation alone, when it is a record: a restarted writer
    that appended a whole record onto torn bytes. Else the line stays the
    one Unparsed its tail was counted as. mends is True for a record that
    replaces that Unparsed.

    before is the line the archive already ends inside, as its segments
    hold it (_open_line); its records were projected with those segments,
    so only what the new segments add is yielded. Ingest, rebuild,
    conversation, and search read lines through here, so they agree on
    every line."""
    left = None
    for start, chunk in before:
        _, left = _chunk_records(start, chunk, left)
    for start, chunk in segments:
        found, left = _chunk_records(start, chunk, left)
        yield from found


def _chunk_records(
    start: int, chunk: bytes, left: _Open | None
) -> tuple[list[tuple[int, Record, bool]], _Open | None]:
    """The records of one segment that follows a line left open, and the
    line it leaves open."""
    found = []
    pos = 0
    if left is not None:
        end = chunk.find(b"\n")
        rest = chunk[: end if end >= 0 else len(chunk)]
        pos = len(rest) + 1
        if not isinstance(whole := parse(left.data + rest), Unparsed):
            if left.torn:
                found.append((left.off, whole, True))
            left = _Open(left.off, left.data + rest, False)
        elif not isinstance(alone := parse(rest), Unparsed) or not left.torn:
            found.append((start, alone, False))  # glued onto torn bytes, or onto a record that lacked its newline
            left = _Open(start, rest, isinstance(alone, Unparsed))
        else:
            left = _Open(left.off, left.data + rest, True)
        if end >= 0:
            left = None
    while pos < len(chunk):
        end = chunk.find(b"\n", pos)
        if end < 0:
            rec = parse(chunk[pos:])
            found.append((start + pos, rec, False))
            left = _Open(start + pos, chunk[pos:], isinstance(rec, Unparsed))
            break
        if end > pos:
            found.append((start + pos, parse(chunk[pos:end]), False))
        pos = end + 1
    return found, left


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
    new_generations: int = 0
    docs_changed: int = 0
    errors: tuple[str, ...] = ()
    busy: bool = False  # wait=False and another writer held the lock; stopped early
    refused: str | None = None  # archive belongs to a newer claudinator


@dataclass(frozen=True, slots=True)
class Refusal:
    """A sweep that waited for the lock and was refused because a newer
    claudinator had stamped the archive: the hourly sweeper of an older
    install, usually."""

    at: Millis
    reason: str
    archive_schema: int  # of the claudinator that was refused
    projection_version: int


@dataclass(frozen=True, slots=True)
class Pending:
    """A file a sweep would archive bytes of now."""

    path: Path
    unarchived_bytes: int  # its next segment: at most about SEGMENT_BYTES, the rest follows segment by segment
    settled: bool  # unchanged for SETTLE_SECONDS, so no writer is about to finish it


@dataclass(frozen=True, slots=True)
class Status:
    checked_at: Millis
    db_path: Path
    db_bytes: int
    archived_bytes: int  # sum of byte_end - byte_start
    stored_bytes: int  # sum of length(zstd)
    sessions: int
    transcripts: int
    gone: int  # main transcripts no longer on disk (still in the archive)
    multi_generation: int  # files that were rewritten at least once
    unparsed_lines: int
    pending: tuple[Pending, ...]  # files with bytes a sweep would archive now
    unclassified: tuple[Path, ...]  # *.jsonl under projects/ in no known layout, so never archived
    last_sweep: SweepReport | None  # the last sweep that waited for the lock and was not refused
    last_refused: Refusal | None  # the last sweep that waited for the lock and was refused


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


class EmptyQuery(ArchiveError):
    """No search term has a letter or a digit."""


class Locked(ArchiveError):
    """Another process held the write lock for LOCK_WAIT_MS."""


class _Busy(Exception):
    """Internal: BEGIN IMMEDIATE failed with SQLITE_BUSY under wait=False."""


@dataclass(frozen=True, slots=True)
class _Delta:
    """What part of a sweep did: one file, or the side files."""

    bytes: int = 0
    rewritten: int = 0  # new generations started
    docs: int = 0  # side-file bodies stored
    errors: tuple[str, ...] = ()
    stop: _Busy | Locked | NewerArchive | None = None  # why the sweep ends here

    def __add__(self, other: _Delta) -> _Delta:
        return _Delta(
            self.bytes + other.bytes,
            self.rewritten + other.rewritten,
            self.docs + other.docs,
            self.errors + other.errors,
            self.stop or other.stop,
        )


def _failed(what: str, e: Exception) -> _Delta:
    """What an exception means for a sweep. The one place that decides it:
    another writer (wait=False), a lock held past LOCK_WAIT_MS, or a newer
    claudinator ends the sweep; a file deleted meanwhile is nothing; any
    other exception is one error, and the sweep goes on."""
    match e:
        case _Busy() | NewerArchive():
            return _Delta(stop=e)
        case Locked():
            return _Delta(errors=(str(e),), stop=e)
        case FileNotFoundError():
            return _Delta()
        case _:
            return _Delta(errors=(f"{what}: {e!r}",))


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
    WAIT = "wait"  # shorter but with the same head: a copy of the transcript still being written


def plan(head: bytes, tail: bytes, cursor: int, size: int, settled: bool, file_head: bytes, file_window: bytes) -> Plan:
    """How to ingest a file whose generation is archived up to cursor.

    file_head is the file's bytes [0, len(head)), fewer if the file is
    shorter. file_window is its bytes [cursor - TAIL_BYTES, cursor), read
    only when size >= cursor. A shorter file with the same head waits until
    it settles, because that is what a copy written in place looks like
    halfway: `claudinator export ID > path`, the restore the README offers,
    or a copy from a backup. Treating it as a rewrite would archive the
    half as a new generation. Any other shorter file, a changed head, or a
    changed window is a rewrite. A rewrite that keeps both the head and the
    window is not detected."""
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


@dataclass(frozen=True, slots=True)
class _Next:
    """The next segment of a file, as a sweep would archive it now."""

    generation: int  # of the source it extends or starts
    cursor: int  # its first byte in the file; 0 starts a new source
    window: bytes  # file bytes [cursor - TAIL_BYTES, cursor), matched against the archive's tail
    chunk: bytes  # its bytes, never empty
    settled: bool  # the file has not changed for SETTLE_SECONDS


def _next(path: Path, a: _Archived | None) -> _Next | None:
    """The next segment to archive of a file whose latest archived
    generation is a, or None when there is none yet: the file is gone,
    unchanged, a copy still being written, empty, or has no complete new
    line and has not settled. A sweep calls it outside the lock to skip
    files with nothing to do, and _ingest_segment calls it again under the
    lock, so the two cannot disagree."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        settled = _settled(st)
        generation, cursor, window = 0, 0, b""
        if a is not None:
            match _look(fd, a, st.st_size, settled):
                case (Plan.UNCHANGED | Plan.WAIT, _):
                    return None
                case (Plan.APPEND, window):
                    generation, cursor = a.generation, a.cursor
                case (Plan.NEW_GENERATION, _):
                    generation = a.generation + 1
        chunk = _next_segment(fd, cursor, st.st_size, settled)
    finally:
        os.close(fd)
    return _Next(generation, cursor, window, chunk, settled) if chunk else None


def _settled(st: os.stat_result) -> bool:
    return now() - st.st_mtime_ns // 1_000_000 >= SETTLE_SECONDS * 1000


def now() -> Millis:
    """The clock, for archive.py and cli.py alike."""
    return Millis(time.time_ns() // 1_000_000)


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
                [("archive_schema", str(ARCHIVE_SCHEMA)), ("projection_version", str(PROJECTION_VERSION))],
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
    once: SQLite's write lock is the only serialization, held per segment,
    and every write transaction re-reads the state it depends on after
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
            db.create_function("casefold", 1, lambda text: text and text.casefold(), deterministic=True)
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
        if not self._db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'meta'").fetchone():
            raise ArchiveError(f"{self.cfg.db} is not a claudinator archive")
        schema, projection = self._meta_int("archive_schema"), self._meta_int("projection_version")
        if schema is None or projection is None:
            raise ArchiveError(f"{self.cfg.db} is not a claudinator archive")
        if schema < ARCHIVE_SCHEMA:
            raise ArchiveError(
                f"{self.cfg.db} has archive schema {schema}, and this claudinator reads schema {ARCHIVE_SCHEMA} only"
            )
        if schema == ARCHIVE_SCHEMA and projection < PROJECTION_VERSION:
            try:
                self.rebuild()
            except NewerArchive:
                pass

    # -- writing ------------------------------------------------------------

    def sweep(
        self,
        *,
        wait: bool = True,
        progress: Callable[[Path], None] | None = None,
    ) -> SweepReport:
        """Archive every byte on disk that is not archived yet. Idempotent:
        a second run with no file changes archives nothing.

        wait=True (launchd, install, `claudinator sweep`): block on the write
        lock up to LOCK_WAIT_MS per file, and record the report as the last
        sweep. wait=False (every read command): give up at the first busy
        lock and report busy=True; the reader proceeds on what is already
        archived.

        A file that fails for any reason becomes one entry in errors and the
        sweep moves on, so one bad file never keeps the rest unarchived. A
        refused sweep is recorded apart from the last sweep, so status can
        say why without hiding the sweep that last wrote."""
        started = now()
        found = discover(self.cfg)
        total, changed = _Delta(errors=found.errors, stop=self._newer()), 0
        if not total.stop:
            known = self._archived()
            for loc in found.transcripts:
                delta = self._ingest(loc, known.get(loc.path), wait)
                total, changed = total + delta, changed + (delta.bytes > 0)
                if total.stop:
                    break
                if progress and delta.bytes:
                    progress(loc.path)
            else:
                total += self._store_docs(found.side_files, wait)
        report = SweepReport(
            started=started,
            finished=now(),
            transcripts_seen=len(found.transcripts),
            transcripts_changed=changed,
            bytes_archived=total.bytes,
            new_generations=total.rewritten,
            docs_changed=total.docs,
            errors=total.errors,
            busy=isinstance(total.stop, _Busy),
            refused=str(total.stop) if isinstance(total.stop, NewerArchive) else None,
        )
        if wait and not isinstance(total.stop, Locked):
            report = self._record(report)
        return report

    def rebuild(self, *, force: bool = False) -> int:
        """Drop and re-derive the projection from the archive in one write
        transaction. open() runs it after an upgrade; `claudinator rebuild`
        runs it with force=True. Readers in other processes keep seeing the
        old projection (WAL snapshot) until commit. Returns how many sources
        it replayed: one per transcript file and generation."""
        with self._write(wait=True):
            if not force and self._projection_current():
                return 0
            self._db.executescript(_PROJECTION_DDL)
            sources = self._db.execute("SELECT id FROM source ORDER BY id").fetchall()
            for (source_id,) in sources:
                self._project(source_id, _records(self._segments(source_id)))
            self._set_meta("projection_version", str(PROJECTION_VERSION))
        return len(sources)

    # -- reading ------------------------------------------------------------

    def search(self, q: Query) -> list[SessionHit]:
        """Sessions in which every term matches some eligible unit, best
        first by the summed bm25 of their matches, each with an excerpt from
        the raw line of its best unit: the one matching the most terms, then
        the best summed bm25. A unit is
        eligible when its kind is in scope and its time in the window. A
        phrase matches inside one unit. Terms without a letter or digit are
        dropped; EmptyQuery if none is left."""
        terms = searchable(q.terms)
        if not terms:
            raise EmptyQuery("nothing to search for: every word lacks a letter or a digit")
        kinds = CONVERSATION | (TOOLS if q.tools else frozenset())
        cursor = self._db.cursor()
        cursor.row_factory = sqlite3.Row
        rows = cursor.execute(
            _SEARCH_SQL,
            {
                "exprs": json.dumps([fts_expr(term) for term in terms]),
                "kinds": json.dumps(sorted(kinds)),
                "since": q.filters.since,
                "until": q.filters.until,
                "project": q.filters.project,
                "limit": q.limit,
            },
        ).fetchall()
        ids = json.dumps([row["session_id"] for row in rows])
        sessions = {row["session_id"]: self._summary(row) for row in self._session_rows(Filters(), None, ids=ids)}
        hits = []
        for row in rows:
            text, marks = excerpt(self._entry_text(row["source_id"], row["line_off"], row["block"]), terms)
            best = Excerpt(Kind(row["kind"]), row["ts"], row["agent_id"], text, marks)
            hits.append(SessionHit(sessions[row["session_id"]], row["hits"], best))
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
        for _, rec, _ in _records(self._segments(source_id)):
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
                n = _next(loc.path, known.get(loc.path))
            except OSError:
                continue  # the sweep that reads it reports why it cannot
            if n is not None:
                pending.append(Pending(loc.path, len(n.chunk), n.settled))
        last, refused = self._meta("last_sweep"), self._meta("last_refused")
        return Status(
            checked_at=now(),
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
            last_refused=_known(Refusal, json.loads(refused)) if refused else None,
        )

    # -- private -----------------------------------------------------------

    @contextmanager
    def _write(self, *, wait: bool, newer_ok: bool = False) -> Iterator[None]:
        """One write transaction. The connection is in autocommit mode, so
        every statement inside, DDL included, joins this transaction.

        Raises NewerArchive when a newer claudinator has stamped the file,
        checked under the lock because one may upgrade it while this
        process has it open. newer_ok skips that check, for the one write
        an older claudinator may make: the record of its refusal."""
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
            if not newer_ok and (newer := self._newer()):
                raise newer
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:  # SQLite rolls back by itself on some errors (disk full, I/O)
                self._db.execute("ROLLBACK")
            raise

    def _ingest(self, f: TranscriptFile, hint: _Archived | None, wait: bool) -> _Delta:
        """Archive and project the new complete lines of one transcript, one
        segment per write transaction, so neither memory nor the time the
        lock is held grows with the file. hint is what the archive held
        before the sweep; a file with nothing to archive against it takes
        no lock.

        Concurrency: each transaction reads the archived state, reads the
        file, and writes, all under BEGIN IMMEDIATE. A second process
        ingesting the same file blocks on the lock, then re-reads a cursor
        that already covers the first one's bytes and archives only what is
        newer, usually nothing. The segment primary key and the contiguity
        trigger turn any bug that would double- or skip-archive into a failed
        transaction.

        Crash safety: a segment, its entry and FTS rows, and the transcript
        facts commit together. A crash leaves the segments committed before
        it, a line-aligned prefix of the file, and the next sweep goes on
        from that cursor."""
        done = _Delta()
        try:
            if _next(f.path, hint) is None:
                return done
            while True:
                with self._write(wait=wait):
                    step = self._ingest_segment(f)
                if step is None:
                    return done
                done += step
        except Exception as e:
            return done + _failed(str(f.path), e)

    def _ingest_segment(self, f: TranscriptFile) -> _Delta | None:
        """Archive and project the next segment of a file inside the caller's
        transaction, or return None when there is nothing to archive yet."""
        a = self._archived(f.path).get(f.path)
        if (n := _next(f.path, a)) is None:
            return None
        archived_at = now()
        if n.cursor == 0:
            (source_id,) = self._db.execute(
                "INSERT INTO source (path, generation, session_id, agent_id, head, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (str(f.path), n.generation, f.session_id, f.agent_id, n.chunk[:1024], archived_at),
            ).fetchone()
        else:
            source_id = a.source_id
        before = self._open_line(source_id) if n.window and not n.window.endswith(b"\n") else ()
        tail = hashlib.sha256((n.window + n.chunk[-TAIL_BYTES:])[-TAIL_BYTES:]).digest()
        self._db.execute(
            "INSERT INTO segment (source_id, byte_start, byte_end, tail, zstd, archived_at) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, n.cursor, n.cursor + len(n.chunk), tail, zstd.compress(n.chunk), archived_at),
        )
        self._project(source_id, _records([(n.cursor, n.chunk)], before))
        return _Delta(len(n.chunk), rewritten=n.generation > 0 and n.cursor == 0)

    def _project(self, source_id: int, records: Iterable[tuple[int, Record, bool]]) -> None:
        """Fold records into the projection. Shared by _ingest and rebuild;
        must run inside a write transaction.

        Total: each record's rows go in under a savepoint, and a record whose
        fold or rows fail for any reason is rolled back to it and counts as
        one unparsed line. The projection is disposable and must never keep
        a segment from committing."""
        row = self._db.execute(
            "SELECT cwd, first_ts, last_ts, ai_title, custom_title, unparsed FROM transcript WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        facts = Facts(*row) if row else Facts()
        for off, rec, mends in records:
            if mends:
                facts = replace(facts, unparsed=facts.unparsed - 1)
            self._db.execute("SAVEPOINT record")
            try:
                folded, drafts = project(facts, rec)
                for d in drafts:
                    (entry_id,) = self._db.execute(
                        "INSERT INTO entry (source_id, line_off, block, kind, ts) VALUES (?, ?, ?, ?, ?) RETURNING id",
                        (source_id, off, d.block, d.kind.value, d.ts),
                    ).fetchone()
                    text = text_of(rec, d.block)
                    self._db.execute("INSERT INTO entry_fts (rowid, text) VALUES (?, ?)", (entry_id, text))
            except Exception:
                if not self._db.in_transaction:  # SQLite rolled the whole transaction back (disk full, I/O)
                    raise
                self._db.execute("ROLLBACK TO record")
                folded, _ = project(facts, Unparsed())
            self._db.execute("RELEASE record")
            facts = folded
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
        """(byte_start, payload) of every segment of a source in file order.

        No statement stays open while the caller works on a segment: the
        starts are fetched whole and each payload by its key. rebuild
        projects as it reads, and once its DDL has run, the ROLLBACK TO
        with which _project drops a refused record aborts every statement
        open on the connection."""
        starts = self._db.execute(
            "SELECT byte_start FROM segment WHERE source_id = ? ORDER BY byte_start", (source_id,)
        ).fetchall()
        for (start,) in starts:
            yield start, self._payload(source_id, start)

    def _payload(self, source_id: int, start: int) -> bytes:
        """The bytes of one segment. A lookup by primary key, so fetching its
        one row finishes the statement."""
        (blob,) = self._db.execute(
            "SELECT zstd FROM segment WHERE source_id = ? AND byte_start = ?", (source_id, start)
        ).fetchone()
        try:
            return zstd.decompress(blob)
        except zstd.ZstdError as e:
            raise ArchiveError(
                f"the archive is damaged: segment {start} of source {source_id} does not decompress ({e})"
            ) from e

    def _session_rows(
        self, filters: Filters, limit: int | None, prefix: str | None = None, ids: str | None = None
    ) -> list[sqlite3.Row]:
        """The rows of _SESSIONS_SQL. ids, a JSON list of session ids, keeps
        only those sessions."""
        cursor = self._db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute(
            _SESSIONS_SQL,
            {
                "prompt": Kind.PROMPT.value,
                "desktop": DocRole.DESKTOP.value,
                "project": filters.project,
                "since": filters.since,
                "until": filters.until,
                "prefix": prefix,
                "ids": ids,
                "limit": -1 if limit is None else limit,
            },
        ).fetchall()

    def _summary(self, row: sqlite3.Row) -> SessionSummary:
        desktop = desktop_session(row["desktop"]) if row["desktop"] else None
        title = row["custom_title"] or (desktop and desktop.title) or row["ai_title"]
        if title is None and row["first_prompt"] is not None:
            entry = self._db.execute("SELECT source_id, line_off, block FROM entry WHERE id = ?", (row["first_prompt"],))
            title = " ".join(self._entry_text(*entry.fetchone()).split())[:200]
        return SessionSummary(
            id=row["session_id"],
            cwd=row["cwd"],
            title=title,
            first_ts=row["first_ts"],
            last_ts=row["last_ts"],
            prompts=row["prompts"],
            subagents=row["subagents"],
            transcript=Path(row["path"]),
            on_disk=os.path.exists(row["path"]),
        )

    def _entry_text(self, source_id: int, line_off: int, block: int) -> str:
        """The text of one entry, from the record _records() makes at
        line_off: the last one there, which replaces a torn tail's Unparsed
        when the line was mended."""
        made = [rec for off, rec, _ in _records(self._line(source_id, line_off)) if off == line_off]
        try:
            return text_of(made[-1], block)
        except LookupError:  # no record starts at line_off, or it has no such block
            raise ArchiveError(
                f"the archive is damaged: entry {block} of the line at byte {line_off} of source {source_id} has no text"
            ) from None

    def _line(self, source_id: int, offset: int) -> list[tuple[int, bytes]]:
        """The bytes from offset through the end of its line, as the
        segments hold them: usually one piece of one segment."""
        pieces = []
        for (start,) in self._db.execute(
            "SELECT byte_start FROM segment WHERE source_id = ?1 AND byte_start >="
            " (SELECT MAX(byte_start) FROM segment WHERE source_id = ?1 AND byte_start <= ?2) ORDER BY byte_start",
            (source_id, offset),
        ).fetchall():
            chunk = self._payload(source_id, start)
            at = max(offset - start, 0)
            end = chunk.find(b"\n", at)
            pieces.append((start + at, chunk[at : end if end >= 0 else len(chunk)]))
            if end >= 0:
                break
        return pieces

    def _open_line(self, source_id: int) -> list[tuple[int, bytes]]:
        """The bytes after the archive's last newline, as the segments hold
        them: the line a settled tail left open."""
        pieces = []
        for (start,) in self._db.execute(
            "SELECT byte_start FROM segment WHERE source_id = ? ORDER BY byte_start DESC", (source_id,)
        ).fetchall():
            chunk = self._payload(source_id, start)
            newline = chunk.rfind(b"\n")
            pieces.append((start + newline + 1, chunk[newline + 1 :]))
            if newline >= 0:
                break
        return pieces[::-1]

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

    def _store_docs(self, docs: Iterable[SideFile], wait: bool) -> _Delta:
        """Store the latest bytes of each side file whose sha256 changed. A
        file whose size and mtime match its row is not read, and the lock is
        taken only when some file's do not. Under the lock each such file is
        stat'ed, read, and compared with its row as it is then, so a sweep
        never overwrites a body another sweep committed with older bytes, or
        stamps that body with the stat of its own read. DESKTOP docs get
        their session id from the body."""
        errors = []
        rows = self._db.execute("SELECT path, size, mtime_ns FROM doc")
        known = {path: (size, mtime_ns) for path, size, mtime_ns in rows}
        changed = []
        for d in docs:
            try:
                st = d.path.stat()
            except FileNotFoundError:
                continue
            except OSError as e:
                errors.append(f"{d.path}: {e!r}")
                continue
            if known.get(str(d.path)) != (st.st_size, st.st_mtime_ns):
                changed.append(d)
        if not changed:
            return _Delta(errors=tuple(errors))
        stored = 0
        try:
            with self._write(wait=wait):
                for d in changed:
                    row = self._db.execute("SELECT size, mtime_ns, sha256 FROM doc WHERE path = ?", (str(d.path),))
                    size, mtime_ns, sha = row.fetchone() or (None, None, None)
                    try:
                        st = d.path.stat()  # before the read, so a write during it shows up as a change next time
                        if (size, mtime_ns) == (st.st_size, st.st_mtime_ns):
                            continue
                        body = d.path.read_bytes()
                    except FileNotFoundError:
                        continue
                    except OSError as e:
                        errors.append(f"{d.path}: {e!r}")
                        continue
                    if (digest := hashlib.sha256(body).digest()) == sha:
                        self._db.execute(
                            "UPDATE doc SET size = ?, mtime_ns = ? WHERE path = ?",
                            (st.st_size, st.st_mtime_ns, str(d.path)),
                        )
                        continue
                    session_id = d.session_id
                    if d.role is DocRole.DESKTOP and (desktop := desktop_session(body)) is not None:
                        session_id = desktop.cli_session_id
                    self._db.execute(
                        "INSERT INTO doc (path, role, session_id, agent_id, sha256, body, size, mtime_ns, archived_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (path) DO UPDATE SET role = excluded.role, session_id = excluded.session_id,"
                        " agent_id = excluded.agent_id, sha256 = excluded.sha256, body = excluded.body,"
                        " size = excluded.size, mtime_ns = excluded.mtime_ns, archived_at = excluded.archived_at",
                        (str(d.path), d.role.value, session_id, d.agent_id, digest, body,
                         st.st_size, st.st_mtime_ns, now()),
                    )
                    stored += 1
        except Exception as e:
            return _Delta(errors=tuple(errors)) + _failed("side files", e)
        return _Delta(docs=stored, errors=tuple(errors))

    def _record(self, report: SweepReport) -> SweepReport:
        """Store report as the last sweep, or a refused one as the last
        refusal, so status can tell a sweeper that cannot write from one
        that stopped running, and a refusal never replaces the report of
        the newer claudinator's own last sweep."""
        try:
            with self._write(wait=True, newer_ok=report.refused is not None):
                if report.refused is None:
                    self._set_meta("last_sweep", json.dumps(asdict(report)))
                else:
                    refusal = Refusal(report.finished, report.refused, ARCHIVE_SCHEMA, PROJECTION_VERSION)
                    self._set_meta("last_refused", json.dumps(asdict(refusal)))
        except Exception as e:
            return replace(report, errors=report.errors + _failed("recording this sweep", e).errors)
        return report

    def _newer(self) -> NewerArchive | None:
        """Whether a newer claudinator has stamped this archive, and so owns
        its writes."""
        if (self._meta_int("archive_schema") or 0) > ARCHIVE_SCHEMA or (
            self._meta_int("projection_version") or 0
        ) > PROJECTION_VERSION:
            return NewerArchive("this archive was written by a newer claudinator")
        return None

    def _projection_current(self) -> bool:
        return self._meta_int("projection_version") == PROJECTION_VERSION

    def _meta(self, key: str) -> str | None:
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
 WHERE (:project IS NULL OR instr(casefold(t.cwd), casefold(:project)) > 0)
   AND (:since IS NULL OR span.last_ts >= :since)
   AND (:until IS NULL OR span.first_ts < :until)
   AND (:prefix IS NULL OR m.session_id GLOB :prefix || '*')
   AND (:ids IS NULL OR m.session_id IN (SELECT value FROM json_each(:ids)))
 ORDER BY span.last_ts DESC, m.session_id
 LIMIT :limit
"""

_SEARCH_SQL = """
WITH term (i, expr) AS (SELECT key, value FROM json_each(:exprs)),
hit AS (
  SELECT t.i AS term, f.rowid AS entry_id, f.rank AS rank, s.session_id
    FROM term t
    JOIN entry_fts f ON f.entry_fts MATCH t.expr
    JOIN entry e ON e.id = f.rowid
    JOIN source s ON s.id = e.source_id
   WHERE e.kind IN (SELECT value FROM json_each(:kinds))
     AND (:since IS NULL OR e.ts >= :since)
     AND (:until IS NULL OR e.ts < :until)
),
matched AS (
  SELECT session_id, SUM(rank) AS score, COUNT(DISTINCT entry_id) AS hits
    FROM hit
   GROUP BY session_id
  HAVING COUNT(DISTINCT term) = (SELECT COUNT(*) FROM term)
),
main AS (
  SELECT MAX(id) AS id, session_id FROM source WHERE agent_id IS NULL GROUP BY session_id
),
kept AS (
  SELECT m.session_id, m.score, m.hits
    FROM matched m
    JOIN main ON main.session_id = m.session_id
    JOIN transcript t ON t.source_id = main.id
   WHERE :project IS NULL OR instr(casefold(t.cwd), casefold(:project)) > 0
   ORDER BY m.score, m.session_id
   LIMIT :limit
),
best AS (
  SELECT session_id, entry_id,
         ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY COUNT(DISTINCT term) DESC, SUM(rank), entry_id) AS place
    FROM hit
   WHERE session_id IN (SELECT session_id FROM kept)
   GROUP BY session_id, entry_id
)
SELECT k.session_id, k.hits, e.source_id, e.line_off, e.block, e.kind, e.ts, s.agent_id
  FROM kept k
  JOIN best b ON b.session_id = k.session_id AND b.place = 1
  JOIN entry e ON e.id = b.entry_id
  JOIN source s ON s.id = e.source_id
 ORDER BY k.score, k.session_id
"""


def _report(obj: dict) -> SweepReport:
    """The stored last-sweep JSON as a SweepReport."""
    return replace(_known(SweepReport, obj), errors=tuple(obj.get("errors", ())))


def _known[T](cls: type[T], obj: dict) -> T:
    """A dataclass from its stored JSON. Keys this version does not know
    come from a newer claudinator and are dropped."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in obj.items() if k in names})
