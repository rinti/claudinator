import json
import os
import plistlib
import shutil
from pathlib import Path

import pytest

from claudinator import cli, launchd
from claudinator.archive import Config
from helpers import (
    AID,
    SID,
    SID2,
    SLUG,
    ai_title,
    append,
    assistant,
    projects,
    reply,
    tool_call,
    tool_result,
    transcript,
    user,
)

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
    assert [c[0] for c in launchctl.calls] == ["print", "print"]


def test_uninstall_unloads_the_agent_and_keeps_the_archive(capsys, home, launchctl):
    run(capsys, "install")
    assert run(capsys, "uninstall")[0] == 0
    assert not (home / "Library" / "LaunchAgents" / "com.claudinator.sweep.plist").exists()
    assert not launchctl.loaded
    assert Config.from_env().db.exists()


def test_plist_runs_sweep_hourly_and_at_login_in_the_background():
    body = launchd.plist(Path("/Users/me/.local/bin/claudinator"), Path("/Users/me/.claudinator/sweep.log"))
    assert plistlib.loads(body) == {
        "Label": "com.claudinator.sweep",
        "ProgramArguments": ["/Users/me/.local/bin/claudinator", "sweep"],
        "StartInterval": 3600,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": "/Users/me/.claudinator/sweep.log",
        "StandardErrorPath": "/Users/me/.claudinator/sweep.log",
    }


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
    later = cli._now_ms() + 2 * 3600 * 1000 + 60_000
    monkeypatch.setattr(cli, "_now_ms", lambda: later)
    monkeypatch.setattr("claudinator.archive._now", lambda: later)
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
