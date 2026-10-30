from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import NewType

PROJECTION_VERSION = 8

SessionId = NewType("SessionId", str)
AgentId = NewType("AgentId", str)
Millis = NewType("Millis", int)  # epoch milliseconds, UTC


class Kind(enum.StrEnum):
    """What a searchable unit is."""

    PROMPT = "prompt"  # human-typed (main) or parent-written (subagent) user text
    REPLY = "reply"  # assistant text block
    TOOL_CALL = "tool_call"  # assistant tool_use: name + compact JSON input
    TOOL_OUTPUT = "tool_output"
    TITLE = "title"  # ai-title or custom-title, one entry per change


CONVERSATION: frozenset[Kind] = frozenset({Kind.PROMPT, Kind.REPLY, Kind.TITLE})
TOOLS: frozenset[Kind] = frozenset({Kind.TOOL_CALL, Kind.TOOL_OUTPUT})


@dataclass(frozen=True, slots=True)
class Block:
    """One displayable, indexable content block of a user or assistant line."""

    kind: Kind
    text: str
    tool: str | None = None
    tool_use_id: str | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """A `user` or `assistant` line that carries content worth showing.

    blocks pairs each kept block with its index in message.content (0 when
    content is a string), so entry.block can find it again in the raw line.
    Thinking, images, and unknown block types are dropped here; the raw line
    still has them."""

    ts: Millis | None
    cwd: str | None
    blocks: tuple[tuple[int, Block], ...]


@dataclass(frozen=True, slots=True)
class Title:
    text: str
    custom: bool
    ts: Millis | None = None


@dataclass(frozen=True, slots=True)
class Other:
    """Any other JSON object: system, attachment, api-request*, mode,
    file-history-*, isMeta user lines, non-human user origins, compaction
    summaries, and every type Claude Code adds later. Archived, never
    indexed. Contributes ts/cwd to Facts so first/last activity stays right."""

    ts: Millis | None
    cwd: str | None


@dataclass(frozen=True, slots=True)
class Unparsed:
    """Not a JSON object: a torn line from a crash, or a settled partial
    tail."""


Record = Turn | Title | Other | Unparsed


@dataclass(frozen=True, slots=True)
class Facts:
    cwd: str | None = None
    first_ts: Millis | None = None
    last_ts: Millis | None = None
    ai_title: str | None = None
    custom_title: str | None = None
    unparsed: int = 0


@dataclass(frozen=True, slots=True)
class EntryDraft:
    block: int
    kind: Kind
    ts: Millis | None


def parse(line: bytes) -> Record:
    """Total function from one JSONL line (without its newline) to a Record.

    It never raises. Anything it does not recognise becomes Other or
    Unparsed. Every str it returns is valid UTF-8 text that SQLite can
    store (see _text)."""
    try:
        obj = json.loads(line)
    except ValueError, RecursionError:
        return Unparsed()
    if not isinstance(obj, dict):
        return Unparsed()
    ts = _millis(obj.get("timestamp"))
    cwd = _str(obj.get("cwd"))
    match obj.get("type"):
        case "ai-title":
            return _title(obj.get("aiTitle"), False, ts) or Other(ts, cwd)
        case "custom-title":
            return _title(obj.get("customTitle"), True, ts) or Other(ts, cwd)
        case "assistant":
            return _turn(obj.get("message"), Kind.REPLY, ts, cwd)
        case "user" if _typed_by_a_person(obj):
            return _turn(obj.get("message"), Kind.PROMPT, ts, cwd)
        case _:
            return Other(ts, cwd)


def project(facts: Facts, record: Record) -> tuple[Facts, list[EntryDraft]]:
    """Fold one record into its transcript's facts and emit searchable units.

    Pure and deterministic. The text for each EntryDraft is re-derived by the
    caller with text_of()."""
    match record:
        case Unparsed():
            return replace(facts, unparsed=facts.unparsed + 1), []
        case Other(ts, cwd):
            return _observe(facts, ts, cwd), []
        case Turn(ts, cwd, blocks):
            drafts = [EntryDraft(i, b.kind, ts) for i, b in blocks if b.text.strip()]
            return _observe(facts, ts, cwd), drafts
        case Title(text, custom, ts):
            kept = "custom_title" if custom else "ai_title"
            observed = _observe(facts, ts, None)
            if text == getattr(facts, kept):
                return observed, []
            when = ts if ts is not None else facts.last_ts
            return replace(observed, **{kept: text}), [EntryDraft(0, Kind.TITLE, when)]


def text_of(record: Record, block: int) -> str:
    """The indexed and displayed text of entry `block` of this record."""
    match record:
        case Turn(blocks=blocks):
            for i, b in blocks:
                if i == block:
                    return b.text
        case Title(text):
            return text
    raise LookupError(f"{type(record).__name__} has no block {block}")


@dataclass(frozen=True, slots=True)
class SubagentMeta:
    description: str | None
    agent_type: str | None
    tool_use_id: str | None


def subagent_meta(body: bytes) -> SubagentMeta:
    obj = _json_object(body)
    return SubagentMeta(_str(obj.get("description")), _str(obj.get("agentType")), _str(obj.get("toolUseId")))


@dataclass(frozen=True, slots=True)
class DesktopSession:
    cli_session_id: SessionId
    title: str | None


