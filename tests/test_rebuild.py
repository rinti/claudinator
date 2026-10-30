import random
import sqlite3
import subprocess
import sys

import pytest

import claudinator.archive as archive_module
import claudinator.record as record_module
from claudinator.layout import Config
from claudinator.record import PROJECTION_VERSION, Turn, Unparsed
from test_concurrency import LOCKER
from helpers import (
    AID,
    SID,
    SID2,
    ai_title,
    append,
    custom_title,
    exported,
    hit_count,
    line,
    open_archive,
    projection_dump,
    reply,
    settle,
    tool_call,
    tool_result,
    transcript,
    user,
)


def conversation(rnd: random.Random, n: int) -> bytes:
    parts = []
    for i in range(n):
        ts = f"2026-10-{1 + i % 28:02d}T{i % 24:02d}:00:00.000Z"
        parts.append(
            rnd.choice(
                [
                    user(f"prompt {i} about quokka number {rnd.randint(0, 99)}", ts=ts),
                    reply(f"reply {i} " + "words " * rnd.randint(1, 3000), ts=ts),
                    tool_call("Bash", {"command": f"echo {i}"}, tool_use_id=f"toolu_{i}", ts=ts),
                    tool_result(f"output {i}", tool_use_id=f"toolu_{i}", ts=ts),
                    ai_title(f"Title {i // 10}"),
                    custom_title(f"Renamed {i // 25}"),
                    line({"type": "hologram", "text": f"unknown {i}"}),
                    b"not json at all\n",
                ]
            )
        )
    return b"".join(parts)


def a_successor_starts_a_fresh_line(unwritten: bytes) -> bytes:
    return unwritten[unwritten.find(b"\n") + 1 :]


def test_incremental_projection_equals_rebuild(home):
    rnd = random.Random(5)
    files = [transcript(), transcript(agent=AID), transcript(SID2)]
    pending = {path: conversation(rnd, 300) for path in files}
    with open_archive() as archive:
        while any(pending.values()):
            for path, rest in pending.items():
                cut = min(len(rest), rnd.randint(1, 200_000))
                append(path, rest[:cut])
                rest = rest[cut:]
                if rnd.random() < 0.15:
                    settle(path)
                    if rnd.random() < 0.5:
                        rest = a_successor_starts_a_fresh_line(rest)
                pending[path] = rest
            assert archive.sweep().errors == ()
        incremental = projection_dump(archive, home)
        archive.rebuild(force=True)
        rebuilt = projection_dump(archive, home)

    unparsed = sum(facts[-1] for _, _, facts, _ in incremental)
    not_json = sum(path.read_bytes().count(b"not json at all\n") for path in files)
    assert unparsed > not_json, "the scenario must archive settled tails and their continuations"
    assert rebuilt == incremental


def test_open_rebuilds_the_projection_after_a_version_bump(monkeypatch):
    append(transcript(), user("one quokka"), user("two"))
    with open_archive() as archive:
        archive.sweep()
        assert hit_count(archive, "quokka") == 1
    monkeypatch.setattr("claudinator.archive.PROJECTION_VERSION", PROJECTION_VERSION + 1)
    monkeypatch.setattr("claudinator.lines.parse", lambda line: Unparsed())
    with open_archive() as archive:
        assert hit_count(archive, "quokka") == 0
        assert archive.status().unparsed_lines == 2


