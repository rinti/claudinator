from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from io import BytesIO
from itertools import groupby
from operator import itemgetter
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from claudinator import human
from claudinator.archive import (
    Archive,
    ArchiveError,
    EmptyQuery,
    Filters,
    Query,
    Session,
    SessionHit,
    SessionSummary,
    Subagent,
    SweepReport,
    UnknownSession,
    now,
)
from claudinator.conversation import Call, Hidden, Image, Key, Marker, Output, Said, Step, Thought, build
from claudinator.layout import AGENT_ID, Config
from claudinator.record import AgentId, Kind, Millis, Note, Notice, Picture, SessionId, image, printed, task_summary
from claudinator.search_text import marks, searchable

PAGE_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'self';"
    " frame-ancestors 'none'"
)
FILE_POLICY = "default-src 'none'; sandbox"
RASTER_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
SEARCH_LIMIT = 50
FOLD_LINES, FOLD_CHARS = 3, 300
INLINE_CHARS = 1 << 20
HTML = "text/html; charset=utf-8"
STYLE = resources.files("claudinator").joinpath("web.css").read_bytes()
FONTS = {
    name: resources.files("claudinator").joinpath(f"fonts/{name}-Variable.woff2").read_bytes()
    for name in ("Geist", "GeistMono")
}


class Server(ThreadingHTTPServer):
    def __init__(self, cfg: Config, port: int) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.cfg = cfg
        self.root = f"/{secrets.token_urlsafe(32)}/"

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.root}"


def bind(cfg: Config, port: int = 0) -> Server:
    Archive.open(cfg, readonly=True).close()
    return Server(cfg, port)


def serve(server: Server) -> int:
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    content_type: str
    body: bytes
    policy: str = PAGE_POLICY
    headers: tuple[tuple[str, str], ...] = ()


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:
        self._send(self._answer())

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._send(_message(code, HTTPStatus(code).phrase))

    def log_message(self, format: str, *args: object) -> None:
        """Silent: a request line can hold search words and the token."""

    def _answer(self) -> Response:
        port = self.server.port
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}") + (("127.0.0.1", "localhost") if port == 80 else ())
        if self.headers.get("Host") not in hosts:
            return _message(403, "This server answers only to 127.0.0.1 and localhost.")
        if self.headers.get("Origin", f"http://{hosts[0]}") not in tuple(f"http://{host}" for host in hosts):
            return _message(403, "This server answers only its own pages.")
        url = urlsplit(self.path)
        if url.path == "/favicon.ico":
            return Response(HTTPStatus.NO_CONTENT, HTML, b"")
        root = self.server.root
        if not hmac.compare_digest(url.path[: len(root)].encode(), root.encode()):
            return _message(403, "Open the URL claudinator serve printed. Addresses from an earlier run no longer work.")
        path, query = url.path[len(root) - 1 :], dict(parse_qsl(url.query))
        for pattern, route in ROUTES:
            if m := pattern.fullmatch(path):
                try:
                    return route(self.server.cfg, root, query, m)
                except UnknownSession as e:
                    return _message(404, str(e), root)
                except (ArchiveError, sqlite3.Error, OSError) as e:
                    return _message(500, str(e), root)
        return _message(404, "Nothing is at this address.", root)

    def _send(self, response: Response) -> None:
        try:
            self.send_response(response.status)
            empty = response.status == HTTPStatus.NO_CONTENT
            for name, value in (
                ("Content-Type", response.content_type),
                *(() if empty else (("Content-Length", str(len(response.body))),)),
                ("Content-Security-Policy", response.policy),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
                ("Cache-Control", "no-store"),
                *response.headers,
            ):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)
        except ConnectionError:
            pass


def _message(status: int, text: str, root: str | None = None) -> Response:
    back = el("p", None, el("a", {"href": "./"}, "All sessions")) if root else None
    return Response(
        status, HTML, _document(page(root, HTTPStatus(status).phrase, el("p", {"class": "banner"}, text), back))
    )


@contextmanager
def _reading(cfg: Config, *, sweep: bool) -> Iterator[tuple[Archive, SweepReport | None]]:
    with Archive.open(cfg, readonly=True) as archive:
        swept = None
        if sweep:
            with Archive.open(cfg) as writer:
                swept = writer.sweep(wait=False)
        yield archive, swept


