"""claudinator design sketch (candidate: opus).

Module map. Each `# ===` banner below is one file in the package. The call
chain is cli.py -> archive.py -> record.py; nothing reaches past archive.py
into the database.

    claudinator/
      __main__.py   `python -m claudinator` -> cli.main()
      cli.py        Boundary. argv/env -> Config, Query, refs, dates. Formats
                    output. Runs `claude --resume`. Composes status with
                    launchd and settings.json facts.
      archive.py    Archive: the only owner of the SQLite file. Discovers
                    files, ingests byte ranges, folds the projection, answers
                    queries, exports bytes, rebuilds the projection.
      record.py     Pure. One JSONL line -> Record; (Facts, Record) ->
                    (Facts, entries). Owns PROJECTION_VERSION. Every Claude
                    Code schema assumption lives here and nowhere else.
      launchd.py    The hourly sweeper: plist rendering, bootstrap, bootout.
      schema.sql    DDL. Archive layer above `-- @projection`, projection below.

Bodies raise NotImplementedError. `TODO` comments carry the pseudocode for the
logic that must be right the first time: the ingest cursor, the partial last
line, concurrency, rewrite detection, and rebuild.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, NewType

# ============================================================================
# claudinator/record.py  (pure: no I/O, no sqlite, no clock)
# ============================================================================

PROJECTION_VERSION = 1
"""Bump when parse() or project() would produce different rows for the same
bytes, or when the projection section of schema.sql changes.

Backstop so a forgotten bump fails CI instead of silently mixing projections:
tests/test_projection_golden.py projects a fixture corpus of real (redacted)
lines and compares a digest of the rows with GOLDEN[PROJECTION_VERSION]. Its
failure message reads "projection output changed: bump PROJECTION_VERSION and
record the new digest"."""

SessionId = NewType("SessionId", str)  # transcript file stem, a UUID
AgentId = NewType("AgentId", str)  # subagent file stem without "agent-"
Millis = NewType("Millis", int)  # epoch milliseconds, UTC


class Kind(enum.StrEnum):
    """What a searchable unit is. The single list; schema.sql stores .value
    and has no CHECK duplicating it."""

    PROMPT = "prompt"  # human-typed (main) or parent-written (subagent) user text
    REPLY = "reply"  # assistant text block
    TOOL_CALL = "tool_call"  # assistant tool_use: name + compact JSON input
    TOOL_OUTPUT = "tool_output"  # user tool_result content text
    TITLE = "title"  # ai-title or custom-title, one entry per change


CONVERSATION: frozenset[Kind] = frozenset({Kind.PROMPT, Kind.REPLY, Kind.TITLE})
TOOLS: frozenset[Kind] = frozenset({Kind.TOOL_CALL, Kind.TOOL_OUTPUT})


@dataclass(frozen=True, slots=True)
class Block:
    """One displayable, indexable content block of a user or assistant line."""

    kind: Kind  # never TITLE
    text: str
    tool: str | None = None  # TOOL_CALL: tool name
    tool_use_id: str | None = None  # TOOL_CALL: links an Agent call to its subagent meta doc


@dataclass(frozen=True, slots=True)
class Turn:
    """A `user` or `assistant` line that carries content worth showing.

    blocks pairs each kept block with its index in message.content (0 when
    content is a string), so entry.block can find it again in the raw line.
    Thinking, redacted thinking, images, and unknown block types are dropped
    here; the raw line still has them."""

    ts: Millis | None
    cwd: str | None
    blocks: tuple[tuple[int, Block], ...]


@dataclass(frozen=True, slots=True)
class Title:
    text: str
    custom: bool  # custom-title (/rename) outranks ai-title


@dataclass(frozen=True, slots=True)
class Other:
    """Any other JSON object: system, attachment, api-request*, mode,
    file-history-*, isMeta user lines, non-human user origins, and every type
    Claude Code adds later. Archived, never indexed. Contributes ts/cwd to
    Facts so first/last activity stays right."""

    ts: Millis | None
    cwd: str | None


@dataclass(frozen=True, slots=True)
class Unparsed:
    """Not a JSON object: a torn line from a crash, or a settled partial tail."""


Record = Turn | Title | Other | Unparsed


