import json
import os
import random
import sqlite3
import subprocess
import sys
import time

from claudinator.archive import Archive, Config
from helpers import AID, SID, SID2, append, exported, open_archive, tool_result, transcript, user

SWEEPER = r"""
import json, os, sys
from claudinator.archive import Archive, Config
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
