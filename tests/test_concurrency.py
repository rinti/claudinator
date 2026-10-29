import json
import os
import random
import sqlite3
import subprocess
import sys
import time

import pytest

from claudinator.archive import Archive, Query
from claudinator.layout import Config
from helpers import AID, SID, SID2, append, exported, open_archive, settle, tool_result, transcript, user

SWEEPER = r"""
import json, os, sys
from claudinator.archive import Archive
from claudinator.layout import Config
stop, wait = sys.argv[1], sys.argv[2] == "wait"
errors, archived = [], 0
with Archive.open(Config.from_env()) as archive:
    while not os.path.exists(stop):
        report = archive.sweep(wait=wait)
        errors += report.errors
        archived += report.bytes_archived
print(json.dumps({"errors": errors, "archived": archived}))
"""


def projection(db_path) -> tuple[list, list]:
    db = sqlite3.connect(db_path)
    try:
        entries = db.execute(
            "SELECT s.path, e.line_off, e.block, e.kind, e.ts FROM entry e JOIN source s ON s.id = e.source_id"
            " ORDER BY 1, 2, 3"
        ).fetchall()
        facts = db.execute(
            "SELECT s.path, t.cwd, t.first_ts, t.last_ts, t.ai_title, t.custom_title, t.unparsed"
            " FROM transcript t JOIN source s ON s.id = t.source_id ORDER BY 1"
        ).fetchall()
    finally:
        db.close()
    return entries, facts


def test_concurrent_sweeps_archive_each_byte_once(tmp_path):
    files = {transcript(): (SID, None), transcript(agent=AID): (SID, AID), transcript(SID2): (SID2, None)}
    for path in files:
        append(path, user("seed"))
    open_archive().close()
    stop = tmp_path / "stop"
    sweepers = [
        subprocess.Popen([sys.executable, "-c", SWEEPER, str(stop), mode], stdout=subprocess.PIPE, text=True)
        for mode in ("wait", "wait", "nowait", "nowait")
    ]
    rnd = random.Random(4)
    deadline = time.time() + 2.5
    i = 0
    while time.time() < deadline:
        path = rnd.choice(list(files))
        data = user(f"message {i} " + "z" * rnd.randint(0, 20_000)) if i % 3 else tool_result(f"output {i}")
        cut = rnd.randint(1, len(data) - 1)
        append(path, data[:cut])
        time.sleep(rnd.random() / 200)
        append(path, data[cut:])
        i += 1
    stop.touch()
    results = [json.loads(p.communicate(timeout=120)[0]) for p in sweepers]

    assert [r["errors"] for r in results] == [[], [], [], []]
    with open_archive() as archive:
        final = archive.sweep()
        assert final.errors == ()
        for path, (sid, agent) in files.items():
            assert exported(archive, sid, agent) == path.read_bytes()
    total = sum(path.stat().st_size for path in files)
    assert sum(r["archived"] for r in results) + final.bytes_archived == total

    sequential_db = tmp_path / "sequential.db"
    with Archive.open(Config.from_env({**os.environ, "CLAUDINATOR_DB": str(sequential_db)})) as fresh:
        fresh.sweep()
    assert projection(Config.from_env().db) == projection(sequential_db)


LOCKER = """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], autocommit=True)
db.execute("BEGIN IMMEDIATE")
print("locked", flush=True)
sys.stdin.read()
"""


def test_readers_proceed_while_another_process_holds_the_write_lock():
    path = transcript()
    append(path, user("archived before the lock: quokka"))
    with open_archive() as archive:
        archive.sweep()
    append(path, user("written while locked: quokka"))
    locker = subprocess.Popen(
        [sys.executable, "-c", LOCKER, str(Config.from_env().db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert locker.stdout.readline() == "locked\n"
        started = time.monotonic()
        with open_archive() as archive:
            report = archive.sweep(wait=False)
            hits = archive.search(Query(("quokka",)))
        elapsed = time.monotonic() - started
    finally:
        locker.stdin.close()
        locker.wait(timeout=10)
    assert report.busy
    assert [(h.session.id, h.hits) for h in hits] == [(SID, 1)]
    assert elapsed < 1


OPENER = r"""
import sys
from pathlib import Path
from claudinator.archive import Archive
from claudinator.layout import Config
go = Path(sys.argv[1])
while not go.exists():
    pass
try:
    with Archive.open(Config.from_env()) as archive:
        archive.sweep(wait=True)
except Exception as e:
    print(f"{type(e).__name__}: {e}")
else:
    print("ok")
"""


def test_processes_that_open_a_new_archive_at_once_all_succeed(tmp_path, monkeypatch):
    append(transcript(), user("seed"))
    for trial in range(3):
        monkeypatch.setenv("CLAUDINATOR_DB", str(tmp_path / f"cold{trial}" / "claudinator.db"))
        go = tmp_path / f"go{trial}"
        openers = [
            subprocess.Popen([sys.executable, "-c", OPENER, str(go)], stdout=subprocess.PIPE, text=True) for _ in range(8)
        ]
        time.sleep(0.5)
        go.touch()
        assert [p.communicate(timeout=60)[0] for p in openers] == ["ok\n"] * 8
        with open_archive() as archive:
            assert exported(archive) == transcript().read_bytes()


def test_a_sweep_that_gives_up_on_the_lock_leaves_the_readers_lock_wait_alone():
    append(transcript(), user("quokka"))
    with open_archive() as archive:
        (default,) = archive._db.execute("PRAGMA busy_timeout").fetchone()
        archive.sweep(wait=False)
        assert archive._db.execute("PRAGMA busy_timeout").fetchone() == (default,)
        assert default >= 1000


def test_a_reader_does_not_wait_for_the_lock_when_only_an_unfinished_line_is_new():
    path = transcript()
    append(path, user("archived"))
    append(transcript(SID2), user("a new file still on its first line")[:20])
    with open_archive() as archive:
        archive.sweep()
    append(path, user("Claude Code is still writing this")[:25])
    locker = subprocess.Popen(
        [sys.executable, "-c", LOCKER, str(Config.from_env().db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert locker.stdout.readline() == "locked\n"
        with open_archive() as archive:
            report = archive.sweep(wait=False)
    finally:
        locker.stdin.close()
        locker.wait(timeout=10)
    assert (report.busy, report.bytes_archived) == (False, 0)


@pytest.mark.parametrize("archived_first", [False, True], ids=["never archived", "archived, then emptied"])
def test_a_reader_does_not_wait_for_the_lock_for_a_settled_empty_file(archived_first):
    path = transcript()
    append(transcript(SID2), user("another session"))
    if archived_first:
        append(path, user("archived, then emptied"))
    with open_archive() as archive:
        archive.sweep()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    settle(path)
    with open_archive() as archive:
        archive.sweep()
    locker = subprocess.Popen(
        [sys.executable, "-c", LOCKER, str(Config.from_env().db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert locker.stdout.readline() == "locked\n"
        with open_archive() as archive:
            report = archive.sweep(wait=False)
    finally:
        locker.stdin.close()
        locker.wait(timeout=10)
    assert (report.busy, report.errors) == (False, ())
