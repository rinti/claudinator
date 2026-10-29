# claudinator

claudinator keeps a permanent, searchable copy of every Claude Code transcript on this Mac in one SQLite file. Claude Code deletes a transcript 30 days after its last write. claudinator keeps the bytes, so you can find an old conversation by what was said, by project, or by date, read it in order, and resume it.

## Install

```sh
uv tool install /Users/andreas/dev/claudinator   # puts `claudinator` in ~/.local/bin
claudinator install
```

`install` creates `~/.claudinator/claudinator.db`, loads the launchd agent `com.claudinator.sweep` (hourly and at login), and runs the first sweep, which archives everything on disk now. It writes nothing under `~/.claude`. Running it again changes nothing.

## Commands

```
claudinator search WORDS... [--project P] [--since WHEN] [--until WHEN] [--tools] [--limit N]
claudinator sessions [--project P] [--since WHEN] [--until WHEN] [--limit N]
claudinator show ID [--agent AID] [--full]
claudinator resume ID
claudinator export ID [--agent AID] > file.jsonl
claudinator status
claudinator sweep
claudinator rebuild
claudinator install | uninstall
```

- `search` lists sessions where every word appears, best match first, with one excerpt each. By default it searches your prompts, Claude's replies, and session titles. `--tools` adds tool calls and tool output.
- `sessions` lists sessions by last activity.
- `show` prints a conversation in order from the archive, whether or not the transcript is still on disk. `--agent` prints one subagent's transcript. `--full` prints tool output in full.
- `resume` runs `claude --resume <id>` in the session's working directory, first restoring the transcript from the archive if Claude Code deleted it.
- `export` writes the archived transcript byte for byte. This restores a transcript that Claude Code has deleted.
- `status` reports archive size, coverage, unarchived bytes, and sweeper health. It exits 1 when the sweeper is not loaded, the last sweep failed, or the last sweep is older than two intervals.
- `sweep` archives new bytes now. launchd runs it every hour.
- `rebuild` re-derives the search index from the archive.
- `uninstall` unloads the sweeper and keeps the database.

`ID` is any unique prefix of a session id, at least 4 characters. `WHEN` is `2026-10-01`, `2026-10-01T14:00`, `7d`, or `12h`. Words match whole tokens, ignoring case and accents, so `forsta` finds "Första". Quote a phrase (`"launchd agent"`). End a word with `*` to match a prefix (`deploy*`).

Every read command first archives new bytes, and skips that step if another sweep holds the lock. A search therefore finds the turn that ended a minute ago in another terminal.

## Backtracking

The sample output below is illustrative. Session ids, projects, and titles are real ones from this Mac.

Find a conversation by what was said:

```
$ claudinator search "launchd agent" --since 7d
711312d3  2026-10-29 20:13  ~/dev/claudinator  claudinator archive design  (6 hits)
    prompt  ...Hooks never fire for a session killed with SIGKILL. A [launchd agent] is the alternative or complement for periodic sweeps...
52a3f837  2026-10-24 11:02  ~/dev/SBR-Base  Worktree branch placement  (1 hit)
    reply   ...the nightly job runs from a [launchd agent] in ~/Library/LaunchAgents, so a worktree path in its plist...
```

Browse a project by date, then read one session:

```
$ claudinator sessions --project coacheroo --since 2026-10-01
SESSION   LAST ACTIVE       PROMPTS  AGENTS  PROJECT          TITLE
8f2d43fe  2026-10-29 19:44       47       0  ~/dev/coacheroo  Första coachmöte feedback
075b60d8  2026-10-16 16:16        3       0  ~/dev/coacheroo  Första coachmöte feedback  [gone]

$ claudinator show 8f2d43fe | less -R
8f2d43fe-dbb7-45bd-a6e3-1682089cd088  ~/dev/coacheroo  "Första coachmöte feedback"
2026-09-15 16:00 to 2026-10-29 19:44, 47 prompts, on disk (claudinator resume 8f2d43fe)

2026-09-15 16:01  you
  /model claude-fable-5-1
2026-09-15 16:03  you
  Läs anteckningarna från första coachmötet och sammanfatta feedbacken.
2026-09-15 16:03  claude
  > Read {"file_path":"/Users/andreas/dev/coacheroo/notes/2026-09-15.md"}
  < 212 lines
  Här är feedbacken i tre punkter ...
```