SID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
AID = AGENT_ID.pattern
OFFSET = r"\d{1,18}"

HOME = re.compile(r"/")


def home(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    form = Form.read(query)
    with _reading(cfg, sweep=True) as (archive, swept):
        try:
            filters, terms = form.filters(now()), form.terms()
            if terms:
                hits = archive.search(Query(terms, filters, form.tools, SEARCH_LIMIT))
                return Response(200, HTML, _document(search_page(root, hits, form, swept)))
            return Response(200, HTML, _document(sessions_page(root, archive.sessions(filters, None), form, swept)))
        except (ValueError, EmptyQuery) as e:
            return Response(400, HTML, _document(sessions_page(root, (), form, swept, str(e))))


TRANSCRIPT = re.compile(rf"/s/({SID})(?:/({AID}))?")


def href_transcript(
    sid: SessionId, aid: AgentId | None = None, at: Key | None = None, q: str = "", hidden: bool = False
) -> str:
    path = f"s/{sid}" + (f"/{aid}" if aid else "")
    landed = {} if at is None else {"at": str(at)} | ({"q": q} if q else {})
    query = landed | ({"hidden": "1"} if hidden else {})
    return path + (f"?{urlencode(query)}" if query else "") + ("" if at is None else f"#m{at}")


def href_call(sid: SessionId, parent: AgentId | None, tool_use_id: str) -> str:
    return f"s/{sid}" + (f"/{quote(parent, safe='')}" if parent else "") + f"?{urlencode({'call': tool_use_id})}"


def transcript(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    sid, aid = SessionId(m[1]), m[2] and AgentId(m[2])
    with _reading(cfg, sweep=True) as (archive, swept):
        session = archive.session(sid)
        steps = build(archive.lines(sid, aid), session.agents, aid)
    if "call" in query:
        for step in steps:
            if isinstance(step, Call) and step.tool_use_id == query["call"]:
                return Response(303, HTML, b"", headers=(("Location", root + href_transcript(sid, aid, step.key)),))
    at = re.fullmatch(rf"({OFFSET})\.(\d{{1,9}})", query.get("at", ""))
    landed = at and Key(int(at[1]), int(at[2]))
    body = transcript_page(root, session, aid, steps, landed, query.get("q", ""), query.get("hidden") == "1", swept)
    return Response(200, HTML, _document(body))


RAW = re.compile(rf"/raw/({SID})(?:/({AID}))?/({OFFSET})")


def href_raw(sid: SessionId, aid: AgentId | None, line_off: int) -> str:
    return f"raw/{sid}" + (f"/{aid}" if aid else "") + f"/{line_off}"


def raw(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    with _reading(cfg, sweep=False) as (archive, _):
        line = archive.line(SessionId(m[1]), m[2] and AgentId(m[2]), int(m[3]))
    if line is None:
        return _message(404, "No line starts there.", root)
    try:
        body = json.dumps(json.loads(line), indent=2, ensure_ascii=True).encode()
    except ValueError, RecursionError:
        body = human.printable(line.decode("utf-8", "replace")).encode("ascii", "backslashreplace")
    return Response(200, "text/plain; charset=utf-8", body, FILE_POLICY)


IMAGE = re.compile(rf"/image/({SID})(?:/({AID}))?/({OFFSET})\.(\d{{1,9}})(?:/(\d{{1,9}}))?")


def href_image(sid: SessionId, aid: AgentId | None, key: Key, part: int | None) -> str:
    return f"image/{sid}" + (f"/{aid}" if aid else "") + f"/{key}" + (f"/{part}" if part is not None else "")


def picture(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    with _reading(cfg, sweep=False) as (archive, _):
        line = archive.line(SessionId(m[1]), m[2] and AgentId(m[2]), int(m[3]))
    found = line and image(line, int(m[4]), m[5] and int(m[5]))
    if not found or found[0] not in RASTER_TYPES:
        return _message(404, "No image of a type this server shows is there.", root)
    return Response(200, found[0], found[1], FILE_POLICY)


EXPORT = re.compile(rf"/export/({SID})(?:/({AID}))?")


def href_export(sid: SessionId, aid: AgentId | None) -> str:
    return f"export/{sid}" + (f"/{aid}" if aid else "")


def export(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    sid, aid = SessionId(m[1]), m[2] and AgentId(m[2])
    out = BytesIO()
    with _reading(cfg, sweep=False) as (archive, _):
        archive.export(sid, out, aid)
    name = f"{sid}-{aid}.jsonl" if aid else f"{sid}.jsonl"
    disposition = (("Content-Disposition", f'attachment; filename="{name}"'),)
    return Response(200, "application/octet-stream", out.getvalue(), FILE_POLICY, disposition)


STYLESHEET = re.compile(r"/web\.css")


def stylesheet(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    return Response(200, "text/css; charset=utf-8", STYLE)


FONT = re.compile(r"/fonts/(Geist|GeistMono)-Variable\.woff2")


def font(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    return Response(200, "font/woff2", FONTS[m[1]])


ROUTES: tuple[tuple[re.Pattern[str], Callable[[Config, str, Mapping[str, str], re.Match[str]], Response]], ...] = (
    (HOME, home),
    (TRANSCRIPT, transcript),
    (RAW, raw),
    (IMAGE, picture),
    (EXPORT, export),
    (STYLESHEET, stylesheet),
    (FONT, font),
)


@dataclass(frozen=True, slots=True)
class Form:
    q: str = ""
    project: str = ""
    since: str = ""
    until: str = ""
    tools: bool = False

    @classmethod
    def read(cls, query: Mapping[str, str]) -> Form:
        fields = (query.get(name, "") for name in ("q", "project", "since", "until"))
        return cls(*fields, tools="tools" in query)

    def terms(self) -> tuple[str, ...]:
        return tuple(phrase or word for phrase, word in re.findall(r'"([^"]*)"|(\S+)', self.q))

    def filters(self, at: Millis) -> Filters:
        def when(name: str, text: str) -> Millis | None:
            try:
                return human.parse_when(text, at) if text else None
            except ValueError as e:
                raise ValueError(f"{name}: {e}") from None

        return Filters(self.project or None, when("since", self.since), when("until", self.until))


def sessions_page(
    root: str, rows: Sequence[SessionSummary], form: Form, swept: SweepReport | None, error: str = ""
) -> Html:
    return page(
        root,
        "claudinator",
        _top(form),
        _banner(swept, error),
        None if error else el("p", {"class": "count"}, _plural(len(rows), "session")),
        *_days(_session_days(rows)),
    )


def _session_days(rows: Sequence[SessionSummary]) -> Iterator[tuple[datetime | None, Html]]:
    for _, run in groupby(rows, key=lambda s: None if s.last_ts is None else _local(s.last_ts).date()):
        day = list(run)
        yield _local(day[0].last_ts), el("ol", {"class": "sessions"}, *map(_session_row, day))


def search_page(root: str, hits: Sequence[SessionHit], form: Form, swept: SweepReport | None) -> Html:
    found = _plural(len(hits), "session") + (" or more" if len(hits) == SEARCH_LIMIT else "")
    return page(
        root,
        f"{form.q} in claudinator",
        _top(form),
        _banner(swept, ""),
        el("p", {"class": "count"}, found),
        el("ol", {"class": "hits"}, *(_hit(hit, form.q) for hit in hits)),
    )


def transcript_page(
    root: str,
    session: Session,
    aid: AgentId | None,
    steps: Sequence[Step],
    at: Key | None,
    q: str,
    hidden: bool,
    swept: SweepReport | None,
) -> Html:
    s = session.summary
    terms = searchable(Form(q=q).terms())
    folded = sum(map(_folded, steps))
    toggle = el(
        "a",
        {"href": href_transcript(s.id, aid, at, q, not hidden)},
        "Hide hidden lines" if hidden else f"Show {_plural(folded, 'hidden line')}",
    )
    links = el(
        "p",
        {"class": "links"},
        el("a", {"href": href_export(s.id, aid)}, "Download the transcript"),
        *((" ", toggle) if folded else ()),
    )
    span = el("p", None, _span(s))
    agents = None
    if aid is None:
        heading, facts = s.title or s.id[:8], (span, _resume(s))
        if session.agents:
            subagents = el("ul", None, *(_agent(s.id, a) for a in session.agents))
            agents = el("section", {"class": "agents"}, el("h2", None, "Subagents"), subagents)
    else:
        agent = next((a for a in session.agents if a.id == aid), None)
        heading, facts = (agent and agent.description) or f"Subagent {aid[:8]}", (_subagent(s, aid, agent), span)
    header = el(
        "header", {"class": "session"}, el("a", {"href": "./"}, "All sessions"), el("h1", None, heading), *facts, links
    )
    shown = [step for step in steps if hidden or not _folded(step)]
    minutes = (
        (
            minute,
            el(
                "div",
                {"class": "minute"},
                el("time", None, f"{minute:%H:%M}") if minute else None,
                *(_step(s.id, aid, step, at, terms) for step in run),
            ),
        )
        for minute, run in _minutes(shown)
    )
    body = el("main", None, *_days(minutes))
    return page(root, heading, header, _banner(swept, ""), agents, body)


def _span(s: SessionSummary) -> str:
    where = f"In {human.short(s.cwd)}" if s.cwd else "In an unknown directory"
    first, last = _local(s.first_ts), _local(s.last_ts)
    if first is None or last is None:
        return where + "."
    end = f"{last:%H:%M}" if last.date() == first.date() else _short_date(last)
    return f"{where}, {_short_date(first)} to {end}."


def _resume(s: SessionSummary) -> Html:
    counts = _plural(s.prompts, "prompt") + (f" and {_plural(s.subagents, 'subagent')}" if s.subagents else "")
    deleted = "" if s.on_disk else " Claude Code has deleted its copy, and resuming puts it back."
    return el("p", None, f"{counts}.{deleted} Resume it with ", el("code", None, f"claudinator resume {s.id[:8]}"))


def _subagent(s: SessionSummary, aid: AgentId, agent: Subagent | None) -> Html:
    kind = f", type {agent.agent_type}," if agent and agent.agent_type else ""
    said = (f"Subagent {aid[:8]}{kind} in ", el("a", {"href": href_transcript(s.id)}, s.title or s.id[:8]), ".")
    if agent and agent.tool_use_id:
        call = el("a", {"href": href_call(s.id, agent.parent, agent.tool_use_id)}, "See the call that started it")
        said += (" ", call, ".")
    return el("p", None, *said)


def _agent(sid: SessionId, a: Subagent) -> Html:
    link = el("a", {"href": href_transcript(sid, a.id)}, a.description or f"Subagent {a.id[:8]}")
    return el(
        "li", None, el("span", {"class": "id"}, a.id[:8]), link, el("span", {"class": "type"}, a.agent_type or "")
    )


def _local(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000)


def _short_date(when: datetime) -> str:
    return f"{when.day} {when:%b %H:%M}"


def _minutes(steps: Sequence[Step]) -> Iterator[tuple[datetime | None, list[Step]]]:
    """Runs of steps by the local minute they happened in. A marker has no
    time, so it joins the run before it."""

    def stamped() -> Iterator[tuple[datetime | None, Step]]:
        minute = None
        for step in steps:
            if not isinstance(step, Marker) and (when := _local(step.ts)) is not None:
                minute = when.replace(second=0, microsecond=0)
            yield minute, step

    for minute, run in groupby(stamped(), key=itemgetter(0)):
        yield minute, [step for _, step in run]


def _days(dated: Iterable[tuple[datetime | None, Html]]) -> Iterator[Html]:
    """Each item, after a heading wherever the local day changes. The first
    heading names the year, and so does one where the year turns."""
    seen = None
    for when, item in dated:
        if when is not None and when.date() != seen:
            year = f" {when.year}" if seen is None or when.year != seen.year else ""
            yield el("h2", {"class": "day"}, f"{when:%A} {when.day} {when:%B}{year}")
            seen = when.date()
        yield item


def _folded(step: Step) -> int:
    match step:
        case Marker(shows=Hidden(lines)):
            return len(lines)
    return 0


NOTICES = {
    Notice.QUEUED: "You queued a prompt while Claude worked",
    Notice.COMPACTED: "The conversation was compacted",
    Notice.INTERRUPTED: "You interrupted Claude",
    Notice.API_ERROR: "The API answered with an error",
    Notice.NOTIFICATION: "A notification came in",
}
STOCK = frozenset({Notice.COMPACTED, Notice.INTERRUPTED})
"""Notices whose short text is Claude Code's wording of what the sentence says."""


def _step(sid: SessionId, aid: AgentId | None, step: Step, at: Key | None, terms: tuple[str, ...]) -> Html:
    raw_link = el("a", {"class": "raw", "href": href_raw(sid, aid, step.key.line_off)}, "raw")
    here = step.key == at
    match step:
        case Said(key, _, kind, text):
            said = el("div", {"class": "text"}, _text(text, here, terms))
            return el("div", {"class": f"step {kind.value}", "id": f"m{key}"}, said, raw_link)
        case Call():
            return _call(sid, aid, step, at, terms, raw_link)
        case Output():
            css = "step output error" if step.error else "step output"
            body = _output_body(sid, aid, step, at, terms)
            if not _folds(step):
                return el("div", {"class": css, "id": f"m{step.key}"}, *body)
            first = (_plain(step).splitlines() or [""])[0]
            summary = el(
                "summary",
                None,
                el("span", {"class": "gist"}, human.clip(first, 120)),
                " ",
                el("span", {"class": "size"}, _size(step)),
            )
            return el("details", {"class": css, "id": f"m{step.key}", "open": here}, summary, *body)
        case Thought(key, _, text):
            return el(
                "details",
                {"class": "step thought", "id": f"m{key}", "open": here},
                el("summary", None, f"Thinking, {_count(text)}"),
                el("div", {"class": "text"}, _text(text, here, terms)),
                raw_link,
            )
        case Image(key, _, shown):
            return el("div", {"class": "step image", "id": f"m{key}"}, _img(sid, aid, key, shown), raw_link)
        case Marker(key, Note(notice, text)):
            css = f"step notice {notice.name.lower()}"
            gist = task_summary(text)
            quoted = notice not in STOCK and text.strip()
            if _long(text) or gist is not None:
                first = gist or (text.strip().splitlines() or [""])[0]
                said = NOTICES[notice] + (f": {human.clip(first, 120)}" if quoted else "")
                return el(
                    "details",
                    {"class": css, "id": f"m{key}", "open": here},
                    el("summary", None, said),
                    el("pre", None, text),
                    raw_link,
                )
            shown = el("span", {"class": "text"}, text) if quoted else None
            return el(
                "div", {"class": css, "id": f"m{key}"}, NOTICES[notice] + (": " if shown else "."), shown, raw_link
            )
        case Marker(key, Hidden(lines)):
            counts = ", ".join(f"{n} {name}" for name, n in Counter(name for _, name in lines).most_common())
            return el(
                "details",
                {"class": "step hidden", "id": f"m{key}"},
                el("summary", None, f"{_plural(len(lines), 'hidden line')}: {counts}"),
                el("ul", None, *(el("li", None, el("a", {"href": href_raw(sid, aid, o)}, name)) for o, name in lines)),
            )


def _call(
    sid: SessionId, aid: AgentId | None, call: Call, at: Key | None, terms: tuple[str, ...], raw_link: Html
) -> Html:
    """One row: the tool, its input's gist, and its output's size. A short
    output shows below the row, and opening the row shows the input and any other."""
    out = call.result
    below = out is not None and not _folds(out) and bool(_plain(out).strip())
    output = None
    if out is not None:
        css = "output error" if out.error else "output"
        output = el("div", {"class": css, "id": f"m{out.key}"}, *_output_body(sid, aid, out, at, terms))
    size = () if call.spawned else (" ", el("span", {"class": "size"}, _size(out)))
    summary = el(
        "summary", None, el("b", None, call.tool), " ", el("span", {"class": "gist"}, _gist(call.input)), *size
    )
    given = el(
        "pre", None, _text(json.dumps(json.loads(call.input), indent=2, ensure_ascii=False), call.key == at, terms)
    )
    opened = call.key == at or (out is not None and not below and out.key == at)
    details = el("details", {"open": opened}, summary, given, None if below else output)
    spawned = (
        el("a", {"class": "spawned", "href": href_transcript(sid, call.spawned.id)}, f"subagent {call.spawned.id[:8]}")
        if call.spawned
        else None
    )
    css = "step call error" if out is not None and out.error else "step call"
    return el("div", {"class": css, "id": f"m{call.key}"}, details, spawned, output if below else None, raw_link)


def _gist(given: str) -> str:
    value = json.loads(given)
    fields = value.values() if isinstance(value, dict) else ()
    first = next((v for v in fields if isinstance(v, str) and v.strip()), None)
    return human.clip(" ".join(first.split()) if first else given, 160)


def _streams(out: Output) -> tuple[tuple[str, bool], ...]:
    streams = printed(out.text)
    return ((out.text, False),) if streams is None else streams


def _plain(out: Output) -> str:
    return "\n".join(text for text, _ in _streams(out))


def _folds(out: Output) -> bool:
    return bool(out.pictures) or _long(_plain(out))


def _size(out: Output | None) -> str:
    if out is not None and out.error:
        return "error"
    shown = "" if out is None else _plain(out)
    lines = [_count(shown)] if shown.strip() else []
    images = [_plural(len(out.pictures), "image")] if out is not None and out.pictures else []
    return " and ".join(lines + images) or "no output"


def _output_body(
    sid: SessionId, aid: AgentId | None, out: Output, at: Key | None, terms: tuple[str, ...]
) -> tuple[Html, ...]:
    here = out.key == at
    return (
        *(_stream(text, stderr, here, terms) for text, stderr in _streams(out)),
        *(_img(sid, aid, out.key, p) for p in out.pictures),
        el("a", {"class": "raw", "href": href_raw(sid, aid, out.key.line_off)}, "raw"),
    )


def _stream(text: str, stderr: bool, here: bool, terms: tuple[str, ...]) -> Html:
    shown = el("pre", None, _text(text, here, terms))
    return el("div", {"class": "stderr"}, el("span", {"class": "label"}, "stderr"), shown) if stderr else shown


def _img(sid: SessionId, aid: AgentId | None, key: Key, shown: Picture) -> Html:
    about = f"image, {shown.media_type or 'no type'}, {human.size(shown.size)}"
    return el("img", {"loading": "lazy", "alt": about, "src": href_image(sid, aid, key, shown.part)})


def _text(s: str, here: bool, terms: tuple[str, ...]) -> Html:
    if len(s) > INLINE_CHARS:
        s = s[:INLINE_CHARS] + f"\n… cut at {human.size(INLINE_CHARS)}; the raw line has all of it"
    return marked(s, marks(s, terms)) if here and terms else text(s)


def _long(s: str) -> bool:
    return len(s) > FOLD_CHARS or s.count("\n") >= FOLD_LINES


def _count(s: str) -> str:
    return _plural(len(s.splitlines()) or 1, "line")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _top(form: Form) -> Html:
    return el(
        "header",
        {"class": "top"},
        el("h1", None, el("a", {"href": "./"}, "claudinator")),
        el(
            "form",
            {"method": "get", "action": "./", "class": "find"},
            el("input", {"type": "search", "name": "q", "value": form.q, "placeholder": 'words, "a phrase", deploy*'}),
            el("input", {"name": "project", "value": form.project, "placeholder": "project"}),
            el("input", {"name": "since", "value": form.since, "placeholder": "since: 2026-10-01, 7d"}),
            el("input", {"name": "until", "value": form.until, "placeholder": "until"}),
            el(
                "label",
                None,
                el("input", {"type": "checkbox", "name": "tools", "checked": form.tools}),
                " include tools",
            ),
            el("button", None, "Search"),
        ),
    )


def _banner(swept: SweepReport | None, error: str) -> Html | None:
    notes = [error] if error else []
    if swept is not None and (n := len(swept.errors)):
        notes.append(f"{_plural(n, 'error')} while archiving new bytes, the first: {swept.errors[0]}")
    if swept is not None and swept.busy:
        notes.append("Another sweep is writing; this shows what is already archived.")
    return el("p", {"class": "banner"}, " ".join(notes)) if notes else None


def _session_row(s: SessionSummary) -> Html:
    when, where = _local(s.last_ts), human.short(s.cwd)
    title = el("a", {"href": href_transcript(s.id)}, human.clip(s.title or s.id[:8], 120))
    if s.cwd:
        project = el("a", {"class": "project", "href": "./?" + urlencode({"project": s.cwd}), "title": where}, where)
    else:
        project = el("span", {"class": "project"}, where)
    return el(
        "li",
        None,
        el("time", None, f"{when:%H:%M}" if when else ""),
        el("span", {"class": "title"}, title, None if s.on_disk else el("span", {"class": "gone"}, " gone")),
        project,
        el("span", {"class": "count"}, _plural(s.prompts, "prompt")),
        el("span", {"class": "count"}, _plural(s.subagents, "subagent") if s.subagents else ""),
    )


KINDS = {
    Kind.PROMPT: "a prompt",
    Kind.REPLY: "a reply",
    Kind.TOOL_CALL: "a tool call",
    Kind.TOOL_OUTPUT: "a tool's output",
    Kind.TITLE: "the title",
}


def _hit(hit: SessionHit, q: str) -> Html:
    s, best = hit.session, hit.best
    at = None if best.kind is Kind.TITLE else Key(best.line_off, best.block)
    when = _local(s.last_ts)
    found = KINDS[best.kind] + (f" in subagent {best.agent_id[:8]}" if best.agent_id else "")
    count = f"{_plural(hit.hits, 'hit')}, best in {found}" if hit.hits > 1 else f"1 hit, in {found}"
    about = (f"{when:%H:%M} on {when.day} {when:%b %Y} " if when else "") + f"in {human.short(s.cwd)}, {count}"
    return el(
        "li",
        None,
        el("a", {"href": href_transcript(s.id)}, s.title or s.id[:8]),
        el("p", None, about),
        el(
            "a",
            {"class": "excerpt", "href": href_transcript(s.id, best.agent_id, at, q)},
            marked(best.text, best.marks),
        ),
    )


class Html(str):
    pass


_VOID = frozenset({"meta", "base", "link", "img", "input"})


def text(s: str) -> Html:
    out, at = [], 0
    for m in human.HIDDEN.finditer(s):
        out += (html.escape(s[at : m.start()]), f'<span class="esc">{human.escape(m[0])}</span>')
        at = m.end()
    out.append(html.escape(s[at:]))
    return Html("".join(out))


def marked(s: str, spans: Sequence[tuple[int, int]]) -> Html:
    out, at = [], 0
    for start, end in spans:
        out += (text(s[at:start]), el("mark", None, s[start:end]))
        at = end
    out.append(text(s[at:]))
    return Html("".join(out))


def el(tag: str, attrs: Mapping[str, str | bool | None] | None = None, *children: Html | str | None) -> Html:
    out = [f"<{tag}"]
    for name, value in (attrs or {}).items():
        if value is True:
            out.append(f" {name}")
        elif value:
            out.append(f' {name}="{html.escape(human.printable(value))}"')
    out.append(">")
    if tag not in _VOID:
        out += (child if isinstance(child, Html) else text(child) for child in children if child is not None)
        out.append(f"</{tag}>")
    return Html("".join(out))


def page(root: str | None, title: str, *body: Html | None) -> Html:
    """Every link on a page is relative to root, its <base>. A page that
    answers a request without the prefix gets None, so it cannot reveal it."""
    head = el(
        "head",
        None,
        el("meta", {"charset": "utf-8"}),
        el("meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}),
        el("base", {"href": root}) if root else None,
        el("link", {"rel": "stylesheet", "href": "web.css"}) if root else None,
        el("title", None, human.printable(title)),
    )
    return el("html", {"lang": "en"}, head, el("body", None, *body))


def _document(markup: Html) -> bytes:
    return ("<!doctype html>\n" + markup).encode(errors="replace")
