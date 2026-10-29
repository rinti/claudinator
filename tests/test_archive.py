import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from claudinator.archive import (
    SEGMENT_BYTES,
    ArchiveError,
    Config,
    DocRole,
    TAIL_BYTES,
    NewerArchive,
    Plan,
    SideFile,
    TranscriptFile,
    classify,
    plan,
)
import claudinator.archive as archive_module
import claudinator.record as record_module
from claudinator.record import PROJECTION_VERSION, Block, Kind, Turn
from helpers import (
    AID,
    SID,
    SID2,
    SLUG,
    append,
    assistant,
    exported,
    found,
    line,
    open_archive,
    projects,
    reply,
    settle,
    transcript,
    user,
)


def complete_lines(path: Path) -> bytes:
    data = path.read_bytes()
    return data[: data.rfind(b"\n") + 1]


def test_export_is_byte_exact_across_sweeps():
    path = transcript()
    many = b"".join(user(f"line {i} " + "y" * 900) for i in range(3000))
    longer_than_a_segment = user("x" * (SEGMENT_BYTES + 5000))
    steps = [
        user("first"),
        reply("second") + user("third")[:20],
        user("third")[20:] + many,
        longer_than_a_segment + reply("tail")[:-7],
        reply("tail")[-7:],
    ]
    with open_archive() as archive:
        for chunk in steps:
            append(path, chunk)
            assert archive.sweep().errors == ()
            assert exported(archive) == complete_lines(path)
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 0


def test_partial_last_line_waits_until_completed():
    path = transcript()
    pending = user("the zebracorn line")
    append(path, user("complete"), pending[:25])
    with open_archive() as archive:
        archive.sweep()
        archive.sweep()
        assert exported(archive) == user("complete")
        assert found(archive, "zebracorn") == 0
        append(path, pending[25:])
        archive.sweep()
        assert exported(archive) == path.read_bytes()
        assert found(archive, "zebracorn") == 1


def test_settled_tail_is_archived_and_a_glued_continuation_is_unparsed():
    path = transcript()
    append(path, user("before the crash"), user("torn by a crash")[:30])
    settle(path)
    with open_archive() as archive:
        archive.sweep()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 1

        append(path, user("glued onto the torn bytes"), user("after the restart"))
        archive.sweep()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 2
        assert found(archive, "glued") == 0
        assert found(archive, "restart") == 1


def test_drift_is_archived_byte_exact_and_non_json_is_counted():
    path = transcript()
    append(
        path,
        user("hello"),
        line({"type": "hologram", "text": "zebracorn", "timestamp": "2026-10-01T10:00:01.000Z"}),
        assistant({"type": "server_widget", "text": "quokka"}),
        b"<<quagga is not json>>\n",
        reply("bye"),
    )
    with open_archive() as archive:
        assert archive.sweep().errors == ()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 1
        assert found(archive, "hello", tools=True) == 1
        for word in ("zebracorn", "quokka", "quagga"):
            assert found(archive, word, tools=True) == 0


def test_rewrite_starts_a_new_generation_and_keeps_the_old_bytes():
    path = transcript()
    append(path, user("original words"), reply("original reply"))
    with open_archive() as archive:
        archive.sweep()
        original = path.read_bytes()
        path.write_bytes(user("rewritten content", ts="2026-10-02T09:00:00.000Z"))
        assert archive.sweep().new_generations == 1
        assert exported(archive, generation=0) == original
        assert exported(archive) == path.read_bytes()
        assert archive.status().multi_generation == 1
        assert found(archive, "rewritten") == 1


