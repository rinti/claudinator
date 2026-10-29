-- claudinator: one SQLite file, two layers.
--
-- Archive layer (above the @projection marker). The durable copy of every
-- transcript byte. Append-only and never dropped. It is created once, with
-- the database, and has no migrations: claudinator refuses a database whose
-- meta.archive_schema differs from archive.ARCHIVE_SCHEMA.
--
-- Projection layer (below the marker). A pure function of the archive layer
-- (claudinator/record.py). Dropped and recreated in one transaction whenever
-- meta.projection_version is older than record.PROJECTION_VERSION.
--
-- Pragmas live in code, not here. archive._create sets journal_mode=WAL once,
-- on the private file it publishes; every connection sets synchronous=NORMAL,
-- foreign_keys=ON, and busy_timeout.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,   -- 'archive_schema' | 'projection_version' | 'last_sweep'
  value TEXT NOT NULL
) STRICT;

-- One row per (transcript file, generation). A row exists only once its first
-- segment exists: both are inserted in the same transaction.
CREATE TABLE source (
  id          INTEGER PRIMARY KEY,
  path        TEXT    NOT NULL,            -- absolute path of the .jsonl file
  generation  INTEGER NOT NULL DEFAULT 0,  -- +1 when the file stops being an append of what we archived
  session_id  TEXT    NOT NULL,            -- from the path, never from file content
  agent_id    TEXT,                        -- NULL = main transcript; else subagents/agent-<agent_id>.jsonl
  head        BLOB    NOT NULL CHECK (length(head) BETWEEN 1 AND 1024),
                                           -- file bytes [0, len(head)) when generation began; rewrite detector
  first_seen  INTEGER NOT NULL,            -- epoch ms
  UNIQUE (path, generation)
) STRICT;
CREATE INDEX source_by_session ON source (session_id, agent_id);

-- The archive proper. Concatenating a source's segments in byte_start order
-- reproduces the file's bytes [0, cursor) exactly, where cursor is the
-- byte_end of the last segment. The cursor is derived, never stored.
CREATE TABLE segment (
  source_id   INTEGER NOT NULL REFERENCES source (id),
  byte_start  INTEGER NOT NULL,
  byte_end    INTEGER NOT NULL,
  zstd        BLOB    NOT NULL,   -- one zstd frame of file bytes [byte_start, byte_end), at most ~1 MiB
                                  -- uncompressed unless a single line is larger
  archived_at INTEGER NOT NULL,   -- epoch ms
  PRIMARY KEY (source_id, byte_start),
  CHECK (byte_end > byte_start)
) STRICT;

-- Contiguity: a segment must start exactly where its source's archive ends.
-- Together with the primary key this makes a double ingest or a gap fail the
-- transaction instead of corrupting the archive.
CREATE TRIGGER segment_contiguous
BEFORE INSERT ON segment
WHEN NEW.byte_start IS NOT COALESCE(
  (SELECT byte_end FROM segment
    WHERE source_id = NEW.source_id
    ORDER BY byte_start DESC LIMIT 1),
  0)
BEGIN
  SELECT RAISE(ABORT, 'segment must start at the archived end of its source');
END;

CREATE TRIGGER segment_no_update BEFORE UPDATE ON segment
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;
CREATE TRIGGER segment_no_delete BEFORE DELETE ON segment
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;
CREATE TRIGGER source_no_update BEFORE UPDATE ON source
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;
CREATE TRIGGER source_no_delete BEFORE DELETE ON source
BEGIN SELECT RAISE(ABORT, 'the archive is append-only'); END;

-- Latest content of small side files that Claude Code rewrites in place
-- instead of appending: subagent *.meta.json, <session>/custom-title.json,
-- <session>/session-memory/summary.md, and desktop-app local_*.json.
-- role and session_id/agent_id are set by the discovery classifier, the only
-- code that knows these path shapes.
CREATE TABLE doc (
  path        TEXT    PRIMARY KEY,   -- absolute path
  role        TEXT    NOT NULL,      -- archive.DocRole value
  session_id  TEXT,                  -- desktop docs: parsed cliSessionId
  agent_id    TEXT,
  sha256      BLOB    NOT NULL,
  body        TEXT    NOT NULL,      -- verbatim UTF-8
  mtime       INTEGER NOT NULL,      -- epoch ms
  archived_at INTEGER NOT NULL
) STRICT;
CREATE INDEX doc_by_session ON doc (session_id, role);

-- @projection
-- Everything below is disposable. Archive.rebuild executes this section as one
-- script inside one write transaction, then replays every segment.

DROP TABLE IF EXISTS entry_fts;
DROP TABLE IF EXISTS entry;
DROP TABLE IF EXISTS transcript;

-- Per-source facts folded by record.project() over the source's records in
-- file order. Exactly-once because each fold step commits with its segment.
CREATE TABLE transcript (
  source_id    INTEGER PRIMARY KEY REFERENCES source (id),
  cwd          TEXT,      -- first cwd seen; resume runs here
  first_ts     INTEGER,   -- epoch ms, min over records
  last_ts      INTEGER,   -- epoch ms, max over records
  ai_title     TEXT,      -- latest ai-title record
  custom_title TEXT,      -- latest custom-title record (/rename)
  unparsed     INTEGER NOT NULL DEFAULT 0   -- lines that were not a JSON object
) STRICT;

-- One row per searchable unit: a content block of a user/assistant line, or a
-- title change. Text is not stored here or in FTS; it is re-derived from the
-- segment that holds (source_id, line_off) when a hit is displayed.
CREATE TABLE entry (
  id        INTEGER PRIMARY KEY,   -- = entry_fts rowid
  source_id INTEGER NOT NULL REFERENCES source (id),
  line_off  INTEGER NOT NULL,      -- byte offset of the JSONL line in the file
  block     INTEGER NOT NULL,      -- index into message.content; 0 for string content and titles
  kind      TEXT    NOT NULL,      -- record.Kind value; the enum is the single list
  ts        INTEGER,               -- epoch ms
  UNIQUE (source_id, line_off, block)
) STRICT;
CREATE INDEX entry_by_source_kind ON entry (source_id, kind);

-- Contentless: the index only. bm25 ranking works; snippet()/highlight() do
-- not, by design (see design.md, "Query surface and indexes").
-- unicode61 without porter: the user writes Swedish and English, and porter
-- stems English only. remove_diacritics 2 lets "forsta" match "Första".
CREATE VIRTUAL TABLE entry_fts USING fts5 (
  text,
  content = '',
  tokenize = 'unicode61 remove_diacritics 2'
);
