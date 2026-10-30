from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from claudinator.archive import Subagent
from claudinator.record import (
    AgentId,
    Block,
    Kind,
    Millis,
    Note,
    Other,
    Picture,
    Record,
    Thinking,
    Turn,
    line_type,
    tool_input,
)


@dataclass(frozen=True, slots=True, order=True)
class Key:
    line_off: int
    block: int

    def __str__(self) -> str:
        return f"{self.line_off}.{self.block}"


@dataclass(frozen=True, slots=True)
class Said:
    key: Key
    ts: Millis | None
    kind: Kind
    text: str


@dataclass(frozen=True, slots=True)
class Output:
    key: Key
    ts: Millis | None
    text: str
    error: bool
    pictures: tuple[Picture, ...]
    tool_use_id: str | None


@dataclass(frozen=True, slots=True)
class Call:
    key: Key
    ts: Millis | None
    tool: str
    text: str
    input: str
    tool_use_id: str | None
    spawned: Subagent | None
    result: Output | None


@dataclass(frozen=True, slots=True)
class Thought:
    key: Key
    ts: Millis | None
    text: str


@dataclass(frozen=True, slots=True)
class Image:
    key: Key
    ts: Millis | None
    picture: Picture


@dataclass(frozen=True, slots=True)
class Hidden:
    lines: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True)
class Marker:
    key: Key
    shows: Note | Hidden


Step = Said | Call | Output | Thought | Image | Marker


def build(lines: Iterable[tuple[int, Record]], agents: Sequence[Subagent], agent_id: AgentId | None) -> list[Step]:
    """An Agent call links the subagent it started, unless that is this
    transcript: a fork's starts with a copy of its own spawning call."""
    spawned = {a.tool_use_id: a for a in agents if a.tool_use_id and a.id != agent_id}
    steps: list[Step] = []
    unanswered: dict[str, int] = {}
    hidden: list[tuple[int, str]] = []
    for off, rec in lines:
        match rec:
            case Turn():
                made = _turn_steps(off, rec, spawned)
            case Other(note=Note() as note):
                made = [Marker(Key(off, 0), note)]
            case _:
                hidden.append((off, line_type(rec)))
                continue
        if hidden:
            steps.append(Marker(Key(hidden[0][0], 0), Hidden(tuple(hidden))))
            hidden = []
        for step in made:
            if isinstance(step, Output) and (at := unanswered.pop(step.tool_use_id, None)) is not None:
                steps[at] = replace(steps[at], result=step)
                continue
            if isinstance(step, Call) and step.tool_use_id:
                unanswered[step.tool_use_id] = len(steps)
            steps.append(step)
    if hidden:
        steps.append(Marker(Key(hidden[0][0], 0), Hidden(tuple(hidden))))
    return steps


def _turn_steps(off: int, turn: Turn, spawned: dict[str, Subagent]) -> list[Step]:
    parts: dict[int, list[Picture]] = {}
    items: list[tuple[int, Block | Thinking | Picture]] = list(turn.blocks)
    for i, extra in turn.extras:
        if isinstance(extra, Picture) and extra.part is not None:
            parts.setdefault(i, []).append(extra)
        else:
            items.append((i, extra))
    steps: list[Step] = []
    for i, item in sorted(items, key=lambda pair: pair[0]):
        key = Key(off, i)
        match item:
            case Block(Kind.TOOL_CALL, text, tool, tool_use_id):
                steps.append(Call(key, turn.ts, tool, text, tool_input(item), tool_use_id, spawned.get(tool_use_id), None))
            case Block(Kind.TOOL_OUTPUT, text, _, tool_use_id, error):
                steps.append(Output(key, turn.ts, text, error, tuple(parts.get(i, ())), tool_use_id))
            case Block(kind, text):
                steps.append(Said(key, turn.ts, kind, text))
            case Thinking(text):
                steps.append(Thought(key, turn.ts, text))
            case Picture():
                steps.append(Image(key, turn.ts, item))
    return steps
