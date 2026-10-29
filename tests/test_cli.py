import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import claudinator.archive as archive_module
from claudinator import cli, launchd
from claudinator.layout import Config
from test_concurrency import LOCKER
from helpers import (
    AID,
    SID,
    SID2,
    SLUG,
    ai_title,
    append,
    assistant,
    exported,
    open_archive,
    projects,
    reply,
    tool_call,
    tool_result,
    transcript,
    user,
)

REAL_EXECUTABLE = cli._executable
NOW = 1790848800000 + 60_000  # a minute after the fixtures' 2026-10-01T10:00Z


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def cursor_hunt() -> None:
    append(
        transcript(),
        user("look into it", ts="2026-10-01T10:00:00.000Z"),
        assistant({"type": "thinking", "thinking": "hmm"}, ts="2026-10-01T10:00:30.000Z"),
        tool_call("Agent", {"description": "Find the cursor"}, tool_use_id="toolu_77", ts="2026-10-01T10:01:00.000Z"),
        tool_result("line one\nline two\nline three", tool_use_id="toolu_77", ts="2026-10-01T10:02:00.000Z"),
        reply("found it", ts="2026-10-01T10:03:00.000Z"),
        ai_title("Cursor hunt"),
    )
    append(transcript(agent=AID), user("find the cursor", ts="2026-10-01T10:01:30.000Z"))
    meta = projects() / SLUG / SID / "subagents" / f"agent-{AID}.meta.json"
    meta.write_text(json.dumps({"agentType": "Explore", "description": "Find the cursor", "toolUseId": "toolu_77"}))


def test_search_archives_new_bytes_first_and_prints_a_marked_excerpt(capsys):
    append(transcript(), user("deploy the quokka agent"), ai_title("Sweeper setup"))
    append(transcript(agent=AID), user("a quokka in a subagent"))
    assert run(capsys, "search", "quokka") == (
        0,
        "8f2d43fe  2026-10-01 10:00  /Users/me/dev/demo  Sweeper setup  (2 hits)\n"
        "    prompt  deploy the [quokka] agent\n",
        "",
    )
    assert run(capsys, "search", "subagent")[1] == (
        "8f2d43fe  2026-10-01 10:00  /Users/me/dev/demo  Sweeper setup  (1 hit)\n"
        "    prompt in agent a7c9c203  a quokka in a [subagent]\n"
    )


def test_sessions_prints_a_table_and_marks_deleted_transcripts(capsys, home):
    append(transcript(), user("one", ts="2026-10-02T09:30:00.000Z"), ai_title("Newer"))
    append(transcript(SID2, slug="-Users-me"), user("two", cwd=str(home)))
    assert run(capsys, "sessions")[0] == 0
    transcript(SID2, slug="-Users-me").unlink()
    assert run(capsys, "sessions")[1] == (
        "SESSION   LAST ACTIVE       PROMPTS  AGENTS  PROJECT             TITLE\n"
        "8f2d43fe  2026-10-02 09:30        1       0  /Users/me/dev/demo  Newer\n"
        "075b60d8  2026-10-01 10:00        1       0  ~                   two  [gone]\n"
    )


def test_show_prints_the_conversation_with_tool_summaries_and_agent_pointers(capsys):
    cursor_hunt()
    assert run(capsys, "show", "8f2d43fe") == (
        0,
        '8f2d43fe-dbb7-45bd-a6e3-1682089cd088  /Users/me/dev/demo  "Cursor hunt"\n'
        "2026-10-01 10:00 to 2026-10-01 10:03, 1 prompt, on disk (claudinator resume 8f2d43fe)\n"
        "agent a7c9c203  Explore  Find the cursor\n"
        "\n"
        "2026-10-01 10:00  you\n"
        "  look into it\n"
        "\n"
        "2026-10-01 10:01  claude\n"
        '  > Agent {"description":"Find the cursor"}\n'
        "    (subagent: claudinator show 8f2d43fe --agent a7c9c203)\n"
        "  < line one  (3 lines)\n"
        "  found it\n",
        "",
    )