def parse(line: bytes) -> Record:
    """Total function from one JSONL line (without its newline) to a Record.

    This is the drift boundary: the only code that reads Claude Code field
    names. It never raises. Anything it does not recognise becomes Other or
    Unparsed, and the raw bytes stay in the archive for a later
    PROJECTION_VERSION to reinterpret.

    TODO:
      try: obj = json.loads(line) except ValueError: return Unparsed()
      if not isinstance(obj, dict): return Unparsed()
      ts = _millis(obj.get("timestamp")); cwd = str-or-None(obj.get("cwd"))
      match obj.get("type"):
        "ai-title"     -> Title(obj["aiTitle"], custom=False) if it is a non-empty str
        "custom-title" -> Title(obj["customTitle"], custom=True) if it is a non-empty str
        "assistant"    -> Turn over message.content list:
                            text     -> Block(REPLY, text)
                            tool_use -> Block(TOOL_CALL, f"{name} {json.dumps(input, ensure_ascii=False, separators=(',', ':'))}",
                                              tool=name, tool_use_id=id)
        "user"         -> Other(ts, cwd) if obj.get("isMeta")
                          Other(ts, cwd) if origin.kind exists and != "human"   (task-notification, peer, ...)
                          content str  -> ((0, Block(PROMPT, content)),)
                          content list -> text -> PROMPT; tool_result -> TOOL_OUTPUT(_result_text(block["content"]))
        _              -> Other(ts, cwd)
      A Turn with no kept blocks degrades to Other(ts, cwd).
    """
    raise NotImplementedError


def project(facts: Facts, record: Record) -> tuple[Facts, list[EntryDraft]]:
    """Fold one record into its transcript's facts and emit searchable units.

    Pure and deterministic. Incremental ingest and rebuild both call it on the
    same lines in the same order (file order, split per segment), so the two
    paths produce identical rows. That equality is a test.

    TODO:
      match record:
        Unparsed()      -> (facts + unparsed 1, [])
        Other(ts, cwd)  -> (_observe(facts, ts, cwd), [])
        Turn(ts, cwd, blocks)
                        -> (_observe(facts, ts, cwd),
                            [EntryDraft(i, b.kind, ts) | (i, b) in blocks if b.text.strip()])
        Title(text, custom)
                        -> current = facts.custom_title if custom else facts.ai_title
                           if text == current: (facts, [])      # titles repeat thousands of times
                           else: (facts with that title = text, [EntryDraft(0, TITLE, facts.last_ts)])
      _observe: cwd = facts.cwd or cwd (first wins); first_ts = min; last_ts = max.
    The text for EntryDraft is re-derived by the caller from the same Record
    via text_of(), so project() never copies strings it does not need.
    """
    raise NotImplementedError


def text_of(record: Record, block: int) -> str:
    """The indexed/displayed text of entry `block` of this record.

    Turn -> the Block whose content index is `block`. Title -> its text.
    Used for FTS insert and for re-rendering a search hit from raw bytes, so
    the indexed text and the shown text cannot drift apart."""
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Facts:
    """Folded per-transcript facts; one transcript row. Owner: project()."""

    cwd: str | None = None
    first_ts: Millis | None = None
    last_ts: Millis | None = None
    ai_title: str | None = None
    custom_title: str | None = None
    unparsed: int = 0


@dataclass(frozen=True, slots=True)
class EntryDraft:
    block: int
    kind: Kind
    ts: Millis | None


def _millis(value: object) -> Millis | None:
    """ISO 8601 ('2026-10-13T14:48:22.241Z') -> epoch ms. None on anything else."""
    raise NotImplementedError


def _result_text(content: object) -> str:
    """tool_result content (str, or list of text/image parts) -> text.
    Images become "[image]". toolUseResult duplicates this and is ignored."""
    raise NotImplementedError


# ============================================================================
# claudinator/archive.py
# ============================================================================

ARCHIVE_SCHEMA = 1
"""Archive-layer schema version. Migrations are an ordered list of SQL
scripts applied in place inside one transaction; the archive layer is never
dropped. Version 1 has none."""

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
    SESSION_FILE = "session-file"  # any other non-.jsonl file under <slug>/<sid>/
    DESKTOP = "desktop"  # ~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json


