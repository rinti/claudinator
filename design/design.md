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
- `status` reports archive size, coverage, unarchived bytes, and sweeper health. It exits 1 when the sweeper is not loaded, the last sweep failed or was refused, the last sweep is older than two intervals, the sweep `status` itself ran first hit an error, or a `.jsonl` file under `projects/` matches no layout claudinator knows.
- `sweep` archives new bytes now. launchd runs it every hour.
- `rebuild` re-derives the search index from the archive.
- `uninstall` unloads the sweeper and keeps the database.

`ID` is any unique prefix of a session id, at least 4 characters. `WHEN` is `2026-10-01`, `2026-10-01T14:00`, `7d`, or `12h`. Words match whole tokens, ignoring case and accents, so `forsta` finds "Första". The words of a session can come from different turns, but a quoted phrase (`"launchd agent"`) must appear inside one. End a word with `*` to match a prefix (`deploy*`). A word with no letter or digit, such as `→`, is ignored.

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

## Limits

- A transcript rewritten in place goes unnoticed if the rewrite keeps the first 1 KiB, keeps the 4 KiB before the archived end, and does not shrink the file. Claude Code only appends, and none of this Mac's 697 transcripts was ever rewritten.

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
- `segment` holds the bytes as `(source_id, byte_start, byte_end, tail, zstd)`. `tail` is a sha256 of the file's last 4 KiB before `byte_end`, the other half of rewrite detection. Concatenating a source's segments in order reproduces the file up to the cursor. The cursor is the last segment's `byte_end`, read through the primary key. It is derived and never stored, per minimize-reader-load ("derive instead of sync").
- `doc` keeps the latest bytes of the small side files that Claude Code rewrites in place. These are subagent `.meta.json` files (description, agent type, `toolUseId`), `custom-title.json`, `session-memory/summary.md`, and the desktop app's `local_*.json` (title, `cliSessionId`).
- `meta` holds the two schema versions and the last sweep report.

The projection layer has three tables.

- `transcript` holds per-source facts folded by `record.project()`: first cwd, first and last timestamp, latest AI title and custom title, and the count of unparsed lines.
- `entry` has one row per searchable unit: `(source_id, line_off, block, kind, ts)`. A unit is one content block of a user or assistant line, or one title change.
- `entry_fts` is a contentless FTS5 index. Its rowid is `entry.id`.

Each access pattern maps onto this structure without an added index.

- Reading a session in order scans its segments by primary key, decompresses them, and parses lines. That is the file's own order.
- Search runs one FTS match per term, joins `entry` and `source`, and keeps the sessions in which every term matched an eligible entry, all in one query (`_SEARCH_SQL`). Each displayed hit decompresses one segment of at most 1 MiB to render its excerpt.
- Listing sessions is one query over `source`, `transcript`, and `entry` counts (`_SESSIONS_SQL`, tested in memory against the schema).
- Ingest reads the cursor from the primary key and appends.
- Export concatenates segments.
- Resume needs the main transcript path and its cwd. A `stat` at call time decides whether the file is still on disk, so no `gone` flag has to stay in sync.

### Ingest unit, cursor, and failure

