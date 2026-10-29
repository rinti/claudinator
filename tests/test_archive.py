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
    NewerArchive,
    SideFile,
    TranscriptFile,
    classify,
)
from claudinator.record import PROJECTION_VERSION
from helpers import (
    AID,
    SID,
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