def test_show_full_prints_tool_output_and_agent_prints_the_subagent(capsys):
    cursor_hunt()
    assert "  < line one\n    line two\n    line three\n" in run(capsys, "show", "8f2d43fe", "--full")[1]
    out = run(capsys, "show", "8f2d", "--agent", "a7c9")[1]
    assert out.endswith("agent a7c9c203  Explore  Find the cursor\n\n2026-10-01 10:01  you\n  find the cursor\n")


def test_show_reads_the_archive_after_claude_code_deletes_the_file(capsys):
    cursor_hunt()
    run(capsys, "sweep")
    shutil.rmtree(projects() / SLUG)
    code, out, _ = run(capsys, "show", "8f2d43fe")
    assert code == 0
    assert "deleted by Claude Code, kept in the archive" in out
    assert "  look into it\n" in out


def test_unknown_or_ambiguous_ids_exit_1(capsys):
    append(transcript(), user("one"))
    append(transcript("8f2d9999-0000-4000-8000-000000000000"), user("two"))
    code, _, err = run(capsys, "show", "ffff")
    assert (code, err) == (1, "claudinator: no archived session starts with ffff\n")
    code, _, err = run(capsys, "show", "8f2d")
    assert code == 1
    assert SID in err and "8f2d9999-0000-4000-8000-000000000000" in err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["search"],
        ["search", "x", "--since", "yesterday"],
        ["sessions", "--limit", "0"],
        ["show", "zz"],
        ["show", "8f2d43fe!"],
        ["frobnicate"],
    ],
)
def test_usage_errors_exit_2(capsys, argv):
    assert run(capsys, *argv)[0] == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-01", 1790812800000),
        ("2026-10-01T14:00", 1790812800000 + 14 * 3_600_000),
        ("7d", NOW - 7 * 86_400_000),
        ("12h", NOW - 12 * 3_600_000),
    ],
)
def test_parse_when(text, expected):
    assert cli.parse_when(text, NOW) == expected


@pytest.mark.parametrize("text", ["yesterday", "7w", "2026-13-01", "-1d", ""])
def test_parse_when_rejects_anything_else(text):
    with pytest.raises(ValueError):
        cli.parse_when(text, NOW)


def test_export_writes_the_archived_bytes(capsysbinary):
    append(transcript(), user("main"))
    append(transcript(agent=AID), user("subagent"))
    assert cli.main(["export", "8f2d"]) == 0
    assert capsysbinary.readouterr().out == transcript().read_bytes()
    assert cli.main(["export", "8f2d", "--agent", "a7c9"]) == 0
    assert capsysbinary.readouterr().out == transcript(agent=AID).read_bytes()
    original = transcript().read_bytes()
    transcript().write_bytes(user("rewritten"))
    assert cli.main(["export", "8f2d", "--generation", "0"]) == 0
    assert capsysbinary.readouterr().out == original


def test_resume_restores_a_deleted_transcript_then_execs_claude(capsys, tmp_path, execs):
    workdir = tmp_path / "project"
    workdir.mkdir()
    append(transcript(), user("hello", cwd=str(workdir)), reply("hi"))
    run(capsys, "sweep")
    original = transcript().read_bytes()
    shutil.rmtree(projects())

    code, out, _ = run(capsys, "resume", "8f2d")
    assert code == 0
    assert transcript().read_bytes() == original
    assert out == f"restored ~/.claude/projects/{SLUG}/{SID}.jsonl from the archive\n"
    assert execs == [(str(workdir), ["claude", "--resume", SID])]


def test_resume_never_overwrites_a_transcript_on_disk(capsys, tmp_path, execs):
    workdir = tmp_path / "project"
    workdir.mkdir()
    append(transcript(), user("hello", cwd=str(workdir)))
    run(capsys, "sweep")
    append(transcript(), user("a line Claude Code is still writing")[:30])
    on_disk = transcript().read_bytes()
    assert run(capsys, "resume", "8f2d")[:2] == (0, "")
    assert transcript().read_bytes() == on_disk
    assert execs == [(str(workdir), ["claude", "--resume", SID])]


