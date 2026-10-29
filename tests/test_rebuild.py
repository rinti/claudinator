import random

from claudinator.record import PROJECTION_VERSION, Unparsed
from helpers import (
    AID,
    SID,
    SID2,
    ai_title,
    append,
    custom_title,
    found,
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
                        rest = rest[rest.find(b"\n") + 1 :]  # the writer died; its successor starts a fresh line
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
        assert found(archive, "quokka") == 1
    monkeypatch.setattr("claudinator.archive.PROJECTION_VERSION", PROJECTION_VERSION + 1)
    monkeypatch.setattr("claudinator.archive.parse", lambda line: Unparsed())
    with open_archive() as archive:
        assert found(archive, "quokka") == 0
        assert archive.status().unparsed_lines == 2