Resume, including a session whose transcript Claude Code has deleted:

```
$ claudinator resume 8f2d
(replaces itself with: cd ~/dev/coacheroo && claude --resume 8f2d43fe-dbb7-45bd-a6e3-1682089cd088)

$ claudinator resume 075b60d8
restored ~/.claude/projects/-Users-andreas-dev-coacheroo/075b60d8-51ec-4fc2-b1d9-290452e9cfcd.jsonl from the archive
(replaces itself with: cd ~/dev/coacheroo && claude --resume 075b60d8-51ec-4fc2-b1d9-290452e9cfcd)
```

`resume` writes the archived bytes back only when the transcript path does not exist, and never overwrites a file. It is the one command that writes under `~/.claude`.

Check that nothing is slipping:

```
$ claudinator status
archive   ~/.claudinator/claudinator.db  398 MB (1.56 GB of transcripts)
coverage  682 transcripts in 284 sessions, 0 unarchived bytes, 0 unparsed lines, 0 rewritten files
sweeper   loaded, last sweep 14 min ago (3 transcripts, 412 KB)
note      cleanupPeriodDays is unset, so Claude Code deletes transcripts after 30 days.
          The archive keeps them. Resuming one after that needs a restore (see `claudinator resume`).
```

---

# Rationale

## Problem

Claude Code's JSONL transcripts are the only copy of every conversation, and Claude Code deletes each one 30 days after its last write. The archive must become the durable copy. That rules out keeping anything less than the bytes. The format drifts between Claude Code versions. New record types appear, and fields such as `origin` were added to old ones. Whatever claudinator ignores today must still exist when a later claudinator learns to read it. Files grow while a session is live, several sessions write at once, and one line on this Mac is 15 MB. Several processes run ingest without coordinating, and a run that dies halfway must neither lose nor duplicate a byte. The constraints are macOS, standard-library Python 3.14 (which ships `compression.zstd`), SQLite 3.53 with FTS5, one database file, and read-only access to `~/.claude`.

## Usage (caller's view)

The consumer is a person at a terminal, and the README above is the spec. Inside the package, `cli.py` is the only caller of the archive:

```python
with Archive.open(Config.from_env()) as archive:
    archive.sweep(wait=False)
    for hit in archive.search(Query(terms=("launchd agent",), filters=Filters(since=week_ago))):
        print_hit(hit)
```

```python
session = archive.session("8f2d")   # unique prefix -> Session; UnknownSession or AmbiguousSession otherwise
for message in archive.conversation(session.summary.id):
    print_message(message, full=args.full)
```

```python
archive.export(session.summary.id, sys.stdout.buffer)   # the bytes Claude Code wrote, for a restore
```

## Shape

### The archive is the file's bytes

One SQLite file holds two layers. The archive layer is append-only and is never dropped or migrated. The projection layer is a pure function of the archive and is dropped and rebuilt when its version changes. The `-- @projection` marker in `claudinator/schema.sql` separates them.

The archive layer has four tables.

- `source` has one row per transcript file and generation. `session_id` and `agent_id` come from the path. `head` keeps the file's first bytes (up to 1 KiB) to detect a rewrite.
- `segment` holds the bytes as `(source_id, byte_start, byte_end, zstd)`. Concatenating a source's segments in order reproduces the file up to the cursor. The cursor is the last segment's `byte_end`, read through the primary key. It is derived and never stored, per minimize-reader-load ("derive instead of sync").
- `doc` keeps the latest body of the small side files that Claude Code rewrites in place. These are subagent `.meta.json` files (description, agent type, `toolUseId`), `custom-title.json`, `session-memory/summary.md`, and the desktop app's `local_*.json` (title, `cliSessionId`).
- `meta` holds the two schema versions and the last sweep report.

The projection layer has three tables.

- `transcript` holds per-source facts folded by `record.project()`: first cwd, first and last timestamp, latest AI title and custom title, and the count of unparsed lines.
- `entry` has one row per searchable unit: `(source_id, line_off, block, kind, ts)`. A unit is one content block of a user or assistant line, or one title change.
- `entry_fts` is a contentless FTS5 index. Its rowid is `entry.id`.

Each access pattern maps onto this structure without an added index.