def test_resume_without_its_directory_prints_the_command_and_exits_1(capsys, execs):
    append(transcript(), user("hello", cwd="/nonexistent/claudinator/project"))
    code, out, _ = run(capsys, "resume", "8f2d")
    assert code == 1
    assert out.endswith(f"\n  claude --resume {SID}\n")
    assert execs == []


def test_install_loads_the_agent_backfills_and_is_idempotent(capsys, home, launchctl):
    append(transcript(), user("archived by the first sweep"))
    claude_before = sorted(p for p in (home / ".claude").rglob("*"))
    code, out, _ = run(capsys, "install")
    assert code == 0
    plist = home / "Library" / "LaunchAgents" / "com.claudinator.sweep.plist"
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][1:] == ["sweep"]
    assert ("bootstrap", f"gui/{os.getuid()}", str(plist)) in launchctl.calls
    assert out.startswith("installed com.claudinator.sweep")
    assert "1 of 1 transcripts changed" in out
    assert sorted(p for p in (home / ".claude").rglob("*")) == claude_before

    launchctl.calls.clear()
    code, out, _ = run(capsys, "install")
    assert code == 0
    assert out.startswith("already installed com.claudinator.sweep")
    assert [c for c in launchctl.calls if c[0] in ("bootout", "bootstrap")] == []


def test_uninstall_unloads_the_agent_and_keeps_the_archive(capsys, home, launchctl):
    run(capsys, "install")
    assert run(capsys, "uninstall")[0] == 0
    assert not (home / "Library" / "LaunchAgents" / "com.claudinator.sweep.plist").exists()
    assert not launchctl.loaded
    assert Config.from_env().db.exists()


def test_plist_runs_sweep_hourly_and_at_login_and_logs_to_one_file():
    executable, log = Path("/Users/me/.local/bin/claudinator"), Path("/Users/me/.claudinator/sweep.log")
    agent = plistlib.loads(launchd.plist(executable, log, {}))
    assert agent["ProgramArguments"] == [str(executable), "sweep"]
    assert (agent["RunAtLoad"], agent["StartInterval"]) == (True, 3600)
    assert (agent["StandardOutPath"], agent["StandardErrorPath"]) == (str(log), str(log))
    assert "EnvironmentVariables" not in agent
    env = {"CLAUDINATOR_DB": "/Volumes/x/archive.db"}
    assert plistlib.loads(launchd.plist(executable, log, env))["EnvironmentVariables"] == env


def test_status_exits_0_when_the_sweeper_is_healthy(capsys):
    append(transcript(), user("hello"))
    run(capsys, "install")
    code, out, _ = run(capsys, "status")
    assert code == 0
    assert "coverage  1 transcripts in 1 sessions, 0 unarchived bytes, 0 unparsed lines, 0 rewritten files\n" in out
    assert "sweeper   loaded, last sweep 0 min ago" in out


def test_status_exits_1_when_the_sweeper_is_not_loaded(capsys, launchctl):
    run(capsys, "install")
    launchctl.loaded = False
    code, out, _ = run(capsys, "status")
    assert (code, "not loaded" in out) == (1, True)


def test_status_exits_1_when_the_last_sweep_is_older_than_two_intervals(capsys, monkeypatch):
    run(capsys, "install")
    later = archive_module.now() + 2 * 3600 * 1000 + 60_000
    monkeypatch.setattr(archive_module, "now", lambda: later)
    code, out, _ = run(capsys, "status")
    assert (code, "last sweep 2 h ago" in out) == (1, True)


def test_status_exits_1_when_the_last_sweep_failed(capsys):
    run(capsys, "install")
    unreadable = transcript()
    append(unreadable, user("cannot be read"))
    unreadable.chmod(0)
    try:
        assert run(capsys, "sweep")[0] == 1
        code, out, _ = run(capsys, "status")
    finally:
        unreadable.chmod(0o600)
    assert code == 1
    assert "1 errors" in out and "Permission denied" in out


