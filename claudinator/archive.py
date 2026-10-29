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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import BinaryIO

from compression import zstd

from claudinator.record import (
    PROJECTION_VERSION,
    AgentId,
    Facts,
    Millis,
    Record,
    SessionId,
    Unparsed,
    desktop_session,
    parse,
    project,
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

    # -- private: the ingest path -------------------------------------------

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
            chunks = list(chunk_lines(body, cursor))
            self._db.executemany(
                "INSERT INTO segment (source_id, byte_start, byte_end, zstd, archived_at) VALUES (?, ?, ?, ?, ?)",
                [(source_id, start, start + len(chunk), zstd.compress(chunk), now) for start, chunk in chunks],
            )
            self._project(source_id, _records(chunks, after_newline))
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


def _report(obj: dict) -> SweepReport:
    return SweepReport(**{**obj, "errors": tuple(obj.get("errors", ()))})