def desktop_session(body: bytes) -> DesktopSession | None:
    """A desktop app local_*.json -> the CLI session it wraps and its title.
    None when the body is not JSON or names no CLI session."""
    obj = _json_object(body)
    sid = _str(obj.get("cliSessionId"))
    return DesktopSession(SessionId(sid), _str(obj.get("title"))) if sid else None


def cleanup_period_days(settings: bytes) -> int | None:
    """<claude>/settings.json -> how many days Claude Code keeps a transcript
    after its last write, when the user set it to a whole number of days, 1
    or more."""
    days = _json_object(settings).get("cleanupPeriodDays")
    return days if isinstance(days, int) and not isinstance(days, bool) and days >= 1 else None


def _typed_by_a_person(obj: dict) -> bool:
    origin = obj.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return not obj.get("isMeta") and not obj.get("isCompactSummary") and kind in (None, "human")


def _turn(message: object, text_kind: Kind, ts: Millis | None, cwd: str | None) -> Turn | Other:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        blocks = ((0, Block(text_kind, _text(content))),)
    elif isinstance(content, list):
        blocks = tuple((i, b) for i, raw in enumerate(content) if (b := _block(raw, text_kind)) is not None)
    else:
        blocks = ()
    if text_kind is Kind.PROMPT:
        blocks = _from_a_person(blocks)
    return Turn(ts, cwd, blocks) if blocks else Other(ts, cwd)


def _from_a_person(blocks: tuple[tuple[int, Block], ...]) -> tuple[tuple[int, Block], ...]:
    """A line that carries a tool result is tool output throughout,
    including any text Claude Code put next to it."""
    if any(b.kind is Kind.TOOL_OUTPUT for _, b in blocks):
        return tuple((i, replace(b, kind=Kind.TOOL_OUTPUT)) for i, b in blocks)
    return tuple((i, t) for i, b in blocks if (t := _typed(b) if b.kind is Kind.PROMPT else b) is not None)


_INTERRUPTED = "[Request interrupted by user"
_SHELL_OUTPUT = re.compile(r"\s*<(?:bash|local-command)-std(?:out|err)>")
_SHELL_INPUT = re.compile(r"\s*<bash-input>(.*?)</bash-input>", re.S)
_COMMAND = re.compile(r"\s*<command-(?:name|message)>")
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def _typed(block: Block) -> Block | None:
    text = block.text
    if text.lstrip().startswith(_INTERRUPTED):
        return None
    if _SHELL_OUTPUT.match(text):
        return replace(block, kind=Kind.TOOL_OUTPUT)
    if m := _SHELL_INPUT.match(text):
        return replace(block, text=f"! {m[1]}")
    if _COMMAND.match(text) and (name := _COMMAND_NAME.search(text)):
        args = _COMMAND_ARGS.search(text)
        return replace(block, text=" ".join(part for part in (name[1].strip(), args and args[1].strip()) if part))
    return block


def _block(raw: object, text_kind: Kind) -> Block | None:
    if not isinstance(raw, dict):
        return None
    match raw.get("type"):
        case "text" if isinstance(raw.get("text"), str):
            return Block(text_kind, _text(raw["text"]))
        case "tool_use":
            name = _str(raw.get("name")) or "?"
            args = _text(json.dumps(raw.get("input"), ensure_ascii=False, separators=(",", ":")))
            return Block(Kind.TOOL_CALL, f"{name} {args}", tool=name, tool_use_id=_str(raw.get("id")))
        case "tool_result":
            return Block(Kind.TOOL_OUTPUT, _text(_result_text(raw.get("content"))))
        case _:
            return None


def _title(value: object, custom: bool, ts: Millis | None) -> Title | None:
    return Title(_text(value), custom, ts) if isinstance(value, str) and value.strip() else None


def _observe(facts: Facts, ts: Millis | None, cwd: str | None) -> Facts:
    return replace(
        facts,
        cwd=facts.cwd if facts.cwd is not None else cwd,
        first_ts=ts if facts.first_ts is None or (ts is not None and ts < facts.first_ts) else facts.first_ts,
        last_ts=ts if facts.last_ts is None or (ts is not None and ts > facts.last_ts) else facts.last_ts,
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_END = datetime(9999, 12, 31, 10, tzinfo=UTC)
"""Year 10000 begins at UTC+14, the zone furthest ahead, 14 hours before
10000-01-01T00:00Z. A time before this has a local date in years 1969 to
9999 in every zone, so the CLI can print it."""


def _millis(value: object) -> Millis | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if not _EPOCH <= dt < _END:
        return None
    return Millis((dt - _EPOCH) // timedelta(milliseconds=1))


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif part.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _str(value: object) -> str | None:
    return _text(value) if isinstance(value, str) else None


_SURROGATE = re.compile("[\ud800-\udfff]")


def _text(value: str) -> str:
    """json.loads turns an escape such as "\\ud800" with no partner into a
    str that is not valid Unicode, and SQLite cannot store it."""
    try:
        value.encode()
    except UnicodeEncodeError:
        return _SURROGATE.sub("\ufffd", value)
    return value


def _json_object(body: bytes) -> dict:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except ValueError, RecursionError:
        return {}
    return obj if isinstance(obj, dict) else {}
