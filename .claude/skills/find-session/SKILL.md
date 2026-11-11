---
name: find-session
description: Search the claudinator archive of past Claude Code sessions, for when the user talked about something or for a session to resume. Use when asked to check or search the db, find an earlier conversation, or resume one.
---

# Find past sessions

Use the installed `claudinator` on PATH. It sweeps new transcript bytes into `~/.claudinator/claudinator.db` before it answers. The repo's own code (`uv run`, `python -m claudinator`) may carry an unreleased projection version and rebuild the live database. `sqlite3` finds no text, because the search index is contentless and transcripts sit in zstd segments.

Transcripts hold passwords, tokens, and personal data. Quote the user's words and describe the work; leave secrets in the archive.

## 1. Search wide

`claudinator search WORDS` lists sessions where every word appears somewhere in the prompts, replies, or titles, best first, each with its best-matching line. Case and diacritics are ignored.

- `"a phrase"` matches consecutive words. `faktur*` matches a prefix.
- Every word must match, so search the bare short word or prefix and leave the narrowing to step 2.
- There is no OR. Put one search per phrasing in a single Bash call, covering synonyms and the Swedish wording (the user writes both languages). Add any synonym the first results show.
- `--tools` also searches tool calls and tool output, which holds file names, commands, and errors.
- `--project P` keeps sessions whose directory contains P. `--since` and `--until` take `2026-10-01`, `7d`, or `12h`. `--limit N` defaults to 20; a search that returns exactly N sessions may have been cut off, so raise it.
- With no words to go on ("what was I doing in amex last week"), list by recency: `claudinator sessions --project amex --since 7d`.

Sessions in `~/dev/claudinator` that searched the archive, this one included, match the topic they searched for. Drop them. A `[gone]` at the end of a listing line means Claude Code deleted the transcript and only the archive has it.

## 2. Filter to the user's words

Search also matches Claude's replies and pasted logs. `prompts.py` in this skill's directory reads `search` or `sessions` output on stdin and prints, per session, each of the user's prompts that has a line matching a case-insensitive regex: the prompt's time and length, then each matching line with its line number. Anchor short words with `\b`, or `supp` matches "supported".

```sh
{ claudinator search 'invoice*' --limit 40; claudinator search 'faktur*' --limit 40; } | python3 .claude/skills/find-session/prompts.py '\binvoice|faktur'
echo ID | python3 .claude/skills/find-session/prompts.py .   # every prompt of one session
claudinator show ID | grep -n -C5 'WORD'                     # the context around a match
claudinator show ID | tail -40                               # where it stopped
```

A match deep in a long prompt (line 65 of 86) is usually pasted text: an email, a Slack thread, a deck. Read its context before you quote it as the user's own words. A line that ends in `…` was cut; `grep` the session for the rest. `ID` is any unique prefix of 4 or more characters.

For a session to resume, also search its distinctive words (a host, a file name) to catch the same work continued in a later session.

Done when every session left is one where the user talked about the topic, or, for a resume, the one that did the work.

## 3. Answer

Date each point by its prompt's time. The listing's date is when the session was last active. A title of `/clear` means the session has none; describe it instead.

**When the user talked about something:** group by session, oldest first. Give the dates, short id, and title, then the user's points with their times, each marked as their own words or as something they pasted. End with `claudinator show ID` for reading one in full.

**A session to resume:** give the short id, the last active date, the directory, the title, and one sentence on what it did and where it stopped. End with `claudinator resume ID` in a code block, for the user to run in a new terminal. It replaces its process with an interactive `claude --resume` in the session's directory, so it needs the user's own terminal. It restores a `[gone]` transcript to disk first. If the session's directory no longer exists, it prints a fallback command; pass that on.
