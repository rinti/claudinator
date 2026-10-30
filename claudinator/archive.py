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
from claudinator.lines import line_at, next_segment, pread, records, segments_end
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
LOCK_WAIT_MS = 60_000
BUSY_MS = 5000
TAIL_BYTES = 4096
SETTLE_SECONDS = 3600


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
    on_disk: bool


@dataclass(frozen=True, slots=True)
class Subagent:
    id: AgentId
    description: str | None
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
    agent_id: AgentId | None
    text: str  # ~160 chars around the first matched term, single line
    marks: tuple[tuple[int, int], ...]  # [start, end) spans of matched terms in text


@dataclass(frozen=True, slots=True)
class SessionHit:
    session: SessionSummary
    hits: int  # matching units in this session (main + subagents)
    best: Excerpt


@dataclass(frozen=True, slots=True)
class Message:
    ts: Millis | None
    kind: Kind
    text: str
    tool: str | None = None
    spawned: AgentId | None = None


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
    claudinator had stamped the archive."""

    at: Millis
    reason: str
    archive_schema: int  # of the claudinator that was refused
    projection_version: int


@dataclass(frozen=True, slots=True)
class Pending:
    """A file a sweep would archive bytes of now."""

    path: Path
    unarchived_bytes: int
    settled: bool  # unchanged for SETTLE_SECONDS


@dataclass(frozen=True, slots=True)
class Status:
    checked_at: Millis
    db_path: Path
    db_bytes: int
    uncompressed_bytes: int
    compressed_bytes: int
    sessions: int
    transcripts: int
    gone: int  # main transcripts no longer on disk (still in the archive)
    multi_generation: int  # files that were rewritten at least once
    unparsed_lines: int
    pending: tuple[Pending, ...]
    unreadable: tuple[str, ...]  # transcripts left out of pending, as "path: error"
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
    pass


@dataclass(frozen=True, slots=True)
class _Delta:
    bytes: int = 0
    new_generations: int = 0
    docs: int = 0
    errors: tuple[str, ...] = ()
    stop: _Busy | Locked | NewerArchive | None = None

    def __add__(self, other: _Delta) -> _Delta:
        return _Delta(
            self.bytes + other.bytes,
            self.new_generations + other.new_generations,
            self.docs + other.docs,
            self.errors + other.errors,
            self.stop or other.stop,
        )


def _failed(what: str, e: Exception) -> _Delta:
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
    source_id: int
    generation: int
    head: bytes
    cursor: int
    tail_sha256: bytes


class Plan(enum.Enum):
    UNCHANGED = "unchanged"
    APPEND = "append"
    NEW_GENERATION = "new generation"
    COPY_IN_PROGRESS = "copy in progress"


def plan(head: bytes, tail_sha256: bytes, cursor: int, size: int, settled: bool, file_head: bytes, file_window: bytes) -> Plan:
    """How to ingest a file whose generation is archived up to cursor.

    file_head is the file's bytes [0, len(head)), fewer if the file is
    shorter. file_window is its bytes [cursor - TAIL_BYTES, cursor), read
    only when size >= cursor. A shorter file with the same head waits until
    it settles. Any other shorter file, a changed head, or a changed window
    is a rewrite. A rewrite that keeps both the head and the
    window is not detected."""
    head_kept = file_head == head[: len(file_head)]
    if size < cursor:
        return Plan.NEW_GENERATION if settled or not head_kept else Plan.COPY_IN_PROGRESS
    if not head_kept or hashlib.sha256(file_window).digest() != tail_sha256:
        return Plan.NEW_GENERATION
    return Plan.UNCHANGED if size == cursor else Plan.APPEND


def _look(fd: int, archived: _Archived, size: int, settled: bool) -> tuple[Plan, bytes]:
    n = min(TAIL_BYTES, archived.cursor)
    window = pread(fd, n, archived.cursor - n) if size >= archived.cursor else b""
    file_head = pread(fd, len(archived.head), 0)
    return plan(archived.head, archived.tail_sha256, archived.cursor, size, settled, file_head, window), window


@dataclass(frozen=True, slots=True)
class _Unarchived:
    generation: int
    cursor: int
    window: bytes
    size: int
    settled: bool


def _unarchived(fd: int, archived: _Archived | None) -> _Unarchived | None:
    """Where a sweep would archive the file from now, and into which
    generation. None when the plan leaves the file as it is."""
    st = os.fstat(fd)
    settled = _settled(st)
    if archived is None:
        return _Unarchived(0, 0, b"", st.st_size, settled)
    match _look(fd, archived, st.st_size, settled):
        case (Plan.UNCHANGED | Plan.COPY_IN_PROGRESS, _):
            return None
        case (Plan.APPEND, window):
            return _Unarchived(archived.generation, archived.cursor, window, st.st_size, settled)
        case (Plan.NEW_GENERATION, _):
            return _Unarchived(archived.generation + 1, 0, b"", st.st_size, settled)


@dataclass(frozen=True, slots=True)
class _Next:
    generation: int
    cursor: int
    window: bytes
    chunk: bytes
    more: bool


def _next(path: Path, archived: _Archived | None) -> _Next | None:
    with open(path, "rb", buffering=0) as f:
        if (rest := _unarchived(f.fileno(), archived)) is None:
            return None
        chunk, more = next_segment(f.fileno(), rest.cursor, rest.size, rest.settled)
    return _Next(rest.generation, rest.cursor, rest.window, chunk, more) if chunk else None


def _pending(path: Path, archived: _Archived | None) -> Pending | None:
    with open(path, "rb", buffering=0) as f:
        if (rest := _unarchived(f.fileno(), archived)) is None:
            return None
        end = segments_end(f.fileno(), rest.cursor, rest.size, rest.settled)
    return Pending(path, end - rest.cursor, rest.settled) if end > rest.cursor else None


def _changed_stat(path: Path, stored: tuple[int, int] | None) -> os.stat_result | None:
    st = path.stat()
    return st if (st.st_size, st.st_mtime_ns) != stored else None


def _settled(st: os.stat_result) -> bool:
    return now() - st.st_mtime_ns // 1_000_000 >= SETTLE_SECONDS * 1000


def now() -> Millis:
    return Millis(time.time_ns() // 1_000_000)


def _create(path: Path) -> None:
    """The file is made with mode 0600, so its -wal and -shm files are too."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = path.with_name(f".{path.name}.new-{os.getpid()}")
    for leftover in (private, Path(f"{private}-wal"), Path(f"{private}-shm")):
        leftover.unlink(missing_ok=True)
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