def test_status_notes_claude_codes_cleanup_period(capsys, home):
    run(capsys, "install")
    assert "cleanupPeriodDays is unset, so Claude Code deletes transcripts after 30 days." in run(capsys, "status")[1]
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text('{"cleanupPeriodDays": 90}')
    assert "cleanupPeriodDays is 90, so Claude Code deletes transcripts after 90 days." in run(capsys, "status")[1]


def test_rebuild_rederives_the_search_index(capsys):
    append(transcript(), user("quokka"))
    run(capsys, "sweep")
    assert run(capsys, "rebuild")[0] == 0
    assert "(1 hit)" in run(capsys, "search", "quokka")[1]


def test_a_file_that_is_not_a_database_is_a_message_not_a_traceback(capsys, tmp_path, monkeypatch):
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not sqlite" * 100)
    monkeypatch.setenv("CLAUDINATOR_DB", str(garbage))
    assert run(capsys, "sessions") == (1, "", f"claudinator: {garbage}: file is not a database\n")


def test_an_unusable_database_directory_is_a_message_not_a_traceback(capsys, tmp_path, monkeypatch):
    (tmp_path / "a-file").write_text("")
    monkeypatch.setenv("CLAUDINATOR_DB", str(tmp_path / "a-file" / "claudinator.db"))
    code, out, err = run(capsys, "sessions")
    assert (code, out) == (1, "")
    assert err.startswith("claudinator: [Errno ") and str(tmp_path / "a-file") in err