- Reading a session in order scans its segments by primary key, decompresses them, and parses lines. That is the file's own order.
- Search runs an FTS match, joins `entry` and `source`, and groups hits by session. Each displayed hit decompresses one segment of at most 1 MiB to render its excerpt.
- Listing sessions is one query over `source`, `transcript`, and `entry` counts (`_SESSIONS_SQL`, tested in memory against the schema).
- Ingest reads the cursor from the primary key and appends.
- Export concatenates segments.
- Resume needs the main transcript path and its cwd. A `stat` at call time decides whether the file is still on disk, so no `gone` flag has to stay in sync.

### Ingest unit, cursor, and failure

The ingest unit is one delta of one transcript, meaning the complete lines between the archived end and the current end of the file. One transaction handles one delta. It takes the write lock with `BEGIN IMMEDIATE`, reads the cursor, opens the file and checks for a rewrite, reads exactly the bytes between the cursor and the size it just stat'ed, keeps them through the last newline, inserts line-aligned segments of about 1 MiB, projects those lines, and commits. This follows make-operations-idempotent. Each step derives from the state it finds, so a repeat finds nothing to do.

- **Partial last line.** Bytes after the last newline stay on disk, and the next sweep reads them again from the same cursor. The one exception is a file unchanged for an hour. Its tail is a torn write from a crashed writer, so the sweep archives it and nothing on disk stays unarchived. The projection counts that line as unparsed.
- **Crash.** Nothing is visible before `COMMIT`. A crash at any point leaves the old cursor, and the next sweep redoes the same delta. `synchronous=NORMAL` can drop the last commits on power loss. The source file still has those bytes, so the next sweep archives them again.
- **Concurrency.** Sweeps run from launchd, from every read command, and from `claudinator sweep`, all at once. SQLite's write lock is the only serialization. The cursor is read after the lock is taken, so a second process ingesting the same file sees the first one's segments and archives only newer bytes. Per separate-before-serializing-shared-state I looked for a way to remove the shared write target. The archive is one deduplicated copy by definition, so the sharing is real. The serialization is therefore structural (the database lock, co-located with the cursor), not a lockfile or a claim table. A lock is held for one delta. A per-turn delta averages about 150 KB (estimate: 1.56 GB over 10,035 `last-prompt` records). The worst case is the first backfill of the 79 MB file, under a second at the 180 MB/s the in-memory prototype measured (estimate). A throwaway prototype of this transaction, with four sweepers racing one writer that leaves partial lines, archived the file byte for byte with no duplicate (see Measurements).
- **Structural guard.** The segment primary key `(source_id, byte_start)` and a `BEFORE INSERT` trigger reject any segment that does not start at the archived end. A double-ingest bug or an overlap aborts the transaction instead of corrupting the archive. `UPDATE` and `DELETE` triggers make `segment` and `source` append-only. Per encode-lessons-in-structure, the invariant lives in the schema, not in a comment. I verified in an in-memory database that a duplicate start, a gap, an overlap, an update, and a delete are each rejected.
- **Rewrite.** A file whose first bytes differ from `head` (compared over the shorter of the two) was rewritten rather than appended. So was a file shorter than the cursor that has not changed for an hour. The old generation stays. A new `source` row (generation + 1) archives the file from byte 0. The hour matters because a shorter file with the right head is what a restore through `export` looks like while it is still being written, and treating it as a rewrite would archive a duplicate generation. No rewrite happened in the 682 files measured here. The check costs one column and one 1 KiB read, and without it a rewrite would stop the archive without any error.

### Drift on both sides

`record.parse` is the drift boundary, per boundary-discipline. It is a total function and the only code that reads Claude Code field names. Unknown record types become `Other`, which is archived and not indexed. A line that is not a JSON object becomes `Unparsed`. Unknown content block types are dropped from the projection and kept in the archive. Code past `parse` trusts the `Record` union.

When claudinator learns a new shape, it bumps `PROJECTION_VERSION`. `meta` also keeps a sha256 of the projection DDL, derived from `schema.sql` at run time, so a DDL edit needs no bump. When either differs, the next `open()` drops the projection and replays every segment in one transaction. Readers in other processes keep their WAL snapshot until it commits. A golden-digest test over a fixture corpus fails with "bump PROJECTION_VERSION" when projection output changes without a bump. The archive layer has its own `ARCHIVE_SCHEMA`. A new database is stamped with it, and there are no migrations. A claudinator refuses a database with an older archive schema. An older claudinator that opens a newer database reads it and refuses to sweep, so two installed versions cannot rebuild over each other. Every write transaction checks both stamps after it takes the lock, so an older process that already had the file open when a newer one upgraded it stops writing too.