@dataclass(frozen=True, slots=True)
class Config:
    db: Path  # $CLAUDINATOR_DB or ~/.claudinator/claudinator.db
    claude_dir: Path  # $CLAUDE_CONFIG_DIR or ~/.claude
    desktop_dir: Path  # ~/Library/Application Support/Claude

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        raise NotImplementedError


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


def classify(path: Path, cfg: Config) -> Location | None:
    """The only code that knows Claude Code's on-disk layout.

        <claude>/projects/<slug>/<sid>.jsonl                        -> TranscriptFile(sid, None)
        <claude>/projects/<slug>/<sid>/subagents/agent-<aid>.jsonl  -> TranscriptFile(sid, aid)
        <claude>/projects/<slug>/<sid>/subagents/agent-<aid>.meta.json -> SideFile(SUBAGENT_META)
        <claude>/projects/<slug>/<sid>/**/<other non-.jsonl file>   -> SideFile(SESSION_FILE)
        <desktop>/claude-code-sessions/<a>/<b>/local_*.json         -> SideFile(DESKTOP)
        anything else (memory/, sessions-index.json, ...)           -> None

    session_id comes from the path, never from record content: forked copies
    carry rewritten sessionIds, and the file is the unit Claude resumes."""
    raise NotImplementedError


def discover(cfg: Config) -> Iterator[Location]:
    """Walk projects/ and the desktop sessions dir; yield classify() hits.
    A missing desktop dir yields nothing."""
    raise NotImplementedError


def chunk_lines(body: bytes, base: int, limit: int = SEGMENT_BYTES) -> Iterator[tuple[int, bytes]]:
    """Split body (file bytes starting at offset `base`) into segment payloads.

    Each chunk ends just after the first newline at or past `limit` bytes, or
    at the end of body. So every segment ends with a newline except a settled
    tail, and no line is split across segments except a settled tail and
    its later continuation (both read back as Unparsed, identically in ingest
    and rebuild)."""
    raise NotImplementedError


def lines(chunk: bytes, base: int) -> Iterator[tuple[int, bytes]]:
    """(file offset, line without newline) for each line of one segment
    payload. An unterminated tail is yielded as a line. Empty lines skipped."""
    raise NotImplementedError


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
    last_sweep: SweepReport | None


class ArchiveError(Exception):
    """Base for errors the CLI turns into a message and exit code 1."""


class UnknownSession(ArchiveError):
    pass


class AmbiguousSession(ArchiveError):
    def __init__(self, ref: str, candidates: list[SessionSummary]) -> None:
        super().__init__(ref)
        self.candidates = candidates


class NewerArchive(ArchiveError):
    """The file was written by a newer claudinator (higher archive_schema or
    projection_version). Reads proceed; writes are refused."""


class _Busy(Exception):
    """Internal: BEGIN IMMEDIATE failed with SQLITE_BUSY under wait=False."""