def test_restore_in_progress_is_not_a_rewrite():
    path = transcript()
    append(path, *(user(f"message {i}") for i in range(200)))
    with open_archive() as archive:
        archive.sweep()
        archived = exported(archive)
        path.unlink()
        path.write_bytes(archived[: len(archived) // 2])
        assert archive.sweep().new_generations == 0
        path.write_bytes(archived)
        assert archive.sweep().new_generations == 0
        assert archive.status().multi_generation == 0
        assert exported(archive) == archived


def test_shorter_file_that_settles_is_a_rewrite():
    path = transcript()
    append(path, user("kept"), user("cut later"))
    with open_archive() as archive:
        archive.sweep()
        path.write_bytes(user("kept"))
        settle(path)
        assert archive.sweep().new_generations == 1
        assert exported(archive, generation=0) == user("kept") + user("cut later")
        assert exported(archive) == user("kept")


def test_second_sweep_archives_nothing():
    path = transcript()
    append(path, user("one"), reply("two"))
    with open_archive() as archive:
        first = archive.sweep()
        second = archive.sweep()
    assert (first.bytes_archived, first.transcripts_changed) == (path.stat().st_size, 1)
    assert (second.bytes_archived, second.transcripts_changed) == (0, 0)


def test_side_files_are_archived_when_they_change(home):
    session_dir = projects() / SLUG / SID
    meta = session_dir / "subagents" / f"agent-{AID}.meta.json"
    files = {
        meta: '{"agentType":"Explore","description":"Find the cursor","toolUseId":"toolu_01"}',
        session_dir / "custom-title.json": '{"customTitle":"Mine"}',
        session_dir / "session-memory" / "summary.md": "# Summary\n",
        session_dir / "tool-results" / "toolu_02.txt": "not a doc",
        home / "Library/Application Support/Claude/claude-code-sessions/a/b/local_1.json": '{"cliSessionId":"' + SID + '"}',
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    with open_archive() as archive:
        assert archive.sweep().docs_changed == 4
        assert archive.sweep().docs_changed == 0
        meta.write_text('{"agentType":"Explore","description":"Find the cursor, again"}')
        assert archive.sweep().docs_changed == 1


def test_older_claudinator_reads_but_refuses_to_sweep_a_newer_archive():
    path = transcript()
    append(path, user("archived by the newer version"))
    with open_archive() as archive:
        archive.sweep()
    db = sqlite3.connect(Config.from_env().db)
    db.execute("UPDATE meta SET value = '99' WHERE key = 'archive_schema'")
    db.commit()
    db.close()
    append(path, user("new bytes"))
    with open_archive() as archive:
        report = archive.sweep()
        assert report.refused
        assert report.bytes_archived == 0
        assert exported(archive) == user("archived by the newer version")


def test_classify_knows_the_layout(home):
    cfg = Config.from_env()
    p = cfg.claude_dir / "projects" / SLUG
    desktop = cfg.desktop_dir / "claude-code-sessions"
    cases = {
        p / f"{SID}.jsonl": TranscriptFile(p / f"{SID}.jsonl", SID, None),
        p / SID / "subagents" / f"agent-{AID}.jsonl": TranscriptFile(p / SID / "subagents" / f"agent-{AID}.jsonl", SID, AID),
        p / SID / "subagents" / f"agent-{AID}.meta.json": SideFile(
            p / SID / "subagents" / f"agent-{AID}.meta.json", DocRole.SUBAGENT_META, SID, AID
        ),
        p / SID / "custom-title.json": SideFile(p / SID / "custom-title.json", DocRole.SESSION_FILE, SID, None),
        p / SID / "session-memory" / "summary.md": SideFile(
            p / SID / "session-memory" / "summary.md", DocRole.SESSION_FILE, SID, None
        ),
        desktop / "a" / "b" / "local_x.json": SideFile(desktop / "a" / "b" / "local_x.json", DocRole.DESKTOP, None, None),
        p / SID / "tool-results" / "toolu_01.txt": None,
        p / SID / "subagents" / "stray.jsonl": None,
        p / "memory" / "MEMORY.md": None,
        p / "sessions-index.json": None,
        p / "not-a-session.jsonl": None,
        cfg.claude_dir / "settings.json": None,
        desktop / "a" / "local_x.json": None,
    }
    assert {path: classify(path, cfg) for path in cases} == cases


def test_missing_claude_directories_are_not_errors():
    with open_archive() as archive:
        assert (archive.sweep().transcripts_seen, archive.sweep().errors) == (0, ())
        append(transcript(), user("no desktop app on this machine"))
        assert (archive.sweep().transcripts_seen, archive.sweep().errors) == (1, ())


def test_an_archive_stamped_with_an_older_schema_is_refused_not_migrated():
    with open_archive():
        pass
    db = sqlite3.connect(Config.from_env().db)
    db.execute("UPDATE meta SET value = '0' WHERE key = 'archive_schema'")
    db.commit()
    db.close()
    with pytest.raises(ArchiveError, match="archive schema 0"):
        open_archive()
    db = sqlite3.connect(Config.from_env().db)
    assert db.execute("SELECT value FROM meta WHERE key = 'archive_schema'").fetchone() == ("0",)
    db.close()


def test_the_database_and_its_wal_files_are_private_whatever_the_umask(tmp_path, monkeypatch):
    elsewhere = tmp_path / "shared"
    elsewhere.mkdir(mode=0o755)
    monkeypatch.setenv("CLAUDINATOR_DB", str(elsewhere / "archive.db"))
    append(transcript(), user("private words"))
    previous = os.umask(0o022)
    try:
        with open_archive() as archive:
            archive.sweep()
            modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in elsewhere.iterdir()}
    finally:
        os.umask(previous)
    assert modes == {"archive.db": 0o600, "archive.db-wal": 0o600, "archive.db-shm": 0o600}


def set_meta(key: str, value: str) -> None:
    db = sqlite3.connect(Config.from_env().db)
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
    db.commit()
    db.close()


def test_an_open_archive_stops_writing_once_a_newer_claudinator_takes_over():
    path = transcript()
    append(path, user("archived before the upgrade"))
    with open_archive() as archive:
        archive.sweep()
        set_meta("projection_version", str(PROJECTION_VERSION + 1))
        append(path, user("arrives after the upgrade"))
        report = archive.sweep()
        assert report.refused
        assert report.bytes_archived == 0
        assert exported(archive) == user("archived before the upgrade")
        with pytest.raises(NewerArchive):
            archive.rebuild(force=True)


def test_status_reads_a_last_sweep_report_from_a_newer_claudinator():
    with open_archive() as archive:
        archive.sweep()
    set_meta("last_sweep", json.dumps({"started": 1, "finished": 2, "errors": [], "added_later": 7}))
    with open_archive() as archive:
        assert (archive.status().last_sweep.finished, archive.status().last_sweep.errors) == (2, ())


def test_a_line_with_an_escaped_lone_surrogate_is_archived_and_searchable():
    path = transcript()
    append(path, b'{"type":"user","message":{"content":"zebracorn \\ud800 here"},"cwd":"/d"}\n', user("next quokka"))
    append(transcript(SID2), user("another file"))
    with open_archive() as archive:
        report = archive.sweep()
        assert (report.errors, report.transcripts_changed) == ((), 2)
        assert exported(archive) == path.read_bytes()
        assert [found(archive, word) for word in ("zebracorn", "quokka")] == [1, 1]


def test_a_record_the_projection_cannot_handle_is_unparsed_and_its_bytes_still_archived(monkeypatch):
    real_project = record_module.project

    def project(facts, record):
        if isinstance(record, Turn) and "poison" in record.blocks[0][1].text:
            raise RuntimeError("a projection bug")
        return real_project(facts, record)

    monkeypatch.setattr("claudinator.archive.project", project)
    path = transcript()
    append(path, user("before"), user("poison pill"), user("after quokka"))
    with open_archive() as archive:
        assert archive.sweep().errors == ()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 1
        assert [found(archive, word) for word in ("before", "poison", "quokka")] == [1, 0, 1]


def test_a_projected_string_sqlite_cannot_store_makes_its_record_unparsed(monkeypatch):
    real_parse = record_module.parse

    def parse(line):
        record = real_parse(line)
        if isinstance(record, Turn) and record.blocks[0][1].text == "poison":
            return Turn(record.ts, record.cwd, ((0, Block(Kind.PROMPT, "\ud800")),))
        return record

    monkeypatch.setattr("claudinator.archive.parse", parse)
    path = transcript()
    append(path, user("poison"), user("after quokka"))
    with open_archive() as archive:
        assert archive.sweep().errors == ()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == 1
        assert found(archive, "quokka") == 1


def test_an_unexpected_error_in_one_file_is_reported_and_the_sweep_goes_on(monkeypatch):
    real_compress = archive_module.zstd.compress

    def compress(data, *args, **kwargs):
        if b"poison" in data:
            raise ValueError("cannot compress this")
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(archive_module.zstd, "compress", compress)
    append(transcript(), user("poison"))
    append(transcript(SID2), user("healthy quokka"))
    side = projects() / SLUG / SID / "custom-title.json"
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text('{"customTitle":"Mine"}')
    with open_archive() as archive:
        report = archive.sweep()
        assert report.errors == (f"{transcript()}: ValueError('cannot compress this')",)
        assert (report.transcripts_changed, report.docs_changed) == (1, 1)
        assert found(archive, "quokka") == 1
        assert archive.status().last_sweep.errors == report.errors


def test_a_transaction_that_sqlite_rolls_back_itself_reports_its_own_error():
    append(transcript(), user("rolled back"))
    with open_archive() as archive:
        archive._db.execute(
            "CREATE TEMP TRIGGER as_if_the_disk_were_full BEFORE INSERT ON segment"
            " BEGIN SELECT RAISE(ROLLBACK, 'database or disk is full'); END"
        )
        report = archive.sweep(wait=False)
        archive._db.execute("DROP TRIGGER as_if_the_disk_were_full")
        assert [e.split(": ", 1)[1] for e in report.errors] == ["IntegrityError('database or disk is full')"]
        assert archive.sweep().errors == ()
        assert exported(archive) == transcript().read_bytes()


def test_a_directory_the_sweep_cannot_list_is_an_error_and_the_rest_is_archived():
    append(transcript(), user("readable"))
    hidden = transcript(SID2, slug="-Users-me-dev-hidden")
    append(hidden, user("behind a locked directory"))
    hidden.parent.chmod(0)
    try:
        with open_archive() as archive:
            report = archive.sweep()
    finally:
        hidden.parent.chmod(0o700)
    assert report.errors == (f"{hidden.parent}: PermissionError(13, 'Permission denied')",)
    assert report.transcripts_changed == 1


def test_tool_results_directories_are_not_walked():
    append(transcript(), user("hello"))
    tool_results = projects() / SLUG / SID / "tool-results"
    tool_results.mkdir(parents=True)
    (tool_results / "toolu_01.txt").write_text("large output")
    tool_results.chmod(0)
    try:
        with open_archive() as archive:
            assert archive.sweep().errors == ()
    finally:
        tool_results.chmod(0o700)


def test_status_lists_transcripts_whose_path_matches_no_known_layout():
    append(transcript(), user("hello"))
    stray = projects() / SLUG / "notes.jsonl"
    append(stray, user("a transcript in a layout claudinator does not know"))
    append(projects() / SLUG / SID / "tool-results" / "toolu_01.jsonl", b"{}\n")
    with open_archive() as archive:
        archive.sweep()
        assert archive.status().unclassified == (stray,)


def test_a_rewrite_to_the_same_size_starts_a_new_generation():
    path = transcript()
    append(path, user("first words"), user("last words"))
    with open_archive() as archive:
        archive.sweep()
        original = path.read_bytes()
        rewritten = original.replace(b"last words", b"LAST WORDS")
        path.write_bytes(rewritten)
        assert archive.sweep().new_generations == 1
        assert exported(archive, generation=0) == original
        assert exported(archive) == rewritten
        assert archive.sweep().new_generations == 0


def test_a_rewrite_past_the_first_kib_that_grows_the_file_starts_a_new_generation():
    path = transcript()
    append(path, *(user(f"message {i} " + "x" * 200) for i in range(20)), user("the old ending"))
    with open_archive() as archive:
        archive.sweep()
        original = path.read_bytes()
        rewritten = original.replace(b"the old ending", b"a new ending, and longer") + user("more")
        path.write_bytes(rewritten)
        assert archive.sweep().new_generations == 1
        assert exported(archive, generation=0) == original
        assert exported(archive) == rewritten


def test_a_restored_transcript_is_not_a_rewrite():
    path = transcript()
    append(path, *(user(f"message {i} " + "x" * 300) for i in range(40)))
    with open_archive() as archive:
        archive.sweep()
        archived = exported(archive)
        path.unlink()
        path.write_bytes(archived)
        report = archive.sweep()
        assert (report.new_generations, report.bytes_archived) == (0, 0)
        append(path, user("resumed"))
        assert archive.sweep().new_generations == 0
        assert exported(archive) == path.read_bytes()
        assert archive.status().multi_generation == 0


ARCHIVED = b"".join(user(f"message {i} " + "x" * 300) for i in range(30))
HEAD = ARCHIVED[:1024]


def window(data: bytes, cursor: int = len(ARCHIVED)) -> bytes:
    return data[max(0, cursor - TAIL_BYTES) : cursor]


def changed_at(offset: int) -> bytes:
    return ARCHIVED[:offset] + b"#" + ARCHIVED[offset + 1 :]


PLAN_CASES = [
    ("same bytes", ARCHIVED, False, Plan.UNCHANGED),
    ("appended", ARCHIVED + user("more"), False, Plan.APPEND),
    ("same size, last line changed", changed_at(len(ARCHIVED) - 10), False, Plan.NEW_GENERATION),
    ("grown, line in the window changed", changed_at(len(ARCHIVED) - 2000) + user("more"), False, Plan.NEW_GENERATION),
    ("grown, head changed", changed_at(10) + user("more"), False, Plan.NEW_GENERATION),
    ("shorter, same head, still being written", ARCHIVED[:5000], False, Plan.WAIT),
    ("shorter, same head, settled", ARCHIVED[:5000], True, Plan.NEW_GENERATION),
    ("shorter, head changed", changed_at(10)[:5000], False, Plan.NEW_GENERATION),
    ("emptied, not settled", b"", False, Plan.WAIT),
    ("changed between head and window (not detected)", changed_at(3000) + user("more"), False, Plan.APPEND),
]


@pytest.mark.parametrize(("data", "settled", "expected"), [c[1:] for c in PLAN_CASES], ids=[c[0] for c in PLAN_CASES])
def test_plan_tells_an_append_from_a_rewrite(data, settled, expected):
    tail = hashlib.sha256(window(ARCHIVED)).digest()
    got = plan(HEAD, tail, len(ARCHIVED), len(data), settled, data[: len(HEAD)], window(data) if len(data) >= len(ARCHIVED) else b"")
    assert got is expected


def meta_path():
    return projects() / SLUG / SID / "subagents" / f"agent-{AID}.meta.json"


def description(archive) -> str:
    (agent,) = archive.session(SID[:8]).agents
    return agent.description


def test_a_side_file_that_is_not_utf8_is_kept_verbatim_and_read_leniently():
    append(transcript(), user("delegate"))
    append(transcript(agent=AID), user("task"))
    meta_path().write_bytes(b'{"agentType":"Explore","description":"Caf\xe9 hunt"}')
    with open_archive() as archive:
        assert archive.sweep().errors == ()
        assert description(archive) == "Caf� hunt"


def test_a_side_file_whose_content_changes_is_stored_even_with_an_older_mtime():
    append(transcript(), user("delegate"))
    append(transcript(agent=AID), user("task"))
    meta_path().write_text('{"description":"first"}')
    with open_archive() as archive:
        archive.sweep()
        meta_path().write_text('{"description":"restored from a backup"}')
        settle(meta_path())
        assert archive.sweep().docs_changed == 1
        assert description(archive) == "restored from a backup"
        os.utime(meta_path())
        assert archive.sweep().docs_changed == 0


def test_a_side_file_with_the_same_size_and_mtime_is_not_read_again():
    append(transcript(), user("delegate"))
    append(transcript(agent=AID), user("task"))
    meta_path().write_text('{"description":"read once"}')
    with open_archive() as archive:
        archive.sweep()
        meta_path().chmod(0)
        try:
            assert archive.sweep().errors == ()
        finally:
            meta_path().chmod(0o600)