def test_rebuild_while_another_process_holds_the_lock_names_it(capsys, monkeypatch):
    append(transcript(), user("quokka"))
    run(capsys, "sweep")
    monkeypatch.setattr(archive_module, "LOCK_WAIT_MS", 200)
    locker = subprocess.Popen(
        [sys.executable, "-c", LOCKER, str(Config.from_env().db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert locker.stdout.readline() == "locked\n"
        code, _, err = run(capsys, "rebuild")
    finally:
        locker.stdin.close()
        locker.wait(timeout=10)
    assert code == 1
    assert err.startswith("claudinator: another claudinator process is writing to the archive")


def test_status_exits_1_when_a_transcript_matches_no_known_layout(capsys):
    run(capsys, "install")
    stray = projects() / SLUG / "notes.jsonl"
    append(stray, user("a transcript in a layout claudinator does not know"))
    code, out, _ = run(capsys, "status")
    assert code == 1
    assert f"unknown   1 .jsonl file under projects/ matches no known layout and is not archived: {stray}\n" in out


def test_search_with_no_searchable_word_says_so(capsys):
    append(transcript(), user("the quokka → the zebracorn"))
    assert run(capsys, "search", "→", "*") == (
        1,
        "",
        "claudinator: nothing to search for: every word lacks a letter or a digit\n",
    )


HOSTILE = "zebracorn \x1b]52;c;ZXZpbA==\x07 then \x1b[2J and \x9b31m"
CONTROL = set(map(chr, [*range(0, 9), *range(11, 32), *range(127, 160)]))


def test_terminal_control_characters_in_a_transcript_are_shown_not_obeyed(capsys):
    append(transcript(), user(HOSTILE), reply("line one\x1b[31m\nline two"), tool_call("Bash", {"command": "x\x1b[2J"}))
    for argv in (["show", "8f2d"], ["sessions"], ["search", "zebracorn"], ["search", "zebracorn", "--tools"]):
        code, out, _ = run(capsys, *argv)
        assert code == 0
        assert CONTROL.isdisjoint(out), argv
    assert "zebracorn \\x1b]52;c;ZXZpbA==\\x07 then \\x1b[2J and \\x9b31m" in run(capsys, "show", "8f2d")[1]
    assert "  line one\\x1b[31m\n  line two\n" in run(capsys, "show", "8f2d")[1]


def test_show_into_a_pipe_that_closes_early_exits_0_quietly():
    append(transcript(), *(user(f"prompt number {i} " + "w" * 100) for i in range(3000)))
    reader = subprocess.Popen(
        [sys.executable, "-m", "claudinator", "show", "8f2d"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert reader.stdout.readline().startswith(b"8f2d43fe")
    reader.stdout.close()
    assert (reader.wait(timeout=60), reader.stderr.read()) == (0, b"")


def deleted_session(capsys, tmp_path) -> tuple[Path, bytes]:
    workdir = tmp_path / "project"
    workdir.mkdir()
    append(transcript(), user("hello", cwd=str(workdir)), reply("hi"))
    run(capsys, "sweep")
    original = transcript().read_bytes()
    transcript().unlink()
    return workdir, original


def test_resume_never_shows_a_partial_transcript_at_its_path(capsys, tmp_path, monkeypatch, execs):
    workdir, original = deleted_session(capsys, tmp_path)
    seen_while_writing = []
    real_export = archive_module.Archive.export

    def export(self, *args, **kwargs):
        seen_while_writing.append(transcript().exists())
        return real_export(self, *args, **kwargs)

    monkeypatch.setattr(archive_module.Archive, "export", export)
    assert run(capsys, "resume", "8f2d")[0] == 0
    assert seen_while_writing == [False]
    assert transcript().read_bytes() == original
    assert stat.S_IMODE(transcript().stat().st_mode) == 0o600
    assert sorted(p.name for p in transcript().parent.iterdir()) == [transcript().name]


def test_resume_without_claude_on_path_writes_nothing_and_exits_1(capsys, tmp_path, monkeypatch, execs):
    deleted_session(capsys, tmp_path)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out, _ = run(capsys, "resume", "8f2d")
    assert code == 1
    assert "claude is not on PATH" in out
    assert not transcript().exists()
    assert execs == []


def test_resume_without_its_directory_writes_nothing(capsys, tmp_path, execs):
    workdir, _ = deleted_session(capsys, tmp_path)
    workdir.rmdir()
    assert run(capsys, "resume", "8f2d")[0] == 1
    assert not transcript().exists()


def test_resume_replaces_a_staged_file_left_by_a_dead_process_with_its_pid(capsys, tmp_path, execs):
    _, original = deleted_session(capsys, tmp_path)
    stale = transcript().with_name(f".{transcript().name}.restore-{os.getpid()}")
    stale.write_bytes(b"half of an earlier restore")
    assert run(capsys, "resume", "8f2d")[0] == 0
    assert transcript().read_bytes() == original
    assert not stale.exists()


def test_resume_that_cannot_write_the_transcript_says_so_and_exits_1(capsys, tmp_path, execs):
    deleted_session(capsys, tmp_path)
    transcript().parent.chmod(0o500)
    try:
        code, _, err = run(capsys, "resume", "8f2d")
    finally:
        transcript().parent.chmod(0o700)
    assert code == 1
    assert err.startswith(f"claudinator: cannot restore ~/.claude/projects/{SLUG}/{SID}.jsonl: Permission denied")
    assert execs == []


def test_a_sweep_after_a_restore_archives_no_new_generation(capsys, tmp_path, execs):
    deleted_session(capsys, tmp_path)
    run(capsys, "resume", "8f2d")
    append(transcript(), user("the resumed session goes on"))
    with open_archive() as archive:
        report = archive.sweep()
        assert (report.new_generations, report.transcripts_changed) == (0, 1)
        assert archive.status().multi_generation == 0
        assert exported(archive) == transcript().read_bytes()


def installed_plist(home) -> dict:
    return plistlib.loads((home / "Library" / "LaunchAgents" / "com.claudinator.sweep.plist").read_bytes())


def test_install_hands_the_sweeper_the_paths_it_was_installed_with(capsys, home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDINATOR_DB", str(tmp_path / "elsewhere" / "archive.db"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/claude-work")
    assert run(capsys, "install")[0] == 0
    assert installed_plist(home)["EnvironmentVariables"] == {
        "CLAUDINATOR_DB": str(tmp_path / "elsewhere" / "archive.db"),
        "CLAUDE_CONFIG_DIR": str(home / "claude-work"),
    }


def test_install_with_default_paths_sets_no_environment(capsys, home):
    assert run(capsys, "install")[0] == 0
    assert "EnvironmentVariables" not in installed_plist(home)


def test_install_runs_the_claudinator_that_is_running(capsys, home, tmp_path, monkeypatch):
    script = tmp_path / "venv" / "bin" / "claudinator"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(cli, "_executable", REAL_EXECUTABLE)
    monkeypatch.setattr(sys, "argv", [str(script), "install"])
    assert run(capsys, "install")[0] == 0
    assert installed_plist(home)["ProgramArguments"][0] == str(script)


def test_install_without_a_claudinator_executable_writes_no_plist(capsys, home, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_executable", REAL_EXECUTABLE)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "gone" / "claudinator"), "install"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code, _, err = run(capsys, "install")
    assert code == 1
    assert err.startswith("claudinator: cannot find the claudinator executable")
    assert not (home / "Library" / "LaunchAgents" / "com.claudinator.sweep.plist").exists()


def test_a_sqlite_file_that_is_not_an_archive_is_refused_and_left_alone(capsys, tmp_path, monkeypatch):
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE notes (body TEXT)")
    db.commit()
    db.close()
    before = other.read_bytes()
    monkeypatch.setenv("CLAUDINATOR_DB", str(other))
    assert run(capsys, "sessions") == (1, "", f"claudinator: {other} is not a claudinator archive\n")
    assert other.read_bytes() == before


def test_status_names_the_reason_the_last_sweep_was_refused(capsys):
    append(transcript(), user("archived by the newer claudinator"))
    run(capsys, "install")
    db = sqlite3.connect(Config.from_env().db)
    db.execute("UPDATE meta SET value = '99' WHERE key = 'archive_schema'")
    db.commit()
    db.close()
    assert run(capsys, "sweep")[0] == 1
    code, out, _ = run(capsys, "status")
    assert code == 1
    assert "transcripts changed, 0 B, refused: this archive was written by a newer claudinator)\n" in out


def test_status_reports_a_directory_its_own_sweep_cannot_list_and_exits_1(capsys):
    append(transcript(), user("archived by a healthy sweep"))
    run(capsys, "install")
    hidden = transcript(SID2, slug="-Users-me-dev-hidden")
    append(hidden, user("behind a locked directory"))
    hidden.parent.chmod(0)
    try:
        code, out, _ = run(capsys, "status")
    finally:
        hidden.parent.chmod(0o700)
    assert code == 1
    assert f"\n          {hidden.parent}: PermissionError(13, 'Permission denied')\n" in out


def no_archive(path: Path) -> None:
    pass


def not_a_database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not sqlite" * 100)


def an_older_archive(path: Path) -> None:
    open_archive().close()
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = '0' WHERE key = 'archive_schema'")
    db.commit()
    db.close()


@pytest.mark.parametrize("archive", [no_archive, not_a_database, an_older_archive])
def test_uninstall_unloads_the_sweeper_whatever_state_the_archive_is_in(capsys, launchctl, archive):
    db = Config.from_env().db
    archive(db)
    before = db.read_bytes() if db.exists() else None
    launchctl.loaded = True
    code, out, _ = run(capsys, "uninstall")
    assert (code, launchctl.loaded) == (0, False)
    assert (db.read_bytes() if db.exists() else None) == before


@pytest.mark.parametrize("ts", ["0001-01-01T00:00:00.000Z", "9999-12-31T23:59:59.000Z", "1969-12-31T23:59:59.000Z"])
def test_a_timestamp_outside_1970_to_9999_reads_as_missing(capsys, monkeypatch, ts):
    monkeypatch.setenv("TZ", "Europe/Stockholm")
    time.tzset()
    append(transcript(), user("dated quokka", ts=ts))
    for argv in (["sessions"], ["search", "quokka"]):
        assert run(capsys, *argv)[::2] == (0, ""), argv
    code, out, err = run(capsys, "show", "8f2d")
    assert (code, err) == (0, "")
    assert "\n? to ?, 1 prompt, on disk" in out and "\n\n?  you\n  dated quokka\n" in out