The ingest unit is one segment of one transcript: about 1 MiB of complete lines that follow the archived end. One transaction handles one segment. It takes the write lock with `BEGIN IMMEDIATE`, reads the cursor, opens the file and checks for a rewrite, reads from the cursor to the first newline at or past 1 MiB (or to the last newline before the size it just stat'ed), inserts that segment, projects its lines, and commits. A file with more new bytes takes as many transactions as it has segments. This follows make-operations-idempotent. Each step derives from the state it finds, so a repeat finds nothing to do.

- **Partial last line.** Bytes after the last newline stay on disk, and the next sweep reads them again from the same cursor. The one exception is a file unchanged for an hour. Its tail is a torn write from a crashed writer, so the sweep archives it and nothing on disk stays unarchived. The projection counts that line as unparsed until bytes that complete it arrive.
- **Crash.** Nothing of a segment is visible before its `COMMIT`. A crash at any point leaves the segments committed before it, a line-aligned prefix of the file, and the next sweep goes on from that cursor. `synchronous=NORMAL` can drop the last commits on power loss. The source file still has those bytes, so the next sweep archives them again.
- **Concurrency.** Sweeps run from launchd, from every read command, and from `claudinator sweep`, all at once. SQLite's write lock is the only serialization. The cursor is read after the lock is taken, so a second process ingesting the same file sees the first one's segments and archives only newer bytes. Per separate-before-serializing-shared-state I looked for a way to remove the shared write target. The archive is one deduplicated copy by definition, so the sharing is real. The serialization is therefore structural (the database lock, co-located with the cursor), not a lockfile or a claim table. A lock is held for one segment, at most about 1 MiB of lines or one longer line. A per-turn delta averages about 150 KB (estimate: 1.56 GB over 10,035 `last-prompt` records), so most deltas are one segment. A throwaway prototype of this transaction, with four sweepers racing one writer that leaves partial lines, archived the file byte for byte with no duplicate (see Measurements).
- **Structural guard.** The segment primary key `(source_id, byte_start)` and a `BEFORE INSERT` trigger reject any segment that does not start at the archived end. A double-ingest bug or an overlap aborts the transaction instead of corrupting the archive. `UPDATE` and `DELETE` triggers make `segment` and `source` append-only. Per encode-lessons-in-structure, the invariant lives in the schema, not in a comment. I verified in an in-memory database that a duplicate start, a gap, an overlap, an update, and a delete are each rejected.
- **Rewrite.** A file whose first bytes differ from `head` (compared over the shorter of the two) was rewritten rather than appended. So was a file whose 4 KiB before the cursor no longer hash to the last segment's `tail`, and a file shorter than the cursor that has not changed for an hour. The pure function `plan()` makes this decision from those inputs and returns `UNCHANGED`, `APPEND`, `NEW_GENERATION`, or `WAIT`. A sweep checks it for every archived file, the ones whose size equals the cursor included, so a rewrite to the same size is caught too. The old generation stays. A new `source` row (generation + 1) archives the file from byte 0. The hour matters because a shorter file with the right head is what a restore through `export` looks like while it is still being written, and treating it as a rewrite would archive a duplicate generation. No rewrite happened in the 682 files measured here. The check costs two columns and two reads of at most 5 KiB per file per sweep, and without it a rewrite would stop the archive without any error, or append new bytes onto stale ones.

### Drift on both sides

`record.parse` is the drift boundary, per boundary-discipline. It is a total function and the only code that reads Claude Code field names. Unknown record types become `Other`, which is archived and not indexed. A line that is not a JSON object becomes `Unparsed`. Unknown content block types are dropped from the projection and kept in the archive. Code past `parse` trusts the `Record` union.

When claudinator learns a new shape or changes the projection DDL, it bumps `PROJECTION_VERSION`. When the version stamped in `meta` is older, the next `open()` drops the projection and replays every segment in one transaction. Readers in other processes keep their WAL snapshot until it commits. A golden-digest test over a fixture corpus fails with "bump PROJECTION_VERSION" when projection output changes without a bump, and a second golden test does the same when the projection DDL, comments stripped, changes. The archive layer has its own `ARCHIVE_SCHEMA`. A new database is stamped with it, and there are no migrations. A claudinator refuses a database with an older archive schema. An older claudinator that opens a newer database reads it and refuses to sweep, so two installed versions cannot rebuild over each other. Every write transaction checks both stamps after it takes the lock, so an older process that already had the file open when a newer one upgraded it stops writing too.

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

The search input cannot be an FTS5 syntax error. Each term becomes a quoted string, and a trailing `*` stays outside the quotes. FTS5 operators are not exposed. Terms without a token are dropped before either FTS or the excerpt sees them, because FTS5 matches nothing for an empty phrase.

Indexes are the segment primary key (cursor, ordered scan, line lookup), `UNIQUE (path, generation)` on `source` (latest generation), `source (session_id, agent_id)`, `entry (source_id, kind)` (prompt counts), `UNIQUE (source_id, line_off, block)` on `entry` (no double projection), and `entry_fts`.

### Interface depth

`Archive` exposes `open`, `sweep`, `rebuild`, `search`, `sessions`, `session`, `conversation`, `export`, and `status`. Behind them sit zstd segments, derived cursors, generations, rewrite detection, the projection and its version, the FTS dialect, three path layouts, two side-file formats, and lock handling. The CLI never sees SQL, byte offsets, or FTS syntax. `record.py` exposes `parse`, `project`, `text_of`, and the `Record` union, and no other module reads a Claude Code field. Per laziness-protocol there is no library API, no plugin point, no configuration file, and no SQL view for a second consumer that does not exist. The call chain is `cli.py` to `archive.py`, which calls `layout.py` (paths), `record.py` (fields), and `search_text.py` (the FTS dialect and excerpts).

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
- After a settled tail is archived, the bytes up to the next newline at the start of the following segment are the rest of the tail's line, and the projection reads the line whole (fixes the cross-judge's finding that the settled-tail rule complicates later appends). See "The projection reads file lines" below.

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
- We accept that a rewrite which keeps the first 1 KiB and the 4 KiB before the cursor, and leaves the file at least as long as the cursor, goes undetected. Claude Code appends to transcripts, and a rewrite that preserves both ends byte for byte at the same offsets has not been seen. Its new bytes after the cursor are still archived as read.

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
- **A prompt is what the person typed.** Of 2,471 prompt entries on this Mac, 521 began with a wrapper Claude Code adds. `parse` now sorts a person's user line further. A slash command (`<command-name>`, `<command-message>`, `<command-args>`) becomes the prompt text `/name args`, and a `!` shell command (`<bash-input>`) becomes `! cmd`, so titles, `show`, and search read what was typed. The output of either (`<bash-stdout>`, `<bash-stderr>`, `<local-command-stdout>`, `<local-command-stderr>`) is `TOOL_OUTPUT`. Claude Code's `[Request interrupted by user…]` marker is `Other`. A user line that carries a `tool_result` is tool output throughout, including the text Claude Code puts next to it. After the change the same corpus has 2,273 prompt entries and none starts with a wrapper. Three begin with `<system-reminder>`, a note the desktop app injects, and stay prompts. No real `<local-command-stderr>` line exists here, so its test uses the stdout line with the tag renamed. `PROJECTION_VERSION` is 2.
- **A title entry has the title line's time.** `Title` carries the `timestamp` of its own line, and its `TITLE` entry uses it, so `--since` and `--until` judge a title by when it was written. None of this Mac's 5,812 title lines has a timestamp today, so the entry falls back, as before, to the last activity seen before the line. `PROJECTION_VERSION` is 3. A dated title line also counts as activity, like any other dated line, so it moves the transcript's first and last timestamps and the session's place in `sessions` (`PROJECTION_VERSION` 7).
- **A timestamp is a date between 1970 and 9999.** `_millis` returns `None` for a timestamp before 1970-01-01 or after 9999-12-31 UTC, as for one that does not parse. A line dated `0001-01-01` used to make `show` and `sessions` end in a traceback, because the local date of that instant does not exist east of UTC (`fromtimestamp` raised for year 0 under `TZ=Europe/Stockholm`). The bounds sit at the parse boundary, so no caller handles such a time. `PROJECTION_VERSION` is 6.
- **Printed text cannot drive the terminal.** This Mac's transcripts hold 5,356 CSI sequences and 8 OSC sequences (counted as `\u001b[` and `\u001b]` in the JSONL). OSC 52 can set the clipboard. `cli._emit` is the one way text reaches the terminal, on stdout and stderr alike. It writes every C0 and C1 control character except newline and tab as a visible `\xNN` (`printable`), in every part but `Markup`, the sequences the CLI writes itself (bold around a search match, the carriage return of the progress line). The first fix round escaped at each call site, and five sites were missed: agent ids from file names, the directory in `resume`'s hint, the spawned-agent pointer in `show`, error lines that quote paths, and the first unknown file in `status`. A test now walks `cli.py`'s syntax tree and fails on any `print` or `write` outside `_emit`. Separately, `classify` takes an agent id only from a file name of `[0-9A-Za-z_-]+`, because the id is printed and typed back as `--agent`. All 417 subagent files on this Mac match. A file name outside that is unclassified, so `status` lists it and exits 1. `export` still writes bytes verbatim, and it already refuses a terminal. In `sessions` the project column is padded to the width of the escaped path.
- **A closed pipe is a normal end.** `claudinator show ID | head` used to exit 120 with "Exception ignored" on stderr. `main` flushes stdout before it returns, and on `BrokenPipeError` points stdout at `/dev/null` and exits 0.
- **`resume` restores through a staged file.** It used to open the transcript path with `xb` and export into it. A killed `resume` left a partial transcript there, and the next `resume` treated it as the real one, as could a second `resume` racing the first. Now `resume` first checks that the session's directory exists and that `claude` resolves on PATH, and writes nothing otherwise. It then writes `.<name>.restore-<pid>` with mode 0600, which `classify` ignores, syncs it, and publishes it with `os.link`, which fails rather than replace a file that appeared meanwhile. A file of that name can only be left by a dead process with the same pid, so `resume` removes it first. An `OSError` while restoring is one line naming the path, and exit 1. A restored transcript is byte-identical to the archive, so the next sweep sees `UNCHANGED` and then appends, with no new generation (tested).
- **Compaction summaries are not prompts.** `parse` maps a user line with `isCompactSummary` to `Other`. `Kind.PROMPT` means text a person or a parent agent wrote, and a summary is neither. This Mac has 88 such lines.
- **The projection reads file lines.** `_records(segments, before)` turns segments into `(offset, Record, mends)` and is the one place that knows a line can cross a segment boundary, which happens only after a settled tail. The first fix round dropped the continuation of a settled tail, so a complete record glued after torn bytes was in the archive and nowhere in `show` or `search`, and was counted nowhere. Now the tail's bytes and the continuation (up to the first newline) are parsed as one line when that makes a record, a writer that paused for an hour. Otherwise the continuation alone is parsed when it is a record, a restarted writer that appended a whole record onto torn bytes. Otherwise the line stays one unparsed line. When the tail was already counted unparsed in an earlier transaction and its line now parses whole, the record carries `mends`, and `_project` takes one off `unparsed` as it folds the record. If the tail itself was a whole record that lacked only its newline, it keeps that record, and what follows it is a line of its own. Ingest knows the archive ends mid-line from the last byte of the 4 KiB window it has just matched against the last segment's `tail`, and only then decompresses the last segment to hand `_records` the open line (`_open_line`). Rebuild runs the same function over all segments, so the two agree, and a test checks each outcome both ways. Search excerpts, first-prompt titles, and `conversation` read through `_records` too: `_entry_text` takes the record `_records` makes at the entry's offset from the bytes of that line, which `_line` reads across segments. `PROJECTION_VERSION` is 5.
- **The projection cannot block the archive.** `json.loads` turns an escaped lone surrogate such as `"\ud800"` into a str that SQLite refuses to bind. One such line raised `UnicodeEncodeError` at the FTS insert, rolled back its file's segments, and ended the sweep. `parse` now replaces lone surrogates with U+FFFD in every str it returns, side-file readers included. Separately, `_project` is total by construction. Each record's entry and FTS rows go in under a `SAVEPOINT`, and any exception from its fold or its inserts rolls back to it and counts the record as one unparsed line, while its bytes commit with the rest of the segment. The first fix round instead re-encoded every projected str to predict SQLite's refusal, and its inserts sat outside that check, so any other SQLite error still rolled back the whole segment. If SQLite has rolled back the whole transaction itself, as on a full disk, there is no savepoint left, and the error goes up as it is. A savepoint per record costs about 0.16 s over this Mac's 250,000 lines (measured in memory). The second fix round's savepoint broke `rebuild`. Once a transaction has changed the schema, as rebuild's DROP and CREATE do, SQLite answers `ROLLBACK TO` by aborting every statement open on the connection. Rebuild read each source's segments through one open scan while it projected them, so a refused record in any segment but a source's last aborted that scan and rolled the whole rebuild back. Because `open()` rebuilds after a `PROJECTION_VERSION` bump, every command and the sweeper would then fail after an upgrade. 323 of this Mac's 711 sources have more than one segment. Now no statement stays open while `_project` runs. `_segments` fetches a source's segment starts whole and then each payload by its primary key, a lookup whose one row ends the statement. A test puts a refused record in the first of four segments and runs `rebuild(force=True)` and an `open()` after a version bump.
- **One bad file never stops a sweep.** `sweep` catches any exception per file and records `path: repr(error)`, and does the same for the side-file transaction and for recording the report, so every read command keeps working. `_write` rolls back only while a transaction is open, because SQLite rolls back by itself on errors such as a full disk, and a second `ROLLBACK` used to replace the real error with "cannot rollback - no transaction is active". A write that waited `LOCK_WAIT_MS` (60 s) raises `Locked`, an `ArchiveError` that names another claudinator holding the lock, and ends the sweep instead of waiting again for every file. `cli.main` turns `sqlite3.Error` and `OSError` into one line on stderr and exit 1.
- **A sweep has one stop status.** The first fix round left three ways to end a sweep early. `_ingest` raised `_Busy` when nothing had committed and returned a delta flagged busy when something had, and the same exception-to-report mapping was written out three times. Now `_ingest` and `_store_docs` always return a `_Delta`, and `_failed(what, error)` is the one place that maps an exception: `_Busy` (another writer under `wait=False`), `Locked`, and `NewerArchive` stop the sweep, a vanished file is nothing, and anything else is one error. After `Locked` the sweep no longer waits another `LOCK_WAIT_MS` to record itself, so one stuck lock costs 60 s, not 120 s. A newer archive is detected before any file is read, not only by a write, because an older sweeper with nothing new to archive used to report success. Its refused report is recorded, through the one write that skips the version check, so `status` in the newer claudinator says `refused: this archive was written by a newer claudinator` and exits 1, instead of only calling the sweeper stale an hour later. `meta` is part of the archive layer, and `_report` drops keys it does not know and defaults the ones it lacks.
- **`status` is a read command like the others.** The first fix round declared it as not sweeping first, then swept inside `cmd_status` and passed one walk of the tree to both `sweep()` and `status()` through a `found` parameter on each. That parameter is gone. The command table declares `status` as sweeping first, `main` hands each command the report of that sweep, and `status` walks the tree a second time for its own report. Before, `status` dropped the errors of its own sweep, so a directory it could not list exited 0 when the hourly sweeper's last report was clean. Now it prints a `swept` line with those errors and exits 1.
- **The command table says what each command needs.** Each command declares one `_Needs` value. `CONFIG` gets the paths only, `ARCHIVE` gets the open archive, and `FRESH` gets the archive after a `wait=False` sweep and that sweep's report. `uninstall` is the one `CONFIG` command. It used to open the archive first, so it failed on an archive this claudinator refuses or cannot read, and on a machine without an archive it created an empty one. Now it unloads the sweeper whatever state the archive is in, and names the archive in its message only when one exists.
- **Discovery reports what it cannot see.** `discover` returns a `Discovery` of transcripts, side files, unclassified files, and errors, instead of a generator. `os.walk` used to skip a directory it could not list without a word. Now each one becomes a sweep error, so `status` exits 1. A missing directory is still not an error, because the desktop app may not be installed and Claude Code deletes session directories. A `.jsonl` file under `projects/` that `classify` does not recognise is listed in `Status.unclassified`, and `status` names the first and exits 1. If Claude Code changes its layout, the gap shows up instead of going unarchived.
- **Rewrite detection reads the tail too.** The sketch compared only the first 1 KiB and skipped any file whose size equalled the cursor. A rewrite to the same size was never inspected, and a rewrite past the first 1 KiB that did not shrink the file was appended onto the stale bytes, so `export` no longer equalled the file. Each segment now stores `tail`, and `ARCHIVE_SCHEMA` is 2. On this Mac's 697 files the check adds about 12 ms to a sweep-on-read (median of 7, 52 ms to 65 ms, measured into a temporary database).
- **A read command locks only for files with work.** A live session whose only new bytes are an unfinished line used to make every read command take the write lock for that file, and report "another sweep is writing" whenever one was. One function, `_next(path, archived)`, now decides what the next segment of a file is: it runs `plan()` and reads the segment `_next_segment` would cut, and returns `None` when there is nothing to archive. A sweep calls it outside the lock and skips the file on `None`, and `_ingest_segment` calls it again under the lock and inserts what it returns. The first fix round answered the outside question with a copy of that plan plus its own newline scan, and the copies disagreed. A settled empty file, or an archived file emptied and left to settle, made every read command take the write lock and archive nothing. Outside the lock `_next` reads up to one segment of a file that has new lines, which the scan did not, so a backfill reads each file's first segment twice.
- **Search matches per session.** The sketch joined the terms with AND inside one FTS expression, so all of them had to sit in one content block, while this document promised sessions where every word appears. `_SEARCH_SQL` now matches each term on its own, through a join with `json_each`, and keeps a session when every term matched some entry within the `--tools` scope and the time window. Sessions rank by the summed bm25 of their matches, and the excerpt comes from the entry that matches the most terms, then the best summed rank. `hits` counts the distinct entries that matched any term. `--project` and the limit apply inside `_SEARCH_SQL`, through the same `casefold` match as `sessions`, so the window that picks each session's best entry runs only for the sessions shown, and `_SESSIONS_SQL` reads only their summaries (`:ids`). The first fix round ran `_SESSIONS_SQL` for every session and filtered in Python. On a temporary database of this Mac's 707 transcripts, `Archive.search` for `the --tools` went from a median of 113 ms to 82 ms (9 runs each, `--project dev` the same), and the whole command from 0.26 s to 0.22 s (7 runs). 338 queries, with and without `--project`, `--tools`, a time window, and limits from 1 to 1,000, returned identical results before and after. A query whose every term lacks a token raises `EmptyQuery`, which the CLI prints. On this Mac `search the --tools`, the commonest word, took a median of 105 ms against 101 ms before (5 runs each).
- **Ingest commits segment by segment.** The sketch read a file's whole new range at once and archived it in one transaction, so memory and lock time grew with the file. `_ingest` now reads and commits one segment per transaction (`_next_segment`), and `chunk_lines` is gone. Measured with `/usr/bin/time -l` on a full backfill of this Mac's 697 transcripts into a temporary database, peak resident memory went from 244 MB to 166 MB. Ingesting the 79 MB transcript alone went from 125 MB to 52 MB. The remaining peak follows the longest line. The file with the 15 MB line peaks at 133 MB alone, because one segment holds that whole line and `json.loads` expands it.
- **`Plan.WAIT` stays, for copies made outside claudinator.** Its reason used to be claudinator's own restore, and `resume` now publishes a restore whole. A copy written in place still passes through a shorter file with the archived head: `claudinator export ID > path`, which the README offers as a restore, or a copy from a backup. Without `WAIT`, a sweep in the middle of one would archive the half as a new generation, and `status` would report a rewrite that never happened. With it, the sweep waits until the file has settled for an hour, by which time the copy is whole and matches the archive. A file that really is shorter for good becomes a new generation once it has settled, so `WAIT` delays a real rewrite by an hour and never hides one.
- **`export` takes a generation.** `Archive.export(..., generation=None)` and `claudinator export --generation N` exist because behavior test 7 needs the old generation of a rewritten file, and the sketch had no way to name one.
- **Only sweeps that wait for the lock become the last sweep.** Every read command sweeps first with `wait=False`, and `status` is a read command. If those sweeps were recorded, `status` would always see a sweep from a moment ago and could never report a dead sweeper.
- **`open()` takes no write lock when both versions are current.** It reads `meta` first and rebuilds only an older projection. Otherwise every read command would wait on any sweep in progress, which behavior test 8 forbids.
- **A new database is built privately and published whole.** `_create` makes `.<name>.new-<pid>` with mode 0600, sets WAL mode, runs both DDL sections, stamps both versions, closes it, and publishes it with `os.link`, which fails if the path exists. The loser of a race unlinks its file and opens the winner's. Before this, several processes opening a missing database each ran `PRAGMA journal_mode = WAL` on the same new file, and one failed with "database is locked" (8 processes released at once failed in 9 of 10 trials). An opener of an existing database never changes its journal mode. Because the file is created 0600, SQLite gives its `-wal` and `-shm` files the same mode, whatever the umask or the directory `CLAUDINATOR_DB` names. A file without the version stamps is refused as not a claudinator archive. `open()` looks for the `meta` table once, so the stamp check inside each write transaction is one query per stamp.
- **Docs.** `_store_docs` returns `(changed, errors)`. A desktop doc whose body is not JSON or names no `cliSessionId` is still stored, with `session_id` NULL, because title extraction no longer runs in SQL and cannot fail the listing. `doc.body` is a BLOB of the file's bytes, and `record.py` decodes it leniently where it reads a field, so a side file that is not UTF-8 is kept like any other instead of becoming a sweep error. `settings.json` is read the same way: `cleanup_period_days` takes its bytes, because `status` and `install` used to end in a `UnicodeDecodeError` traceback on a settings file with one byte that is not UTF-8. The sha256 alone decides whether a body changed. The sketch's upsert also required a newer `mtime`, which dropped a changed file restored with an older one, and it counted rows it had not written. `docs_changed` counts the bodies a sweep stored. `doc` stores each file's `size` and `mtime_ns`, and a sweep reads only side files whose pair differs. It compares the pairs without the lock, and takes the lock only when one differs. Under the lock it stats, reads, and compares each such file against its row as the row is then. The first fix round read and decided before taking the lock, so a slower sweep could overwrite a newer body with older bytes. Worse, a sweep that had found the bytes unchanged could then stamp another sweep's newer body with its own stat, and every later sweep skipped that file for good (reproduced in a test with two archives interleaved). Side files are small, and only files whose stat changed are read under the lock.
- **`tool-results/` is not archived.** `discover` does not enter `<slug>/<sid>/tool-results/`, and `classify` returns None for files there. On this Mac that is 310 files and 60 MB, including 43 PNGs. The `doc` table holds the latest UTF-8 text of small files that Claude Code rewrites in place, and these are neither. See Open questions.
- **Session listing.** The `main` CTE takes `MAX(id)` per `session_id` among main transcripts, which is the latest generation of the newest path. A session that Claude Code moved between project directories (the `relocated` record type) then lists once. The SQL adds `:prefix` so `session(ref)` resolves prefixes in the same query. `--project` uses `instr(casefold(cwd), casefold(:project))`, so `_` and `%` in the argument are literal. `casefold` is Python's `str.casefold`, registered on each connection, because SQLite's `lower()` folds ASCII only and `FÖRSTA` did not match `första`.
- **`Config.from_env` derives every default from `$HOME`**, the desktop directory included. Tests sandbox all three paths by setting `HOME` and need no extra variable.
- **launchd.** The module is `launchd.py` as the sketch names it. `install` boots the agent out and in only when the plist changed or the agent is not loaded, so a second `install` changes nothing. `launchctl()` is a module function that the tests replace. `bootout` returns before launchd has removed the agent, and a `bootstrap` right after it can fail with exit 5 until it has, so `install` tries the bootstrap up to five times, 0.5 s apart, and then raises `LaunchdError`. `launchd.sleep` is replaced in the tests too. When `CLAUDINATOR_DB` or `CLAUDE_CONFIG_DIR` is set, `install` writes its resolved path into the plist's `EnvironmentVariables`, so the hourly sweeper archives what `install` archived. Before, it swept the default paths. The plist runs the console script that is running `install`, so the sweeper is the version that wrote it, else the `claudinator` on PATH. It must be a file, or `install` fails with a message and writes no plist. The tests replace `cli._executable`, so the suite passes without `claudinator` on PATH.
- **No migration machinery.** The sketch ran the current archive DDL and then ordered migrations on every older database. That order breaks any migration that adds a column or a table, and no migration existed to test it. It is deleted. A new database gets the DDL and the stamp, a database at `ARCHIVE_SCHEMA` needs nothing, and an older stamp is refused with an `ArchiveError`.
- **The projection DDL is pinned by a test, not by `meta`.** The first fix round stored a sha256 of the projection DDL in `meta` and rebuilt whenever it differed. A comment edit then forced a foreground rebuild of about 15 s under the write lock, and two builds with the same `PROJECTION_VERSION` but different DDL text rebuilt against each other on every open (4 opens, 4 rebuilds). Now `open()` compares the version only, and `test_projection_golden.py` pins a sha256 of the DDL with its comments stripped for each version. It fails with "bump PROJECTION_VERSION" when the DDL changes and passes when only a comment does. A database stamped by the first round keeps a `projection_ddl` row in `meta` that nothing reads.
- **Modules.** `archive.py` grew past 1,000 lines. `layout.py` now holds `Config`, `DocRole`, the file types, `classify`, and `discover`. `search_text.py` holds `fts_expr`, `searchable`, the token folding, the term patterns, and `excerpt`. Both moves are verbatim apart from dropping the `_` of the three names `archive.py` imports. `archive.py` is still about 1,050 lines, because the fixes in this round added about 240. The next seam would be the file-reading helpers (`plan`, `_look`, `_next`, `_next_segment`, about 150 lines), left in place because their interface to `_ingest` is wide.
- **Small ones.** `archive.now()` is the one clock, and `Status.checked_at` carries it to `status`, so a test moves time in one place. Session and search rows are read by column name. `rebuild()` returns how many sources it replayed, one per transcript file and generation, so `claudinator rebuild` no longer walks the tree to count them, and it says "transcript generations" because that is what it counts. `claudinator sweep` stamps its log line with the sweep's own finish time, from `now()`, instead of a second reading of the wall clock. `export` to a terminal exits 2. `search` with no match prints nothing and exits 0. `SweepReport.lines_archived` is gone. Nothing read it, and it counted a torn line twice, once for its tail and once for its continuation.