@dataclass
class Archive:
    """The archive. Every public method is safe to call from many processes at
    once: SQLite's write lock is the only serialization, held per transcript
    delta, and every write transaction re-reads the state it depends on after
    taking the lock.

    Hides: segments, zstd, derived cursors, generations, the projection and
    its version, FTS syntax, path layouts, desktop/meta side files."""

    cfg: Config
    _db: object = field(repr=False)  # sqlite3.Connection(autocommit=True); DDL must join explicit transactions
    _writable: bool = True

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(cls, cfg: Config) -> Archive:
        """Open or create the database, converge its schema, and make the
        projection current.

        TODO:
          mkdir -p cfg.db.parent (0700); connect(autocommit=True)
          PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON
          executescript(archive section of schema.sql)        # IF NOT EXISTS: idempotent
          archive_schema: absent -> write ARCHIVE_SCHEMA; lower -> migrate; higher -> _writable=False
          projection_version: absent or lower -> rebuild(); higher -> _writable=False
        """
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing ------------------------------------------------------------

    def sweep(self, *, wait: bool = True, progress: Callable[[Path, int], None] | None = None) -> SweepReport:
        """Archive every byte on disk that is not archived yet. Idempotent:
        a second run with no file changes archives nothing.

        wait=True (launchd, install, `claudinator sweep`): block on the write
        lock up to 60 s per file. wait=False (every read command): give up at
        the first busy lock and report busy=True; the reader proceeds on what
        is already archived.

        TODO:
          if not self._writable: return report(refused=...)
          known = self._cursors()                      # {path: cursor} of latest generations, no lock
          docs = []
          for loc in discover(cfg):
            TranscriptFile -> try st = stat(loc.path) except FileNotFoundError: continue
                              if known.get(loc.path) == st.st_size: continue      # fast path, no lock
                              try: delta = self._ingest(loc, wait)
                              except _Busy: busy = True; break
                              except OSError as e: errors.append(f"{loc.path}: {e}"); continue
                              tally delta; progress(loc.path, delta.bytes) if progress
            SideFile       -> docs.append(loc)
          if not busy: self._store_docs(docs, wait)            # _Busy here -> busy = True
          write meta.last_sweep (own small transaction; skip if busy)
        """
        raise NotImplementedError

    def rebuild(self, *, force: bool = False) -> None:
        """Drop and re-derive the projection from the archive in one write
        transaction. open() runs it after an upgrade; `claudinator rebuild`
        runs it with force=True. Readers in other processes keep seeing the
        old projection (WAL snapshot) until commit.

        TODO:
          with self._write(wait=True):
            if not force and stored projection_version == PROJECTION_VERSION:
              return                                                      # another process just did it
            executescript(projection section of schema.sql)               # drops + creates
            for source_id in SELECT id FROM source ORDER BY id:
              self._project(source_id, self._lines(source_id))
            upsert meta.projection_version = PROJECTION_VERSION
        """
        raise NotImplementedError

    # -- reading ------------------------------------------------------------

    def search(self, q: Query) -> list[SessionHit]:
        """Sessions containing every term, best first.

        TODO:
          kinds = CONVERSATION | (TOOLS if q.tools else {})
          rows = SELECT e.id, e.source_id, e.line_off, e.block, e.kind, e.ts, s.session_id, s.agent_id
                   FROM entry_fts f JOIN entry e ON e.id = f.rowid JOIN source s ON s.id = e.source_id
                  WHERE entry_fts MATCH :expr AND e.kind IN (SELECT value FROM json_each(:kinds))
                    AND (:since IS NULL OR e.ts >= :since) AND (:until IS NULL OR e.ts < :until)
                  ORDER BY f.rank
                 with expr = _fts_expr(q.terms)
          by_id = {s.id: s for s in self.sessions(Filters(project=q.filters.project), limit=None)}
          group rows by session_id in rank order (first row = best, count = hits), keep ids in by_id
          for the first q.limit groups:
            rec = parse(self._line_at(best.source_id, best.line_off))
            text = text_of(rec, best.block)
            hit.best = _excerpt(text, q.terms, kind, ts, agent_id)
        """
        raise NotImplementedError

    def sessions(self, filters: Filters = Filters(), limit: int | None = 30) -> list[SessionSummary]:
        """Sessions newest-activity first. since/until select sessions whose
        [first_ts, last_ts] overlaps the window. SQL is _SESSIONS_SQL with
        :prompt and :desktop bound from Kind.PROMPT and DocRole.DESKTOP, so
        no enum literal is repeated in SQL."""
        raise NotImplementedError

    def session(self, ref: str) -> Session:
        """Resolve a unique session-id prefix (4+ hex chars) and return the
        session with its subagents. Raises UnknownSession or AmbiguousSession."""
        raise NotImplementedError

    def conversation(self, session_id: SessionId, agent_id: AgentId | None = None) -> Iterator[Message]:
        """The transcript in file order, from the archive, whether or not the
        file still exists on disk.

        TODO:
          src = latest generation of (session_id, agent_id); UnknownSession if none
          spawned = {meta["toolUseId"]: agent_id for SUBAGENT_META docs of this session}
          for off, line in self._lines(src.id):
            match parse(line):
              Turn(ts, _, blocks) -> for _, b in blocks:
                                       yield Message(ts, b.kind, b.text, b.tool, spawned.get(b.tool_use_id))
              _ -> skip
        """
        raise NotImplementedError

    def export(self, session_id: SessionId, out: BinaryIO, agent_id: AgentId | None = None) -> int:
        """Write the archived bytes of the latest generation of a transcript,
        byte-for-byte as Claude Code wrote them. Returns bytes written. This is
        what makes `claude --resume` possible after Claude Code's cleanup
        deleted the file."""
        raise NotImplementedError

    def status(self) -> Status:
        raise NotImplementedError

    # -- private: the ingest path -------------------------------------------

    @contextmanager
    def _write(self, *, wait: bool) -> Iterator[None]:
        """One write transaction.

        TODO:
          PRAGMA busy_timeout = 60000 if wait else 0
          try: BEGIN IMMEDIATE
          except sqlite3.OperationalError as e if SQLITE_BUSY and not wait: raise _Busy
          try: yield; COMMIT
          except BaseException: ROLLBACK; raise
        """
        raise NotImplementedError

    def _ingest(self, f: TranscriptFile, wait: bool) -> _Delta:
        """Archive and project the new complete lines of one transcript.

        Concurrency: the read of the cursor, the read of the file, and the
        writes all happen inside one BEGIN IMMEDIATE transaction. A second
        process ingesting the same file blocks on the lock, then re-reads a
        cursor that already covers the first one's bytes and archives only
        what is newer, usually nothing. No lockfile, no claim table. The
        segment PK and the contiguity trigger turn any bug that would double-
        or skip-archive into a failed transaction.

        Crash safety: segment rows, entry/FTS rows and transcript facts commit
        together. A crash before COMMIT leaves no trace; the next sweep redoes
        the same delta from the same cursor.

        TODO:
          with self._write(wait=wait):
            src = SELECT * FROM source WHERE path = :p ORDER BY generation DESC LIMIT 1
            with open(f.path, "rb") as fh:                       # FileNotFoundError -> rollback, _Delta(0)
              size, mtime = fstat(fh)
              settled = now - mtime > SETTLE_SECONDS
              cursor = self._cursor(src.id) if src else 0        # MAX(byte_end), derived
              n = min(len(src.head), size) if src else 0
              head_changed = src is not None and os.pread(fd, n, 0) != src.head[:n]
                                                                 # prefix compare: a file mid-restore is shorter than head
              shrank = src is not None and size < cursor
              if shrank and not head_changed and not settled: return _Delta(0)
                                                                 # same head, shorter: a restore via export may be
                                                                 # mid-write; decide once the file settles
              rewritten = head_changed or shrank
              if rewritten: generation = src.generation + 1; src = None; cursor = 0
              buf = os.pread(fd, size - cursor, cursor)          # exactly the bytes stat promised
            take = len(buf) if settled else buf.rfind(b"\n") + 1   # partial last line waits for the next sweep
            if take == 0: return _Delta(0)                         # empty transaction; nothing written
            body = buf[:take]
            if src is None:                                        # first bytes of this generation
              src = INSERT source(path, generation, f.session_id, f.agent_id, head=body[:1024], now)
            chunks = list(chunk_lines(body, cursor))
            for start, chunk in chunks:
              INSERT segment(src.id, start, start + len(chunk), zstd.compress(chunk), now)
            self._project(src.id, chain.from_iterable(lines(c, s) for s, c in chunks))
            return _Delta(take, n_lines, rewritten)
        """
        raise NotImplementedError

    def _project(self, source_id: int, lines_: Iterable[tuple[int, bytes]]) -> None:
        """Fold lines into the projection. Shared by _ingest and rebuild; must
        run inside a write transaction.

        TODO:
          facts = row of transcript(source_id) as Facts, or Facts()
          for off, line in lines_:
            rec = parse(line)
            facts, drafts = project(facts, rec)
            for d in drafts:
              id = INSERT entry(source_id, off, d.block, d.kind, d.ts) RETURNING id
              INSERT entry_fts(rowid, text) VALUES (id, text_of(rec, d.block))
          UPSERT transcript(source_id, *facts)
        """
        raise NotImplementedError

    def _cursor(self, source_id: int) -> int:
        """SELECT byte_end FROM segment WHERE source_id = ? ORDER BY byte_start DESC LIMIT 1.
        Served by the segment primary key; never stored separately."""
        raise NotImplementedError

    def _cursors(self) -> dict[Path, int]:
        """{path: cursor} for the latest generation of every source. One query.
        Used outside any lock as a fast-path filter only; _ingest re-reads."""
        raise NotImplementedError

    def _lines(self, source_id: int) -> Iterator[tuple[int, bytes]]:
        """Every line of a source in file order: decompress segments in
        byte_start order and split each with lines(). Lines never join across
        segments, which keeps rebuild identical to incremental ingest."""
        raise NotImplementedError

    def _line_at(self, source_id: int, offset: int) -> bytes:
        """One line by file offset: the segment with the greatest byte_start
        <= offset, decompressed (<= ~1 MiB), sliced to the next newline."""
        raise NotImplementedError

    def _store_docs(self, docs: list[SideFile], wait: bool) -> int:
        """Upsert the latest body of each side file whose sha256 changed.
        DESKTOP bodies are parsed here for cliSessionId (the boundary for that
        format); unparseable desktop JSON is skipped and reported. One
        transaction for all docs. Returns how many changed."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _Delta:
    bytes: int
    lines: int = 0
    rewritten: bool = False


_SESSIONS_SQL = """
WITH main AS (
  SELECT s.id, s.session_id, s.path
    FROM source s
   WHERE s.agent_id IS NULL
     AND s.generation = (SELECT MAX(generation) FROM source g WHERE g.path = s.path)
),
span AS (
  SELECT s.session_id,
         MIN(t.first_ts) AS first_ts,
         MAX(t.last_ts)  AS last_ts,
         COUNT(DISTINCT s.agent_id) AS subagents
    FROM source s JOIN transcript t ON t.source_id = s.id
   GROUP BY s.session_id
)
SELECT m.session_id, t.cwd,
       COALESCE(t.custom_title,
                (SELECT json_extract(d.body, '$.title') FROM doc d
                  WHERE d.session_id = m.session_id AND d.role = :desktop
                  ORDER BY d.mtime DESC LIMIT 1),
                t.ai_title) AS title,
       span.first_ts, span.last_ts,
       (SELECT COUNT(*) FROM entry e WHERE e.source_id = m.id AND e.kind = :prompt) AS prompts,
       span.subagents, m.path
  FROM main m
  JOIN transcript t ON t.source_id = m.id
  JOIN span ON span.session_id = m.session_id
 WHERE (:project IS NULL OR t.cwd LIKE '%' || :project || '%')
   AND (:since IS NULL OR span.last_ts >= :since)
   AND (:until IS NULL OR span.first_ts < :until)
 ORDER BY span.last_ts DESC
 LIMIT :limit
