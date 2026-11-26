# claudinator

claudinator keeps a copy of all Claude Code transcripts on this Mac.
Claude Code deletes a transcript 30 days after the last change.
claudinator keeps the transcript after that time.
You can find, read, and resume a session after Claude Code deletes it.

claudinator has two parts:

- The **archive** is one SQLite file, `~/.claudinator/claudinator.db`.
  It contains a copy of each transcript and a search index.
- The **sweeper** is a launchd agent, `com.claudinator.sweep`.
  It runs `claudinator sweep` each hour and at login.
  A sweep copies the new transcript data into the archive.

## Install

You must have Python 3.14 and uv.

1. Install the tool:

   ```sh
   uv tool install /path/to/claudinator
   ```

2. Make the archive and start the sweeper:

   ```sh
   claudinator install
   ```

The `install` command also does the first sweep.
This sweep copies all the transcripts on the disk into the archive.
claudinator does not write to `~/.claude`.

## Commands

| Command | Function |
| --- | --- |
| `claudinator search WORDS` | Find the sessions that contain all the words. |
| `claudinator sessions` | Show the sessions in the order of the last activity. |
| `claudinator show ID` | Show one conversation from the archive. |
| `claudinator serve` | Open the archive in your browser. |
| `claudinator resume ID` | Resume a session in Claude Code. If Claude Code deleted the transcript, claudinator restores it first. |
| `claudinator export ID` | Write the transcript from the archive as JSONL. |
| `claudinator status` | Show the size of the archive and the condition of the sweeper. |
| `claudinator sweep` | Copy new data into the archive now. |
| `claudinator rebuild` | Make the search index again from the archive. |
| `claudinator uninstall` | Stop the sweeper. The database stays. |

`ID` is the first 4 or more characters of a session ID.
To see all the options of a command, run `claudinator COMMAND --help`.
For more information, refer to `design/design.md`.

## Use with Claude Code

This repository contains the Claude Code skill `find-session`.
The skill tells Claude how to search the archive with claudinator.
Claude Code loads the skill only in this repository.

To use the skill in all projects, make a link to it:

```sh
ln -s /path/to/claudinator/.claude/skills/find-session ~/.claude/skills/find-session
```

To find a session, ask Claude about it, or type `/find-session`. Examples:

- "Find the session where I worked on the invoice export."
- "What did I say about launchd last week?"

Claude gives you the sessions that match, with their IDs.
To resume a session, Claude also gives you the command `claudinator resume ID`.
Run this command in a new terminal.

## Tests

Run the tests:

```sh
uv run pytest
```