@dataclass
class Archive:
    """Every public method is safe to call from many processes at once."""

    cfg: Config
    _db: sqlite3.Connection = field(repr=False)

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

    def sweep(
        self,
        *,
        wait: bool = True,
        progress: Callable[[Path], None] | None = None,
    ) -> SweepReport:
        """Archive every byte on disk that is not archived yet. Idempotent:
        a second run with no file changes archives nothing.

        wait=True: block on the write lock up to LOCK_WAIT_MS per file, and
        record the report as the last sweep, or as the last refusal when a
        newer claudinator's stamp refused it. A sweep that gave up on the
        lock is not recorded. wait=False: give up at the first busy lock and
        report busy=True.

        A file that fails for any reason becomes one entry in errors and the
        sweep moves on."""
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
            new_generations=total.new_generations,
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
        transaction. Readers in other processes keep seeing the old
        projection (WAL snapshot) until commit. Returns how many sources it
        replayed: one per transcript file and generation."""
        with self._write(wait=True):
            if not force and self._projection_current():
                return 0
            self._db.executescript(_PROJECTION_DDL)
            sources = self._db.execute("SELECT id FROM source ORDER BY id").fetchall()
            for (source_id,) in sources:
                self._project(source_id, records(self._segments(source_id)))
            self._set_meta("projection_version", str(PROJECTION_VERSION))
        return len(sources)

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
        hits = []
        with self._read():
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
            for row in rows:
                text, marks = excerpt(self._entry_text(row["source_id"], row["line_off"], row["block"]), terms)
                best = Excerpt(Kind(row["kind"]), row["ts"], row["agent_id"], text, marks)
                hits.append(SessionHit(sessions[row["session_id"]], row["hits"], best))
        return hits

    def sessions(self, filters: Filters = Filters(), limit: int | None = 30) -> list[SessionSummary]:
        """Sessions newest-activity first. since/until select sessions whose
        [first_ts, last_ts] overlaps the window."""
        with self._read():
            return [self._summary(row) for row in self._session_rows(filters, limit)]

    def session(self, ref: str) -> Session:
        """Resolve a unique session-id prefix and return the session with its
        subagents. Raises UnknownSession or AmbiguousSession."""
        with self._read():
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
        file still exists on disk. The transcript, its subagent links, and
        its segments are fixed in one snapshot when this is called, and the
        messages are read as the caller iterates."""
        with self._read():
            source_id = self._source(session_id, agent_id)
            spawned = {}
            for aid, body in self._docs(session_id, DocRole.SUBAGENT_META):
                if tool_use_id := subagent_meta(body).tool_use_id:
                    spawned[tool_use_id] = aid
            segments = self._segments(source_id)
        return (
            Message(rec.ts, b.kind, b.text, b.tool, spawned.get(b.tool_use_id))
            for _, rec, _ in records(segments)
            if isinstance(rec, Turn)
            for _, b in rec.blocks
        )

    def export(
        self,
        session_id: SessionId,
        out: BinaryIO,
        agent_id: AgentId | None = None,
        generation: int | None = None,
    ) -> int:
        """Write the archived bytes of a transcript, byte for byte as Claude
        Code wrote them: the latest generation, or the one named. Returns
        bytes written."""
        with self._read():
            segments = self._segments(self._source(session_id, agent_id, generation))
        written = 0
        for _, chunk in segments:
            out.write(chunk)
            written += len(chunk)
        return written

    def status(self) -> Status:
        with self._read():
            uncompressed, compressed = self._db.execute(
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
            pending, unreadable = [], []
            for loc in found.transcripts:
                try:
                    p = _pending(loc.path, known.get(loc.path))
                except OSError as e:
                    unreadable += _failed(str(loc.path), e).errors
                    continue
                if p is not None:
                    pending.append(p)
            last, refused = self._meta("last_sweep"), self._meta("last_refused")
            return Status(
                checked_at=now(),
                db_path=self.cfg.db,
                db_bytes=sum(p.stat().st_size for p in (self.cfg.db, Path(f"{self.cfg.db}-wal")) if p.exists()),
                uncompressed_bytes=uncompressed,
                compressed_bytes=compressed,
                sessions=sessions,
                transcripts=transcripts,
                gone=sum(not p.exists() for p in mains),
                multi_generation=rewritten,
                unparsed_lines=unparsed,
                pending=tuple(pending),
                unreadable=tuple(unreadable),
                unclassified=found.unclassified,
                last_sweep=_report(json.loads(last)) if last else None,
                last_refused=_known(Refusal, json.loads(refused)) if refused else None,
            )

    @contextmanager
    def _read(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
        finally:
            if self._db.in_transaction:
                self._db.execute("COMMIT")

    @contextmanager
    def _write(self, *, wait: bool, newer_ok: bool = False) -> Iterator[None]:
        """The connection is in autocommit mode, so every statement inside, DDL
        included, joins this transaction."""
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
        done = _Delta()
        try:
            more = _next(f.path, hint) is not None
            while more:
                with self._write(wait=wait):
                    step, more = self._ingest_segment(f)
                done += step
        except Exception as e:
            return done + _failed(str(f.path), e)
        return done

    def _ingest_segment(self, f: TranscriptFile) -> tuple[_Delta, bool]:
        archived = self._archived(f.path).get(f.path)
        if (n := _next(f.path, archived)) is None:
            return _Delta(), False
        archived_at = now()
        if n.cursor == 0:
            (source_id,) = self._db.execute(
                "INSERT INTO source (path, generation, session_id, agent_id, head, first_seen)"
                " VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (str(f.path), n.generation, f.session_id, f.agent_id, n.chunk[:1024], archived_at),
            ).fetchone()
        else:
            source_id = archived.source_id
        before = self._line(source_id, n.cursor - 1) if n.window and not n.window.endswith(b"\n") else ()
        tail_sha256 = hashlib.sha256((n.window + n.chunk[-TAIL_BYTES:])[-TAIL_BYTES:]).digest()
        self._db.execute(
            "INSERT INTO segment (source_id, byte_start, byte_end, tail, zstd, archived_at) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, n.cursor, n.cursor + len(n.chunk), tail_sha256, zstd.compress(n.chunk), archived_at),
        )
        self._project(source_id, records([(n.cursor, n.chunk)], before))
        return _Delta(len(n.chunk), new_generations=n.generation > 0 and n.cursor == 0), n.more

    def _project(self, source_id: int, parsed: Iterable[tuple[int, Record, bool]]) -> None:
        row = self._db.execute(
            "SELECT cwd, first_ts, last_ts, ai_title, custom_title, unparsed FROM transcript WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        facts = Facts(*row) if row else Facts()
        for off, rec, mends in parsed:
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
        """rebuild projects as it reads, and once its DDL has run, the ROLLBACK TO
        with which _project drops a refused record aborts every statement
        open on the connection."""
        starts = self._starts(source_id)
        return ((start, self._payload(source_id, start)) for start in starts)

    def _starts(self, source_id: int) -> list[int]:
        rows = self._db.execute("SELECT byte_start FROM segment WHERE source_id = ? ORDER BY byte_start", (source_id,))
        return [start for (start,) in rows.fetchall()]

    def _payload(self, source_id: int, start: int) -> bytes:
        """A lookup by primary key, so fetching its one row finishes the
        statement."""
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
        if title is None and row["prompt_source"] is not None:
            text = self._entry_text(row["prompt_source"], row["prompt_off"], row["prompt_block"])
            title = " ".join(text.split())[:200]
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
        made = [rec for off, rec, _ in records(self._line(source_id, line_off)) if off == line_off]
        try:
            return text_of(made[-1], block)
        except LookupError:
            raise ArchiveError(
                f"the archive is damaged: entry {block} of the line at byte {line_off} of source {source_id} has no text"
            ) from None

    def _line(self, source_id: int, offset: int) -> list[tuple[int, bytes]]:
        starts = self._starts(source_id)
        return line_at(starts, lambda k: self._payload(source_id, starts[k]), offset)

    def _docs(self, session_id: SessionId, role: DocRole) -> list[tuple[AgentId | None, bytes]]:
        return self._db.execute(
            "SELECT agent_id, body FROM doc WHERE session_id = ? AND role = ? ORDER BY path", (session_id, role.value)
        ).fetchall()

    def _source(self, session_id: SessionId, agent_id: AgentId | None, generation: int | None = None) -> int:
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
        rows = self._db.execute("SELECT path, size, mtime_ns FROM doc").fetchall()
        stored = {path: (size, mtime_ns) for path, size, mtime_ns in rows}
        seen, changed = _Delta(), []
        for d in docs:
            try:
                if _changed_stat(d.path, stored.get(str(d.path))):
                    changed.append(d)
            except OSError as e:
                seen += _failed(str(d.path), e)
        if not changed:
            return seen
        written = _Delta()
        try:
            with self._write(wait=wait):
                for d in changed:
                    try:
                        written += self._store_doc(d)
                    except OSError as e:
                        written += _failed(str(d.path), e)
        except Exception as e:
            return seen + _Delta(errors=written.errors) + _failed("side files", e)
        return seen + written

    def _store_doc(self, d: SideFile) -> _Delta:
        row = self._db.execute("SELECT size, mtime_ns, sha256 FROM doc WHERE path = ?", (str(d.path),)).fetchone()
        if (st := _changed_stat(d.path, row[:2] if row else None)) is None:
            return _Delta()
        body = d.path.read_bytes()
        digest = hashlib.sha256(body).digest()
        if row and digest == row[2]:
            self._db.execute(
                "UPDATE doc SET size = ?, mtime_ns = ? WHERE path = ?", (st.st_size, st.st_mtime_ns, str(d.path))
            )
            return _Delta()
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
        return _Delta(docs=1)

    def _record(self, report: SweepReport) -> SweepReport:
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
       p.source_id AS prompt_source, p.line_off AS prompt_off, p.block AS prompt_block,
       span.first_ts, span.last_ts,
       (SELECT COUNT(*) FROM entry e WHERE e.source_id = m.id AND e.kind = :prompt) AS prompts,
       span.subagents, s.path
  FROM main m
  JOIN source s ON s.id = m.id
  JOIN transcript t ON t.source_id = m.id
  JOIN span ON span.session_id = m.session_id
  LEFT JOIN entry p ON p.id = (SELECT e.id FROM entry e
                                WHERE e.source_id = m.id AND e.kind = :prompt
                                ORDER BY e.line_off, e.block LIMIT 1)
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
    return replace(_known(SweepReport, obj), errors=tuple(obj.get("errors", ())))


def _known[T](cls: type[T], obj: dict) -> T:
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in obj.items() if k in names})