"""


def _fts_expr(terms: tuple[str, ...]) -> str:
    """Terms -> FTS5 MATCH expression that cannot be a syntax error.

    Each term becomes a quoted string ("" escapes "), a trailing * stays
    outside the quotes as a prefix query, terms are joined with AND. So
    `claudinator search "launchd agent" sync*` -> '"launchd agent" AND "sync"*'.
    FTS5 operators are not exposed; nobody has to learn them."""
    raise NotImplementedError


def _excerpt(text: str, terms: tuple[str, ...], width: int = 160) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Window of `width` chars around the first match, newlines collapsed,
    with match spans. Matching folds case and diacritics per character
    (NFKD, keep the base char) so offsets survive folding and the marks agree
    with FTS remove_diacritics=2."""
    raise NotImplementedError


# ============================================================================
# claudinator/launchd.py
# ============================================================================

LABEL = "com.claudinator.sweep"
SWEEP_INTERVAL_SECONDS = 3600


def plist(executable: Path, log: Path) -> bytes:
    """plistlib.dumps of: Label, ProgramArguments [executable, "sweep"],
    StartInterval 3600, RunAtLoad true, ProcessType Background,
    LowPriorityIO true, Nice 10, StandardOutPath/StandardErrorPath = log.
    Pure."""
    raise NotImplementedError


def install(executable: Path, log: Path) -> bool:
    """Write ~/Library/LaunchAgents/com.claudinator.sweep.plist if its bytes
    differ, then `launchctl bootout gui/<uid>/<label>` (ignore not-loaded) and
    `launchctl bootstrap gui/<uid> <plist>`. Idempotent. Returns True if the
    plist changed."""
    raise NotImplementedError


def uninstall() -> None:
    """bootout (ignore not-loaded), remove the plist. Keeps the database."""
    raise NotImplementedError


def loaded() -> bool:
    """`launchctl print gui/<uid>/com.claudinator.sweep` exits 0."""
    raise NotImplementedError


# ============================================================================
# claudinator/cli.py  (boundary: every string from argv/env is parsed here)
# ============================================================================

READ_COMMANDS = frozenset({"search", "sessions", "show", "resume", "export", "status"})
"""Commands that sweep with wait=False before answering, so results include
the turn that just finished in another terminal without any hook."""


def main(argv: list[str] | None = None) -> int:
    """claudinator <command> [...]. Exit 0 ok, 1 domain error (unknown or
    ambiguous session, transcript gone on resume), 2 usage error.

    TODO:
      args = _parser().parse_args(argv)
      cfg = Config.from_env()
      with Archive.open(cfg) as archive:
        if args.command in READ_COMMANDS:
          report = archive.sweep(wait=False)
          if report.bytes_archived > 4 MiB: note on stderr "archived N new MB"
        return HANDLERS[args.command](archive, args)       # one dict, one place
    """
    raise NotImplementedError


def parse_when(text: str, now: Millis) -> Millis:
    """'2026-10-01' (local midnight), '2026-10-01T14:00' (local), '7d', '12h'
    (ago). Anything else -> argparse error (exit 2)."""
    raise NotImplementedError


def parse_ref(text: str) -> str:
    """Session or agent id prefix: [0-9a-f-]{4,36}, lowercased. Otherwise exit 2."""
    raise NotImplementedError


def cmd_search(archive: Archive, args: object) -> int:
    """Print one block per SessionHit:
    `<id8>  <last active>  <~cwd>  <title>  (<n> hits)` then the excerpt,
    indented, marks bold on a tty and [bracketed] otherwise."""
    raise NotImplementedError


def cmd_sessions(archive: Archive, args: object) -> int:
    """Table: SESSION LAST-ACTIVE PROMPTS AGENTS PROJECT TITLE, `[gone]` when
    the transcript is no longer on disk."""
    raise NotImplementedError


def cmd_show(archive: Archive, args: object) -> int:
    """Header (id, cwd, title, span, subagents with descriptions, resume
    hint), then Messages. Default: prompts and replies in full, tool calls
    as one line, tool output as its first line plus a line count, a spawned
    subagent as a `show <id8> --agent <aid8>` pointer. --full prints tool
    output in full. --agent shows a subagent transcript instead."""
    raise NotImplementedError


def cmd_resume(archive: Archive, args: object) -> int:
    """If the main transcript path does not exist, write the archived bytes
    there (create-only: open with "xb", never overwrite; parent dirs created)
    and print what was restored. Then chdir(cwd) and
    os.execvp("claude", ["claude", "--resume", id]). If cwd no longer exists,
    print the resume command and exit 1. This is the only writer under
    ~/.claude."""
    raise NotImplementedError


def cmd_export(archive: Archive, args: object) -> int:
    """archive.export(id, sys.stdout.buffer, agent). Refuses a tty stdout."""
    raise NotImplementedError


def cmd_status(archive: Archive, args: object) -> int:
    """archive.status() plus launchd.loaded() plus cleanupPeriodDays read from
    <claude>/settings.json (read-only). Warns when the sweeper is not loaded,
    when a pending file is older than SETTLE_SECONDS, or when Claude Code's
    30-day cleanup is in effect (informational: the archive keeps everything;
    only resume then needs a restore)."""
    raise NotImplementedError


def cmd_install(archive: Archive, args: object) -> int:
    """Idempotent: Archive.open already created the schema; launchd.install
    with the absolute path of the running `claudinator` executable and
    ~/.claudinator/sweep.log; archive.sweep(wait=True, progress=...) for the
    backfill; print status. Touches nothing under ~/.claude."""
    raise NotImplementedError


def cmd_uninstall(archive: Archive, args: object) -> int:
    """launchd.uninstall(). Prints where the database stays."""
    raise NotImplementedError


def cmd_sweep(archive: Archive, args: object) -> int:
    """archive.sweep(wait=True). What launchd runs. Prints the report as one
    line to stdout (the launchd log); exit 1 if report.errors or refused."""
    raise NotImplementedError


def cmd_rebuild(archive: Archive, args: object) -> int:
    """archive.rebuild(force=True)."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
