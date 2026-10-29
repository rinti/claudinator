"""One JSONL line of a Claude Code transcript, as a Record, and the projection fold.

Pure: no I/O, no sqlite, no clock. This is the drift boundary. It is the only
module that reads Claude Code field names, transcript lines and side files
alike.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import NewType

PROJECTION_VERSION = 1
"""Bump when parse() or project() would produce different rows for the same
bytes, or when the projection section of schema.sql changes.

tests/test_projection_golden.py projects a fixture corpus of real (redacted)
lines and compares a digest of the rows with GOLDEN[PROJECTION_VERSION], so a
forgotten bump fails the suite instead of silently mixing projections."""

SessionId = NewType("SessionId", str)  # transcript file stem, a UUID
AgentId = NewType("AgentId", str)  # subagent file stem without "agent-"
Millis = NewType("Millis", int)  # epoch milliseconds, UTC


class Kind(enum.StrEnum):
    """What a searchable unit is. The single list; schema.sql stores .value
    and has no CHECK duplicating it."""

    PROMPT = "prompt"  # human-typed (main) or parent-written (subagent) user text
    REPLY = "reply"  # assistant text block
    TOOL_CALL = "tool_call"  # assistant tool_use: name + compact JSON input
    TOOL_OUTPUT = "tool_output"  # user tool_result content text
    TITLE = "title"  # ai-title or custom-title, one entry per change


CONVERSATION: frozenset[Kind] = frozenset({Kind.PROMPT, Kind.REPLY, Kind.TITLE})
TOOLS: frozenset[Kind] = frozenset({Kind.TOOL_CALL, Kind.TOOL_OUTPUT})


@dataclass(frozen=True, slots=True)
class Block:
    """One displayable, indexable content block of a user or assistant line."""

    kind: Kind  # never TITLE
    text: str
    tool: str | None = None  # TOOL_CALL: tool name
    tool_use_id: str | None = None  # TOOL_CALL: links an Agent call to its subagent meta doc


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
    custom: bool  # custom-title (/rename) outranks ai-title


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
    """Not a JSON object: a torn line from a crash, a settled partial tail, or
    the continuation that a later append glued onto that tail."""


Record = Turn | Title | Other | Unparsed


@dataclass(frozen=True, slots=True)
class Facts:
    """Folded per-transcript facts; one transcript row. Owner: project()."""

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
    Unparsed, and the raw bytes stay in the archive for a later
    PROJECTION_VERSION to reinterpret."""
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
            return _title(obj.get("aiTitle"), False) or Other(ts, cwd)
        case "custom-title":
            return _title(obj.get("customTitle"), True) or Other(ts, cwd)
        case "assistant":
            return _turn(obj.get("message"), Kind.REPLY, ts, cwd)
        case "user" if _typed_by_a_person(obj):
            return _turn(obj.get("message"), Kind.PROMPT, ts, cwd)
        case _:
            return Other(ts, cwd)


def project(facts: Facts, record: Record) -> tuple[Facts, list[EntryDraft]]:
    """Fold one record into its transcript's facts and emit searchable units.

    Pure and deterministic. Incremental ingest and rebuild both call it on the
    same records in the same order, so the two paths produce identical rows.
    The text for each EntryDraft is re-derived by the caller with text_of()."""
    match record:
        case Unparsed():
            return replace(facts, unparsed=facts.unparsed + 1), []
        case Other(ts, cwd):
            return _observe(facts, ts, cwd), []
        case Turn(ts, cwd, blocks):
            drafts = [EntryDraft(i, b.kind, ts) for i, b in blocks if b.text.strip()]
            return _observe(facts, ts, cwd), drafts
        case Title(text, custom=True):
            if text == facts.custom_title:
                return facts, []
            return replace(facts, custom_title=text), [EntryDraft(0, Kind.TITLE, facts.last_ts)]
        case Title(text, custom=False):
            if text == facts.ai_title:
                return facts, []
            return replace(facts, ai_title=text), [EntryDraft(0, Kind.TITLE, facts.last_ts)]


def text_of(record: Record, block: int) -> str:
    """The indexed and displayed text of entry `block` of this record.

    Used for the FTS insert and for re-rendering a search hit from raw bytes,
    so the indexed text and the shown text cannot drift apart."""
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


def subagent_meta(body: str) -> SubagentMeta:
    """<sid>/subagents/agent-<aid>.meta.json -> what show and search need."""
    obj = _json_object(body)
    return SubagentMeta(_str(obj.get("description")), _str(obj.get("agentType")), _str(obj.get("toolUseId")))


@dataclass(frozen=True, slots=True)
class DesktopSession:
    cli_session_id: SessionId
    title: str | None


def desktop_session(body: str) -> DesktopSession | None:
    """A desktop app local_*.json -> the CLI session it wraps and its title.
    None when the body is not JSON or names no CLI session."""
    obj = _json_object(body)
    sid = _str(obj.get("cliSessionId"))
    return DesktopSession(SessionId(sid), _str(obj.get("title"))) if sid else None


def _typed_by_a_person(obj: dict) -> bool:
    origin = obj.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return not obj.get("isMeta") and not obj.get("isCompactSummary") and kind in (None, "human")


def _turn(message: object, text_kind: Kind, ts: Millis | None, cwd: str | None) -> Turn | Other:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        blocks = ((0, Block(text_kind, content)),)
    elif isinstance(content, list):
        blocks = tuple((i, b) for i, raw in enumerate(content) if (b := _block(raw, text_kind)) is not None)
    else:
        blocks = ()
    return Turn(ts, cwd, blocks) if blocks else Other(ts, cwd)


def _block(raw: object, text_kind: Kind) -> Block | None:
    if not isinstance(raw, dict):
        return None
    match raw.get("type"):
        case "text" if isinstance(raw.get("text"), str):
            return Block(text_kind, raw["text"])
        case "tool_use":
            name = _str(raw.get("name")) or "?"
            args = json.dumps(raw.get("input"), ensure_ascii=False, separators=(",", ":"))
            return Block(Kind.TOOL_CALL, f"{name} {args}", tool=name, tool_use_id=_str(raw.get("id")))
        case "tool_result":
            return Block(Kind.TOOL_OUTPUT, _result_text(raw.get("content")))
        case _:
            return None


def _title(value: object, custom: bool) -> Title | None:
    return Title(value, custom) if isinstance(value, str) and value.strip() else None


def _observe(facts: Facts, ts: Millis | None, cwd: str | None) -> Facts:
    if ts is None and (cwd is None or facts.cwd is not None):
        return facts
    return replace(
        facts,
        cwd=facts.cwd if facts.cwd is not None else cwd,
        first_ts=ts if facts.first_ts is None or (ts is not None and ts < facts.first_ts) else facts.first_ts,
        last_ts=ts if facts.last_ts is None or (ts is not None and ts > facts.last_ts) else facts.last_ts,
    )


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _millis(value: object) -> Millis | None:
    """ISO 8601 ('2026-10-13T14:48:22.241Z') -> epoch ms. None on anything else."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return Millis((dt - _EPOCH) // timedelta(milliseconds=1))


def _result_text(content: object) -> str:
    """tool_result content (str, or a list of text/image parts) -> text.
    Images become "[image]". toolUseResult duplicates this and is ignored."""
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
    return value if isinstance(value, str) else None


def _json_object(body: str) -> dict:
    try:
        obj = json.loads(body)
    except ValueError, RecursionError:
        return {}
    return obj if isinstance(obj, dict) else {}