def test_builds_that_share_a_projection_version_do_not_rebuild_each_others_projection(monkeypatch):
    append(transcript(), user("one quokka"))
    with open_archive() as archive:
        archive.sweep()
    edited = archive_module._PROJECTION_DDL + "\n-- a comment another build of this version wrote\n"
    monkeypatch.setattr(archive_module, "_PROJECTION_DDL", edited)
    monkeypatch.setattr(archive_module, "LOCK_WAIT_MS", 200)
    locker = subprocess.Popen(
        [sys.executable, "-c", LOCKER, str(Config.from_env().db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert locker.stdout.readline() == "locked\n"
        with open_archive() as archive:
            assert hit_count(archive, "quokka") == 1
    finally:
        locker.stdin.close()
        locker.wait(timeout=10)


def test_a_record_the_projection_refuses_in_an_early_segment_does_not_stop_a_rebuild(monkeypatch):
    real_project = record_module.project

    def project(facts, record):
        if isinstance(record, Turn) and "poison" in record.blocks[0][1].text:
            raise RuntimeError("a projection bug")
        return real_project(facts, record)

    monkeypatch.setattr("claudinator.archive.project", project)
    path = transcript()
    append(path, user("poison pill"), *(user(f"filler {i} quokka " + "x" * 1000) for i in range(3000)))
    with open_archive() as archive:
        assert archive.sweep().errors == ()
        assert archive._db.execute("SELECT COUNT(*) FROM segment").fetchone()[0] >= 3
        incremental = entry_rows(archive)
        assert archive.rebuild(force=True) == 1
        assert entry_rows(archive) == incremental
    monkeypatch.setattr("claudinator.archive.PROJECTION_VERSION", PROJECTION_VERSION + 1)
    with open_archive() as archive:
        assert entry_rows(archive) == incremental
        assert archive.status().unparsed_lines == 1
        assert hit_count(archive, "quokka") == 3000


def entry_rows(archive) -> list[tuple]:
    db = sqlite3.connect(archive.cfg.db)
    try:
        return db.execute(
            "SELECT s.path, s.generation, t.cwd, t.first_ts, t.last_ts, t.unparsed, e.line_off, e.block, e.kind, e.ts"
            " FROM source s JOIN transcript t ON t.source_id = s.id LEFT JOIN entry e ON e.source_id = s.id"
            " ORDER BY 1, 2, 7, 8"
        ).fetchall()
    finally:
        db.close()


PAUSED = user("the paused writer's quokka", ts="2026-10-03T08:00:00.000Z")
WHOLE = user("a whole wombat", ts="2026-10-04T08:00:00.000Z")

OPEN_LINES = {
    "the paused writer finishes the line": (PAUSED[:40], PAUSED[40:], 0, ["quokka"], []),
    "a restarted writer glues a whole record on": (PAUSED[:40], WHOLE, 1, ["wombat"], ["quokka"]),
    "a restarted writer glues more torn bytes on": (PAUSED[:40], b"zebracorn torn again\n", 1, [], ["quokka", "zebracorn"]),
    "a record that lacked only its newline gets one glued on": (PAUSED[:-1], WHOLE, 0, ["quokka", "wombat"], []),
}


@pytest.mark.parametrize(("tail", "then", "unparsed", "shown", "hidden"), OPEN_LINES.values(), ids=OPEN_LINES)
def test_a_line_left_open_by_a_settled_tail_projects_once_and_the_same_on_rebuild(tail, then, unparsed, shown, hidden):
    path = transcript()
    append(path, user("before"), tail)
    settle(path)
    with open_archive() as archive:
        archive.sweep()
        append(path, then, user("after the restart", ts="2026-10-05T08:00:00.000Z"))
        assert archive.sweep().errors == ()
        assert exported(archive) == path.read_bytes()
        assert archive.status().unparsed_lines == unparsed
        assert {word: hit_count(archive, word) for word in shown + hidden} == {word: 1 for word in shown} | {word: 0 for word in hidden}
        shown_by_show = [b.text for _, rec in archive.lines(SID) if isinstance(rec, Turn) for _, b in rec.blocks]
        incremental = entry_rows(archive)
        archive.rebuild(force=True)
        assert entry_rows(archive) == incremental
        assert [b.text for _, rec in archive.lines(SID) if isinstance(rec, Turn) for _, b in rec.blocks] == shown_by_show
    assert all(any(word in text for text in shown_by_show) for word in shown)