Measured after implementation (2026-10-29, read-only sweep of this Mac's `~/.claude` into a temporary database):

- First sweep. 692 transcripts in 289 sessions, 1,584 MB archived as 345 MB of zstd segments, a 409 MB database file, 418 side files, 0 unparsed lines, 0 errors, 18.4 s.
- Projection. 84,035 entries (38,183 tool calls, 37,940 tool outputs, 5,248 replies, 2,464 prompts, 188 titles). `rebuild(force=True)` took 15.5 s and produced rows identical to the incremental ones.
- A read command, sweep-on-read included, took 0.14 to 0.22 s end to end.
- Memory. These were the sketch-shaped numbers. Ingesting the 79 MB transcript alone peaked at 125 MB resident with an 82 MB Python heap, which was the whole new byte range read at once. The full backfill peaked at 246 MB resident. See "Ingest commits segment by segment" for the numbers after.

Measured after the first fix round (2026-10-29, same method, the code before and after the round run alternately, 3 runs each, on a machine with a load average of about 4.5 on 10 cores):

- First sweep. 697 transcripts in 289 sessions, 1,595 MB archived as 347 MB of zstd segments, a 411 MB database file (411 MB before), 423 side files, 0 unparsed lines, 0 errors. Median 19.2 s (19.2 to 20.1) against 20.9 s (19.6 to 22.0) before.
- Peak resident memory of that sweep, from `/usr/bin/time -l`. Median 161 MB (161 to 163) against 251 MB (238 to 251) before. The longest line bounds it (see "Ingest commits segment by segment").
- Sweep-on-read, `open()` plus `sweep(wait=False)` with nothing new. Median of 7, three rounds, 56 to 62 ms against 52 to 55 ms before. The tail check opens each of the 697 files and reads up to 5 KiB, and the side-file stat check saves reading 423 files. A profile puts two thirds of the time in `discover`, mostly `pathlib` arithmetic in `classify`, not in system calls.
- Projection. 84,793 entries (38,559 tool calls, 38,463 tool outputs, 5,307 replies, 2,276 prompts, 188 titles). No prompt entry starts with a Claude Code wrapper.
- A separate check, which exports each file's latest generation and compares it with the file on disk, found all 697 byte-exact.

Measured after the second fix round (2026-10-30, same method, against the first round's code where a comparison is given, at a load average of 3 to 5 on 10 cores):

- First sweep. 707 transcripts in 290 sessions, 1,612 MB archived as 353 MB of zstd segments, a 417 MB database file, 432 side files, 0 unparsed lines, 0 errors, 18.9 s. Three runs each, alternated, took 18.6 to 19.2 s against 19.0 to 20.1 s.
- Peak resident memory of that sweep. Median 182 MB (178 to 186) against 161 MB (159 to 166). The rise comes from `_next` reading the next segment outside the lock as well as under it, which for the file with the 15 MB line reads that line one more time. Archiving that file alone peaks at 154 MB against 134 MB, while the peak of live Python memory is 67.8 MB with either code (`tracemalloc`). macOS keeps a freed block that large resident until it is reused or memory runs short (a separate check: RSS stayed 15 MB higher after a 15 MB read was freed), so the extra read shows in the peak and not in what the process holds. Avoiding it would mean returning the segment's bounds from `_next` and reading the bytes in a second step, which splits the one decision again.
- A second `claudinator sweep` with nothing new took 0.21 s end to end.
- Sweep-on-read, `open()` plus `sweep(wait=False)` with nothing new. Median of 15, 57 ms against 54 ms. `status`'s archive work, that sweep plus `status()`, took 97 ms against 61 ms, because it now walks the tree twice and one walk (`discover`) takes 30 ms.
- Projection. 85,702 entries (38,988 tool calls, 38,886 tool outputs, 5,348 replies, 2,291 prompts, 189 titles). `rebuild(force=True)` took 16.3 s, and its `transcript` and `entry` rows equal the incremental ones.
- Every archived file's latest generation is a byte-exact prefix of the file on disk (707 of 707).
- Six `claudinator sweep` processes started at once on a missing database all exited 0. Two of them archived everything between them, 186 and 523 files (two files partly by each), and four found nothing left. All 707 files were byte-exact afterwards.

## Open questions and risks

- Should claudinator offer to raise `cleanupPeriodDays`? `status` only warns today. The archive does not depend on it.
- Is `~/.claudinator` in this Mac's Time Machine backup? After 30 days the database is the only copy. Should claudinator ship `backup PATH` (SQLite `VACUUM INTO`)?
- Does `claude --resume <id>` from a terminal find a desktop-app session? Untested.
- Should search include subagent hits by default? This design says yes, labeled with the agent id.
- Growth is about 1 GB of transcripts a month at current use, which this design stores as roughly 250 MB a month (estimate from 337 MB archive plus 52 MB index per 1.56 GB). Is about 3 GB a year acceptable?
- Peak memory follows the longest line, because one line is never split across segments. That measured 166 MB resident for this Mac's backfill, against the 60 MB estimated above. A line of hundreds of MB would need a streaming path. None exists on this Mac.
- `tool-results/` files are not archived (see Implementation reconciliation). After Claude Code's cleanup, a resumed session that references one will not find it. Archiving them needs a binary-safe, compressed home such as a `blob` table.

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
- Concurrent ingest prototype (`proto/concurrent_ingest.py`, the `_ingest` transaction without projection). Four sweeper processes ran against one writer that split every line into two writes, for 3 s. The writer produced 2,290 lines and 22.4 MB. The archive matched the file byte for byte. The bytes archived by the four sweepers summed to the file size exactly, so nothing was archived twice, and no transaction hit an integrity error. A replayed segment insert was rejected by the contiguity trigger. It inserts segments positionally into the schema of that time, which has no `tail` column. Rerun it from `design/` with `git show a9a1926:claudinator/schema.sql > /tmp/schema-v1.sql && python3 -I -B proto/concurrent_ingest.py /tmp/schema-v1.sql`. It writes `p.db` and `t.jsonl` next to itself.