### Sessions, subagents, forks

The file is the identity, per model-the-domain. `classify()` derives `session_id` and `agent_id` from the path and is the only code that knows the directory layout. A session is its main source plus the subagent sources that share its `session_id`.

On this Mac, 153 message uuids appear in more than one file. 105 sit in a main transcript and its background fork (`sessionKind: "bg"`). 48 sit in four forked subagents of one session. All 249 extra copies differ from the first occurrence, in `sessionId`, `agentId`, `sessionKind`, `slug`, and in 17 cases `message`. A copy is a new record that shares a uuid, so each occurrence stays in its own file's segments and its own entry rows. A forked conversation shows up under both sessions in search results, which matches what is on disk. There is no uuid table.

Subagent transcripts are sources under the parent session. Search labels a hit inside one with its agent id. `show --agent` prints one. In `show`, the parent's Agent tool call points to the subagent through the `toolUseId` in its meta doc.

### What triggers ingest

- **launchd** (`StartInterval` 3600, `RunAtLoad`) is the durability guarantee. An hourly sweep against a 30-day deletion window leaves a wide margin. It catches sessions killed with SIGKILL, crashed terminals, desktop-app sessions, and subagent files that finish after the parent's last turn.
- **Every read command** sweeps first with `wait=False`. This gives freshness exactly when someone looks. The fixed cost is the discovery walk, measured at 96 ms for 1,545 files.
- **No Claude Code hooks.** A hook does not fire on SIGKILL, it would start a process after every assistant turn, and it needs a write to `settings.json`. The two paths above already cover what a hook would add.

### Query surface and indexes

FTS5 is contentless, with `unicode61 remove_diacritics 2` and no porter stemmer. The user writes Swedish and English, and porter stems English only. The default scope is prompts, replies, and titles. `--tools` adds tool calls and tool output. Thinking blocks (0.2 MB, mostly empty), attachments (72 MB of hook output and skill listings), `isMeta` user lines, and user lines whose `origin.kind` is not `human` are not indexed. They stay in the archive.

On the full corpus the contentless index measured 52 MB, of which 49 MB is tool calls and tool output and 3 MB is conversation. The indexed text itself is 167 MB. External-content FTS would store that text again uncompressed, 50% on top of the 337 MB archive, to get `snippet()`. Instead `text_of()` re-derives a hit's text from its raw line. It is the same function that produced the indexed text, so what is shown cannot drift from what was indexed.

The search input cannot be an FTS5 syntax error. Each term becomes a quoted string, a trailing `*` stays outside the quotes, and terms are joined with AND. FTS5 operators are not exposed.

Indexes are the segment primary key (cursor, ordered scan, line lookup), `UNIQUE (path, generation)` on `source` (latest generation), `source (session_id, agent_id)`, `entry (source_id, kind)` (prompt counts), `UNIQUE (source_id, line_off, block)` on `entry` (no double projection), and `entry_fts`.

### Interface depth

`Archive` exposes `open`, `sweep`, `rebuild`, `search`, `sessions`, `session`, `conversation`, `export`, and `status`. Behind them sit zstd segments, derived cursors, generations, rewrite detection, the projection and its version, the FTS dialect, three path layouts, two side-file formats, and lock handling. The CLI never sees SQL, byte offsets, or FTS syntax. `record.py` exposes `parse`, `project`, `text_of`, and the `Record` union, and no other module reads a Claude Code field. Per laziness-protocol there is no library API, no plugin point, no configuration file, and no SQL view for a second consumer that does not exist. The call chain is `cli.py` to `archive.py` to `record.py`.

### Screen against design-red-flags.md

- Shallow module. One deep `Archive`. Each CLI command is one call, plus the shared `sweep(wait=False)`.
- Information leakage. Claude Code field names live in `record.py` only, and path shapes live in `classify()` only. `Kind` and `DocRole` values are bound into SQL as parameters instead of repeated as literals.
- Temporal decomposition. Ingest and rebuild share `_project()`. No load, transform, and save modules.
- Pass-through method. None. `session()` resolves the prefix and loads subagents in one call.
- Split ownership. Segments have one writer (`_ingest`), facts have one writer (`project()`), and docs have one writer (`_store_docs`).
- Two ways to do one task. One ingest path (`sweep`) with two lock policies. One text function (`text_of`) for indexing and display.
- Importable internals. Helpers are `_`-prefixed module privates. Python cannot enforce more than that without ceremony.
- Hand-synced list. `Kind` is the single list, and `schema.sql` has no CHECK that repeats it. The projection DDL's DROP lines sit next to their CREATE lines in one section. That is the one small hand-synced pair, and the rebuild test catches a miss.

