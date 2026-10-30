CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE source (
  id          INTEGER PRIMARY KEY,
  path        TEXT    NOT NULL,
  generation  INTEGER NOT NULL DEFAULT 0,
  session_id  TEXT    NOT NULL,
  agent_id    TEXT,
  head        BLOB    NOT NULL CHECK (length(head) BETWEEN 1 AND 1024),
  first_seen  INTEGER NOT NULL,
  UNIQUE (path, generation)
) STRICT;
CREATE INDEX source_by_session ON source (session_id, agent_id);

CREATE TABLE segment (
  source_id   INTEGER NOT NULL REFERENCES source (id),
  byte_start  INTEGER NOT NULL,
  byte_end    INTEGER NOT NULL,
  tail        BLOB    NOT NULL CHECK (length(tail) = 32),
  zstd        BLOB    NOT NULL,
  archived_at INTEGER NOT NULL,
  PRIMARY KEY (source_id, byte_start),
  CHECK (byte_end > byte_start)
) STRICT;

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

CREATE TABLE doc (
  path        TEXT    PRIMARY KEY,
  role        TEXT    NOT NULL,
  session_id  TEXT,
  agent_id    TEXT,
  sha256      BLOB    NOT NULL,
  body        BLOB    NOT NULL,
  size        INTEGER NOT NULL,
  mtime_ns    INTEGER NOT NULL,
  archived_at INTEGER NOT NULL
) STRICT;
CREATE INDEX doc_by_session ON doc (session_id, role);

-- @projection

DROP TABLE IF EXISTS entry_fts;
DROP TABLE IF EXISTS entry;
DROP TABLE IF EXISTS transcript;

CREATE TABLE transcript (
  source_id    INTEGER PRIMARY KEY REFERENCES source (id),
  cwd          TEXT,
  first_ts     INTEGER,
  last_ts      INTEGER,
  ai_title     TEXT,
  custom_title TEXT,
  unparsed     INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE TABLE entry (
  id        INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES source (id),
  line_off  INTEGER NOT NULL,
  block     INTEGER NOT NULL,
  kind      TEXT    NOT NULL,
  ts        INTEGER,
  UNIQUE (source_id, line_off, block)
) STRICT;
CREATE INDEX entry_by_source_kind ON entry (source_id, kind);

-- Contentless: the index only. bm25 ranking works; snippet()/highlight() do not.
CREATE VIRTUAL TABLE entry_fts USING fts5 (
  text,
  content = '',
  tokenize = 'unicode61 remove_diacritics 2'
);
