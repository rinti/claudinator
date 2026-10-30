import io
import json
import os
import sqlite3
import time
from pathlib import Path

from claudinator.archive import Archive, Query
from claudinator.layout import Config
from claudinator.record import parse, text_of

SLUG = "-Users-me-dev-demo"
CWD = "/Users/me/dev/demo"
SID = "8f2d43fe-dbb7-45bd-a6e3-1682089cd088"
SID2 = "075b60d8-51ec-4fc2-b1d9-290452e9cfcd"
AID = "a7c9c2033823d93f7"


def projects() -> Path:
    return Path(os.environ["HOME"]) / ".claude" / "projects"


def transcript(sid: str = SID, agent: str | None = None, slug: str = SLUG) -> Path:
    if agent:
        return projects() / slug / sid / "subagents" / f"agent-{agent}.jsonl"
    return projects() / slug / f"{sid}.jsonl"


def line(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def user(text: str, ts: str = "2026-10-01T10:00:00.000Z", cwd: str = CWD, **extra) -> bytes:
    return line({"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts, "cwd": cwd, **extra})


def assistant(*content: dict, ts: str = "2026-10-01T10:00:05.000Z", cwd: str = CWD) -> bytes:
    return line(
        {"type": "assistant", "message": {"role": "assistant", "content": list(content)}, "timestamp": ts, "cwd": cwd}
    )


def reply(text: str, ts: str = "2026-10-01T10:00:05.000Z") -> bytes:
    return assistant({"type": "text", "text": text}, ts=ts)


def tool_call(name: str, args: dict, tool_use_id: str = "toolu_01", ts: str = "2026-10-01T10:00:06.000Z") -> bytes:
    return assistant({"type": "tool_use", "id": tool_use_id, "name": name, "input": args}, ts=ts)


def tool_result(text: str, tool_use_id: str = "toolu_01", ts: str = "2026-10-01T10:00:07.000Z") -> bytes:
    return line(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}]},
            "timestamp": ts,
            "cwd": CWD,
        }
    )


def ai_title(text: str) -> bytes:
    return line({"type": "ai-title", "aiTitle": text, "sessionId": SID})


def custom_title(text: str) -> bytes:
    return line({"type": "custom-title", "customTitle": text, "sessionId": SID})


def append(path: Path, *chunks: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as f:
        for chunk in chunks:
            f.write(chunk)


def settle(path: Path, seconds: int = 2 * 3600) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def open_archive() -> Archive:
    return Archive.open(Config.from_env())


def exported(archive: Archive, sid: str = SID, agent: str | None = None, generation: int | None = None) -> bytes:
    out = io.BytesIO()
    archive.export(sid, out, agent, generation)
    return out.getvalue()


def found(archive: Archive, *terms: str, tools: bool = False) -> int:
    return sum(hit.hits for hit in archive.search(Query(terms, tools=tools, limit=1000)))


def projection_dump(archive: Archive, root: Path) -> list[tuple]:
    db = sqlite3.connect(archive.cfg.db)
    try:
        dump = []
        for source_id, path, generation, sid, agent in db.execute(
            "SELECT id, path, generation, session_id, agent_id FROM source ORDER BY path, generation"
        ).fetchall():
            data = exported(archive, sid, agent, generation)
            facts = db.execute(
                "SELECT cwd, first_ts, last_ts, ai_title, custom_title, unparsed FROM transcript WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            entries = []
            for line_off, block, kind, ts in db.execute(
                "SELECT line_off, block, kind, ts FROM entry WHERE source_id = ? ORDER BY line_off, block", (source_id,)
            ):
                end = data.find(b"\n", line_off)
                entries.append((line_off, block, kind, ts, text_of(parse(data[line_off : end if end >= 0 else None]), block)))
            dump.append((Path(path).relative_to(root).as_posix(), generation, facts, entries))
        return dump
    finally:
        db.close()