## Synthesis decision

Four runners drafted designs in parallel (Opus, Fable, Codex gpt-6.1-sol, Codex gpt-6.1-terra). This document is the Opus candidate with the grafts below. The read-only cross-judge (gpt-6.1-terra) scored sol 26 and this candidate 25 of 30. It then recommended grafting this candidate's segment storage and derived cursor into sol, which converges on this shape. The parent picked this base on the simplicity criterion: sol's payload pool, guard hashes, cursor revisions, and stored tails buy little over the settled-tail rule here.

Measured for the pick: compressing each line independently with zlib shrinks the corpus from 1560 MB to 660 MB, and the seven internal types are 66 MB of that. Keeping every type costs little, so no candidate's drop list survives. This candidate's 1 MiB zstd segments measured 337 MB.

Grafts:

- `resume` restores a deleted transcript itself, create-only, then resumes (Fable's resume-knows-it-is-gone, made automatic per experience-first). This relaxes the read-only-`~/.claude` constraint for that one explicit command.
- Title precedence is custom-title, then desktop-app title, then latest ai-title, then the first prompt (Fable's first-prompt fallback).
- `status` exits 1 when the sweeper is unhealthy (sol's sweep health reporting).
- After a settled tail is archived, the bytes up to the next newline at the start of the following segment are a torn continuation and count as unparsed (fixes the cross-judge's finding that the settled-tail rule complicates later appends).

Rejected:

- Claude Code hooks as a trigger (Fable, terra). Sweep-on-read plus hourly launchd covers freshness and durability without a `settings.json` write.
- A uuid-keyed message table, first writer wins (Fable). Copied uuids differ across files (measured above), so it would misattribute messages.
- A content-addressed payload pool, generation guards, and stored tails (sol). More machinery than a single-user archive needs.
- Uncompressed per-line storage and a switched projection build (terra). Disk size above the source, and a one-transaction rebuild already gives atomicity.
- `show --around` (Fable). `show ID | less -p WORD` does the same.

## Tradeoffs accepted

- We accept archiving records nobody reads today (`api-request*` is 384 MB raw and compresses with the rest) in exchange for byte-exact export, resume after cleanup, and never having to decide in advance which type matters.
- We accept decompressing up to 1 MiB per displayed hit and per `show` in exchange for storing each text once.
- We accept compression 4% worse than whole-file zstd (1 MiB segments, measured) in exchange for one-line random access in about 1 ms and no compaction job.
- We accept that read commands write to the database (sweep-on-read) in exchange for no hooks and no `settings.json` edit.
- We accept up to an hour of lag for sessions nobody queries, against a 30-day deletion window.
- We accept a full projection rebuild on each `PROJECTION_VERSION` bump, an estimated 10 to 20 s today and minutes after years, in exchange for a projection that is always a pure function of the archive.
- We accept that a forked conversation appears under every session that contains it.
- We accept no stemming ("install" does not match "installing", but `install*` does) in exchange for correct Swedish tokens.
- We accept that a rewrite which keeps the first 1 KiB and leaves the file at least as long as the cursor goes undetected. The bytes are still archived as read, and the projection shows unparsed lines.

## Alternatives considered

- **One row per JSONL line, each compressed.** The interface would be the same. Per-line zstd measured 2.9x against 4.8x for 1 MiB segments on the same sample, which comes to an estimated 540 MB instead of 337 MB today. Export would need reassembly. It hides nothing more and costs 60% more disk.
- **Projection only, no raw bytes.** The database would be smallest. When Claude Code adds a field or type that today's projection drops, that data is gone after 30 days and no rebuild can recover it. It fails the goal.
- **Claude Code hooks (`Stop`, `SessionEnd`) as the trigger, launchd as the backstop.** The archive would be fresher between queries, but freshness only matters at query time, which sweep-on-read covers. It costs a `settings.json` write, a process per assistant turn, and a second trigger path to reason about.
- **A uuid-keyed message table with per-session membership, deduplicating forks.** It would save under 0.1% of rows. The measured copies differ in `sessionId`, `agentId`, and sometimes content, so "first writer wins" would attribute messages to the wrong session.
- **External-content FTS that stores the text.** `snippet()` would come free. It stores 167 MB of text a second time for today's corpus.
- **A file-watcher daemon (kqueue through `select`).** Ingest would be instant. It adds a long-running process to supervise and buys nothing over sweep-on-read plus launchd.

## Implementation reconciliation

The implementation lives in `claudinator/` and follows `sketch.py`. Each place it differs is listed here with its reason.

- **Side-file fields live in `record.py`.** `subagent_meta()`, `desktop_session()`, and `cleanup_period_days()` read `.meta.json`, the desktop app's `local_*.json`, and `settings.json`. This keeps `record.py` the only code that reads a Claude Code field name. As a result `_SESSIONS_SQL` no longer calls `json_extract(d.body, '$.title')`. It returns the desktop doc body and the first prompt's entry id, and `Archive._summary` applies the title precedence (custom, desktop, latest ai-title, first prompt) in one expression. The first-prompt title has its whitespace collapsed and is cut at 200 characters.
- **Compaction summaries are not prompts.** `parse` maps a user line with `isCompactSummary` to `Other`. `Kind.PROMPT` means text a person or a parent agent wrote, and a summary is neither. This Mac has 88 such lines.
- **`_project` takes records, not lines.** `_records(segments, after_newline)` turns segments into `(offset, Record)` pairs and is the one place that forces the first line after a segment without a trailing newline to `Unparsed` (the settled-tail graft). A torn continuation must be `Unparsed` even when its bytes are valid JSON, because a restarted writer can glue a whole record onto the torn bytes. Ingest, rebuild, and `conversation` all read through `_records`. Ingest decides whether the archive ends mid-line by decompressing the last segment, so the decision depends on the archive alone, as it does in rebuild. `_lines(source_id)` became `_segments(source_id)`.
- **The projection cannot block the archive.** `json.loads` turns an escaped lone surrogate such as `"\ud800"` into a str that SQLite refuses to bind. One such line raised `UnicodeEncodeError` at the FTS insert, rolled back its file's segments, and ended the sweep. `parse` now replaces lone surrogates with U+FFFD in every str it returns, side-file readers included. Separately, `_project` is total. A record whose fold raises, or yields a str SQLite cannot store, counts as one unparsed line, and its bytes commit with the rest of the segment.
- **One bad file never stops a sweep.** `sweep` catches any exception per file and records `path: repr(error)`, and does the same for the side-file transaction and for recording the report, so every read command keeps working. `_write` rolls back only while a transaction is open, because SQLite rolls back by itself on errors such as a full disk, and a second `ROLLBACK` used to replace the real error with "cannot rollback - no transaction is active". A write that waited `LOCK_WAIT_MS` (60 s) raises `Locked`, an `ArchiveError` that names another claudinator holding the lock, and ends the sweep instead of waiting again for every file. `cli.main` turns `sqlite3.Error` and `OSError` into one line on stderr and exit 1.
- **Ingest projects segment by segment.** `_ingest` inserts and projects each segment before compressing the next, instead of building the full chunk list first. A full backfill of this Mac peaked at 246 MB resident instead of 285 MB.
- **`export` takes a generation.** `Archive.export(..., generation=None)` and `claudinator export --generation N` exist because behavior test 7 needs the old generation of a rewritten file, and the sketch had no way to name one.
- **Only sweeps that wait for the lock become the last sweep.** Every read command sweeps first with `wait=False`, and `status` is a read command. If those sweeps were recorded, `status` would always see a sweep from a moment ago and could never report a dead sweeper.
- **`open()` takes no write lock when both versions are current.** It reads `meta` first and rebuilds only an older projection. Otherwise every read command would wait on any sweep in progress, which behavior test 8 forbids.
- **A new database is built privately and published whole.** `_create` makes `.<name>.new-<pid>` with mode 0600, sets WAL mode, runs both DDL sections, stamps both versions, closes it, and publishes it with `os.link`, which fails if the path exists. The loser of a race unlinks its file and opens the winner's. Before this, several processes opening a missing database each ran `PRAGMA journal_mode = WAL` on the same new file, and one failed with "database is locked" (8 processes released at once failed in 9 of 10 trials). An opener of an existing database never changes its journal mode. Because the file is created 0600, SQLite gives its `-wal` and `-shm` files the same mode, whatever the umask or the directory `CLAUDINATOR_DB` names. A file without the version stamps is refused as not a claudinator archive.
- **Docs.** `_store_docs` returns `(changed, errors)`. A desktop doc whose body is not JSON or names no `cliSessionId` is still stored, with `session_id` NULL, because title extraction no longer runs in SQL and cannot fail the listing. A side file that is not UTF-8 text becomes a sweep error. An upsert keeps the newer `mtime` when two sweepers race.
- **`tool-results/` is not archived.** `classify` returns None for `<slug>/<sid>/tool-results/*`. On this Mac that is 310 files and 60 MB, including 43 PNGs. The `doc` table holds the latest UTF-8 text of small files that Claude Code rewrites in place, and these are neither. See Open questions.
- **Session listing.** The `main` CTE takes `MAX(id)` per `session_id` among main transcripts, which is the latest generation of the newest path. A session that Claude Code moved between project directories (the `relocated` record type) then lists once. The SQL adds `:prefix` so `session(ref)` resolves prefixes in the same query. `--project` uses `instr(lower(cwd), lower(:project))`, so `_` and `%` in the argument are literal.
- **`Config.from_env` derives every default from `$HOME`**, the desktop directory included. Tests sandbox all three paths by setting `HOME` and need no extra variable.
- **launchd.** The module is `launchd.py` as the sketch names it. `install` boots the agent out and in only when the plist changed or the agent is not loaded, so a second `install` changes nothing. `launchctl()` is a module function that the tests replace, and a failed bootstrap raises `LaunchdError`.
- **No migration machinery.** The sketch ran the current archive DDL and then ordered migrations on every older database. That order breaks any migration that adds a column or a table, and no migration existed to test it. It is deleted. A new database gets the DDL and the stamp, a database at `ARCHIVE_SCHEMA` needs nothing, and an older stamp is refused with an `ArchiveError`.
- **Small ones.** `export` to a terminal exits 2. `search` with no match prints nothing and exits 0.

Measured after implementation (2026-10-29, read-only sweep of this Mac's `~/.claude` into a temporary database):

- First sweep. 692 transcripts in 289 sessions, 1,584 MB archived as 345 MB of zstd segments, a 409 MB database file, 418 side files, 0 unparsed lines, 0 errors, 18.4 s.
- Projection. 84,035 entries (38,183 tool calls, 37,940 tool outputs, 5,248 replies, 2,464 prompts, 188 titles). `rebuild(force=True)` took 15.5 s and produced rows identical to the incremental ones.
- A read command, sweep-on-read included, took 0.14 to 0.22 s end to end.
- Memory. Ingesting the 79 MB transcript alone peaks at 125 MB resident with an 82 MB Python heap, which is the whole new byte range read at once. The full backfill peaked at 246 MB resident.

## Open questions and risks

- Should claudinator offer to raise `cleanupPeriodDays`? `status` only warns today. The archive does not depend on it.
- Is `~/.claudinator` in this Mac's Time Machine backup? After 30 days the database is the only copy. Should claudinator ship `backup PATH` (SQLite `VACUUM INTO`)?
- Does `claude --resume <id>` from a terminal find a desktop-app session? Untested.
- Should search include subagent hits by default? This design says yes, labeled with the agent id.
- Growth is about 1 GB of transcripts a month at current use, which this design stores as roughly 250 MB a month (estimate from 337 MB archive plus 52 MB index per 1.56 GB). Is about 3 GB a year acceptable?
- A first backfill reads each file's whole new range at once, so peak memory follows the largest file. Measured 246 MB resident for this Mac's backfill, against the 60 MB estimated above. Steady-state sweeps read per-turn deltas. Is that acceptable for a one-time backfill?
- `tool-results/` files are not archived (see Implementation reconciliation). After Claude Code's cleanup, a resumed session that references one will not find it. Archiving them needs a binary-safe, compressed home such as a `blob` table.
- A session with no title falls back to its first prompt, which can be a slash-command wrapper (`<command-message>pstack:poteto-mode</command-message> ...`). Should the fallback strip command tags?
- The launchd plist runs `claudinator sweep` without `CLAUDINATOR_DB` or `CLAUDE_CONFIG_DIR`. A user who sets either for interactive use gets a sweeper that archives the default paths.

## Next implementation step

Implemented in `claudinator/`. Each behavior test below has a test in `tests/` that fails when its behavior breaks.

---

## Behavior tests (each must go red when its behavior breaks)

1. **Byte-exact archive.** Append lines across several sweeps, including a partial last line that is completed later. `export` equals the file. Breaking the newline cut or the cursor read turns it red.
2. **Partial line waits.** With the file's mtime fresh, a trailing partial line is not archived and not searchable. After the line is completed it appears exactly once.
3. **Settled tail.** With mtime set two hours back, a torn tail is archived. `export` equals the file, and `status` shows one unparsed line.
4. **Concurrent sweeps.** Four processes sweep the same growing directory while a writer appends. The result has the same segments and entry count as one sequential sweep. A trigger violation or a duplicate fails it.
5. **Incremental equals rebuild.** After N incremental sweeps with random append boundaries, dump `transcript` and `entry` plus each entry's `text_of`. Run `rebuild(force=True)` and dump again. The dumps are equal.
6. **Drift survives.** A line with an unknown `type`, a line with an unknown block type, and a non-JSON line are all exported byte-exact. None of them produces a search hit, and `status` counts the non-JSON line.
7. **Rewrite gets a generation.** Truncate a transcript and write different content. The old bytes remain exportable through the old generation, and the new content is searchable.
8. **Busy readers proceed.** While one process holds the write lock, `search` returns archived results with `busy=True` within one second.
9. **Golden projection.** A fixture corpus of redacted real lines projects to a recorded digest for the current `PROJECTION_VERSION`.

## Principles applied

- foundational-thinking. The core structure is the file's byte range, so every access pattern above traces through the primary key.
- make-operations-idempotent. The cursor is derived from segments inside the write transaction, and a crash at any point redoes the same delta.
- separate-before-serializing-shared-state. The shared target is real, so the serialization is SQLite's lock, co-located with the state it protects. There is no lockfile.
- encode-lessons-in-structure. Contiguity and append-only rules are triggers. The version-bump rule is a golden test whose failure names the fix.
- boundary-discipline. `parse` absorbs drift, `classify` absorbs layout, `cli.py` parses argv and dates, and `_fts_expr` is the boundary into FTS syntax.
- model-the-domain. `Record` and `Location` are unions, and facts are a fold, so there are no flag columns and no per-type branches spread across modules.
- laziness-protocol. There are no hooks, no uuid table, no views, no compaction job, no stored cursor, and no stored `gone` flag.
- minimize-reader-load. The call chain is three files, and every derived value is computed where it is read.

## Measurements (2026-10-29, this Mac, read-only)

- 682 transcript files and 257,090 lines. No invalid JSON and no partial last line at the time of measurement. The longest line is 15,005,206 bytes. 5.0 s for a full `json.loads` pass.
- zstd level 3 on a 60-file sample (82.9 MB). Per line 2.9x, 16 KiB 3.5x, 128 KiB 4.2x, 1 MiB 4.8x, whole file 5.0x.
- Full corpus in 1 MiB line-aligned segments. 1,560 MB became 337 MB (4.63x).
- Contentless FTS5 index on the full corpus. Tool calls and output 48.8 MB, conversation 3.1 MB. The in-memory build, with compression, took 8.6 s.
- Indexable text. Tool output 130.7 MB, tool calls 30.2 MB, prompts 3.9 MB, replies 3.2 MB, thinking 0.2 MB, attachments 72.0 MB (not indexed).
- Duplicate uuids. 153 uuids appear in more than one file, and all 249 extra copies differ from the first.
- Discovery walk with stat. 1,545 files in 96 ms.
- Concurrent ingest prototype (`proto/concurrent_ingest.py`, the `_ingest` transaction without projection). Four sweeper processes ran against one writer that split every line into two writes, for 3 s. The writer produced 2,290 lines and 22.4 MB. The archive matched the file byte for byte. The bytes archived by the four sweepers summed to the file size exactly, so nothing was archived twice, and no transaction hit an integrity error. A replayed segment insert was rejected by the contiguity trigger. Rerun it from `design/` with `python3 -I -B proto/concurrent_ingest.py ../claudinator/schema.sql`. It writes `p.db` and `t.jsonl` next to itself.
