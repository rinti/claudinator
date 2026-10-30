from __future__ import annotations

import hmac
import html
import json
import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    phrase = HTTPStatus(status).phrase
    said = el(
        "main",
        {"class": "message"},
        el("h1", None, phrase),
        el("p", {"class": "banner"}, text),
        _back("./", "All sessions") if root else None,
    )
    return Response(status, HTML, _document(page(root, phrase, _top("") if root else None, said)))


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
    form, at = Form.read(query), now()
    today = datetime.fromtimestamp(at / 1000).date()
    with _reading(cfg, sweep=True) as (archive, swept):
        try:
            filters, terms = form.filters(at), form.terms()
            if terms:
                hits = archive.search(Query(terms, filters, form.tools, SEARCH_LIMIT))
                return Response(200, HTML, _document(search_page(root, hits, form, swept)))
            rows = archive.sessions(filters, None)
            return Response(200, HTML, _document(sessions_page(root, rows, form, swept, today)))
        except (ValueError, EmptyQuery) as e:
            return Response(400, HTML, _document(sessions_page(root, (), form, swept, today, str(e))))


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
    root: str, rows: Sequence[SessionSummary], form: Form, swept: SweepReport | None, today: date, error: str = ""
) -> Html:
    count = "No sessions" if error else _plural(len(rows), "session")
    days = groupby(rows, key=lambda s: None if s.last_ts is None else _local(s.last_ts).date())
    return page(
        root,
        "claudinator",
        _top(form.q),
        el(
            "main",
            {"class": "list"},
            _toolbar(form, count, _scope(form), clear=False),
            _banner(swept, error),
            *(_day_section(day, list(run), today) for day, run in days),
        ),
    )


def _day_section(day: date | None, rows: list[SessionSummary], today: date) -> Html:
    near = {today: "Today", today - timedelta(days=1): "Yesterday"}.get(day) if day else None
    head = el(
        "div",
        {"class": "day-head"},
        el("h2", None, "No date" if day is None else _day(day, year=day.year != today.year)),
        el("span", {"class": "tag"}, near) if near else None,
        el("span", {"class": "n"}, _plural(len(rows), "session")),
    )
    return el("section", {"class": "day"}, head, el("ol", {"class": "sessions"}, *map(_session_row, rows)))


def _session_row(s: SessionSummary) -> Html:
    when, where = _local(s.last_ts), human.short(s.cwd)
    gone = None if s.on_disk else el("span", {"class": "badge"}, "deleted")
    title = el("span", {"class": "title"}, el("a", {"class": "open", "href": href_transcript(s.id)}, *_title(s)), gone)
    if s.cwd:
        project = el(
            "a", {"class": "project", "href": "./?" + urlencode({"project": s.cwd}), "title": where}, *_where(s.cwd)
        )
    else:
        project = el("span", {"class": "project"}, *_where(s.cwd))
    return el(
        "li",
        None,
        el("time", None, f"{when:%H:%M}" if when else ""),
        title,
        project,
        _tally(s.prompts, "prompt"),
        _tally(s.subagents, "subagent") if s.subagents else el("span", None),
    )


COMMAND = re.compile(r"/[^\W\d][\w:.-]*(?=\s|$)")
"""A leading slash command, as Claude Code writes one into the prompt it
records: /clear or /pstack:poteto-mode, never a path like /Users/me."""


def _title(s: SessionSummary) -> tuple[Html | None, ...]:
    title = s.title or s.id[:8]
    if m := COMMAND.match(title):
        rest = title[m.end() :].strip()
        return el("span", {"class": "cmd"}, m[0]), el("span", {"class": "rest"}, rest) if rest else None
    if s.heading and title.startswith(f"# {s.heading}"):
        rest = title[2 + len(s.heading) :].strip()
        head = el("span", {"class": "head"}, s.heading)
        return el("span", {"class": "hash"}, "#"), head, el("span", {"class": "rest"}, rest) if rest else None
    return (el("span", {"class": "head"}, title),)


def _where(cwd: str | None) -> tuple[Html | None, Html]:
    parent, slash, leaf = human.short(cwd).rpartition("/")
    return el("span", {"class": "dir"}, parent + slash) if slash else None, el("span", {"class": "leaf"}, leaf)


def _tally(n: int, noun: str) -> Html:
    return el("span", {"class": "count"}, el("span", {"class": "n"}, f"{n:,}"), " ", _noun(n, noun))


def search_page(root: str, hits: Sequence[SessionHit], form: Form, swept: SweepReport | None) -> Html:
    count = _plural(len(hits), "session") + (", the best of more" if len(hits) == SEARCH_LIMIT else "")
    about = " ".join(part for part in (f"matching “{form.q}”", _scope(form)) if part)
    if hits:
        found = el("ol", {"class": "hits"}, *(_hit(hit, form.q) for hit in hits))
    else:
        tip = "Try fewer words, a prefix like deploy*, or turn on Include tools."
        found = el("div", {"class": "none"}, el("p", {"class": "big"}, "Nothing matches that."), el("p", None, tip))
    return page(
        root,
        f"{form.q} in claudinator",
        _top(form.q),
        el("main", {"class": "search"}, _toolbar(form, count, about, clear=True), _banner(swept, ""), found),
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
    found = ("best in " if hit.hits > 1 else "in ") + KINDS[best.kind]
    meta = el(
        "p",
        {"class": "meta"},
        el("span", None, f"{when:%a} {when.day} {when:%b %Y} · {when:%H:%M}") if when else None,
        el("span", None, *_where(s.cwd)),
        el("span", {"class": "tag"}, found + (f" · subagent {best.agent_id[:8]}" if best.agent_id else "")),
    )
    title = el("a", {"class": "open", "href": href_transcript(s.id, best.agent_id, at, q)}, s.title or s.id[:8])
    return el(
        "li",
        None,
        el(
            "p",
            {"class": "tally"},
            el("span", {"class": "n"}, f"{hit.hits:,}"),
            " ",
            el("span", None, _noun(hit.hits, "hit")),
        ),
        el(
            "div",
            {"class": "found"},
            el("h2", None, title),
            meta,
            None if at is None else el("p", {"class": "excerpt"}, marked(best.text, best.marks)),
        ),
    )


def _toolbar(form: Form, count: str, about: str, *, clear: bool) -> Html:
    said = el(
        "div",
        {"class": "said"},
        el("h1", None, count),
        el("span", {"class": "scope"}, about) if about else None,
        el("a", {"class": "clear", "href": _href_filters(form)}, "Clear") if clear else None,
    )
    tools = el("input", {"class": "switch", "type": "checkbox", "name": "tools", "form": "find", "checked": form.tools})
    return el(
        "div",
        {"class": "toolbar"},
        said,
        el(
            "div",
            {"class": "pills"},
            _pill("Project", "project", form.project, "any"),
            _pill("Since", "since", form.since, "7d"),
            _pill("Until", "until", form.until, "any"),
            el("label", {"class": "pill tools"}, tools, "Include tools"),
        ),
    )


def _pill(word: str, name: str, value: str, placeholder: str) -> Html:
    field = el(
        "input", {"name": name, "form": "find", "value": value, "placeholder": placeholder, "spellcheck": "false"}
    )
    return el("label", {"class": "pill"}, word, field)


def _scope(form: Form) -> str:
    said = (("in", form.project), ("since", form.since), ("until", form.until))
    return " ".join(f"{word} {value}" for word, value in said if value)


def _href_filters(form: Form) -> str:
    kept = {"project": form.project, "since": form.since, "until": form.until, "tools": "on" if form.tools else ""}
    query = urlencode({name: value for name, value in kept.items() if value})
    return "./" + (f"?{query}" if query else "")


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
    agent = None if aid is None else next((a for a in session.agents if a.id == aid), None)
    if aid is None:
        heading = s.title or s.id[:8]
    else:
        heading = (agent and agent.description) or f"Subagent {aid[:8]}"
    if q:
        back = _back("./?" + urlencode({"q": q}), f"Results for “{q}”")
    elif aid:
        back = _back(href_transcript(s.id), s.title or s.id[:8])
    else:
        back = _back("./", "All sessions")
    header = el("header", {"class": "session"}, el("h1", None, heading), _facts(s, main=aid is None))
    prompts = (step.key for step in steps if isinstance(step, Said) and step.kind is Kind.PROMPT)
    numbers = {key: n for n, key in enumerate(prompts, 1)}
    shown = [step for step in steps if hidden or not _folded(step)]
    toggle = _toggle(href_transcript(s.id, aid, at, q, not hidden), sum(map(_folded, steps)), hidden)
    aside = _aside(session, aid, agent, el("div", {"class": "actions"}, _download(s.id, aid), toggle))
    body = el("div", {"class": "steps"}, *_dated(s.id, aid, shown, at, terms, numbers))
    return page(
        root,
        heading,
        _top(q),
        el(
            "main",
            {"class": "transcript"},
            back,
            header,
            _banner(swept, ""),
            el("div", {"class": "columns"}, aside, body),
        ),
    )


def _back(href: str, label: str) -> Html:
    return el("a", {"class": "back", "href": href}, el("span", {"aria-hidden": "true"}, "←"), label)


def _facts(s: SessionSummary, *, main: bool) -> Html:
    first, last = _local(s.first_ts), _local(s.last_ts)
    span = duration = None
    if first and last:
        end = f"{last:%H:%M}" if last.date() == first.date() else _stamp(last)
        span, duration = f"{_stamp(first)} – {end}", _duration(last - first)
    return el(
        "p",
        {"class": "facts"},
        el("span", None, *_where(s.cwd)),
        el("span", None, span) if span else None,
        el("span", {"class": "faint"}, duration) if duration else None,
        el("span", None, _plural(s.prompts, "prompt")) if main else None,
        el("span", None, _plural(s.subagents, "subagent")) if main and s.subagents else None,
    )


def _stamp(when: datetime) -> str:
    return f"{when:%a} {when.day} {when:%b} · {when:%H:%M}"


def _duration(span: timedelta) -> str:
    hours, minutes = divmod(round(span.total_seconds() / 60), 60)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _aside(session: Session, aid: AgentId | None, agent: Subagent | None, actions: Html) -> Html:
    s = session.summary
    if aid is not None:
        about = [el("h2", None, "Subagent")]
        if agent and agent.agent_type:
            about.append(el("p", {"class": "kind"}, agent.agent_type))
        about.append(el("p", None, "In ", el("a", {"href": href_transcript(s.id)}, s.title or s.id[:8])))
        if agent and agent.tool_use_id:
            about.append(
                el(
                    "p",
                    None,
                    el("a", {"href": href_call(s.id, agent.parent, agent.tool_use_id)}, "See the call that started it"),
                )
            )
        return el("aside", None, el("section", None, *about, actions))
    command = el(
        "p",
        {"class": "command"},
        el("span", {"aria-hidden": "true"}, "$"),
        el("code", None, f"claudinator resume {s.id[:8]}"),
    )
    gone = None if s.on_disk else el("p", {"class": "gone"}, "Claude Code deleted its copy. Resuming puts it back.")
    resume = el("section", None, el("h2", None, "Resume"), command, gone, actions)
    if not session.agents:
        return el("aside", None, resume)
    agents = el(
        "section",
        None,
        el("h2", None, "Subagents ", el("span", {"class": "n"}, f"{len(session.agents):,}")),
        el("ul", {"class": "agents"}, *(_agent(s.id, a) for a in session.agents)),
    )
    return el("aside", None, resume, agents)


def _agent(sid: SessionId, a: Subagent) -> Html:
    return el(
        "li",
        None,
        el(
            "a",
            {"href": href_transcript(sid, a.id)},
            el("span", {"class": "name"}, a.description or f"Subagent {a.id[:8]}"),
            el("span", {"class": "id"}, a.id[:8]),
            el("span", {"class": "type"}, a.agent_type) if a.agent_type else None,
        ),
    )


def _download(sid: SessionId, aid: AgentId | None) -> Html:
    return el(
        "a",
        {"class": "button", "href": href_export(sid, aid)},
        "Download transcript",
        el("span", {"class": "faint"}, ".jsonl ↓"),
    )


def _toggle(href: str, folded: int, hidden: bool) -> Html | None:
    if not folded:
        return None
    state = el(
        "span", {"class": "switch", "role": "img", "aria-label": "shown" if hidden else "not shown", "data-on": hidden}
    )
    return el(
        "a",
        {"class": "button", "href": href},
        el("span", None, "Hidden lines ", el("span", {"class": "n"}, f"{folded:,}")),
        state,
    )


def _local(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000)


def _day(day: date, *, year: bool) -> str:
    return f"{day:%A} {day.day} {day:%B}" + (f" {day.year}" if year else "")


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


def _dated(
    sid: SessionId,
    aid: AgentId | None,
    steps: Sequence[Step],
    at: Key | None,
    terms: tuple[str, ...],
    numbers: Mapping[Key, int],
) -> Iterator[Html]:
    """Each minute's run, after a divider wherever the local day changes.
    Within a run, consecutive calls, outputs, and thoughts share one box."""
    seen = None
    for minute, run in _minutes(steps):
        if minute is not None and minute.date() != seen:
            seen = minute.date()
            yield el("h2", {"class": "day"}, _day(seen, year=True))
        parts: list[Html] = []
        for machine, group in groupby(run, key=lambda step: isinstance(step, Call | Output | Thought)):
            made = [_step(sid, aid, step, at, terms, numbers) for step in group]
            parts += [el("div", {"class": "box"}, *made)] if machine else made
        yield el("div", {"class": "minute"}, el("time", None, f"{minute:%H:%M}") if minute else None, *parts)


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


def _step(
    sid: SessionId, aid: AgentId | None, step: Step, at: Key | None, terms: tuple[str, ...], numbers: Mapping[Key, int]
) -> Html:
    raw_link = el("a", {"class": "raw", "href": href_raw(sid, aid, step.key.line_off)}, "raw")
    here = step.key == at
    match step:
        case Said(key, _, Kind.PROMPT, text):
            text = text.strip()
            command = COMMAND.match(text)
            said = el(
                "div",
                {"class": "text"},
                el("span", {"class": "cmd"}, _text(command[0], here, terms)) if command else None,
                *_prose(text[command.end() :].lstrip() if command else text, here, terms),
            )
            label = el("p", {"class": "label"}, f"Prompt {numbers[key]}")
            return el("div", {"class": "step prompt", "id": f"m{key}"}, label, said, raw_link)
        case Said(key, _, _, text):
            said = el("div", {"class": "text"}, *_prose(text.strip(), here, terms))
            return el("div", {"class": "step reply", "id": f"m{key}"}, said, raw_link)
        case Call():
            return _call(sid, aid, step, at, terms)
        case Output():
            first = (_plain(step).strip().splitlines() or [""])[0]
            body = el("div", {"class": "part"}, *_output_body(sid, aid, step, at, terms))
            return _row(
                step.key, "output error" if step.error else "output", "Output", first, _size(step), (body,), here
            )
        case Thought(key, _, text):
            first = (text.strip().splitlines() or [""])[0]
            body = el("div", {"class": "part"}, el("div", {"class": "text"}, *_prose(text, here, terms)), raw_link)
            return _row(key, "thought", "Thinking", first, el("span", {"class": "size"}, _lines(text)), (body,), here)
        case Image(key, _, shown):
            return el("div", {"class": "step image", "id": f"m{key}"}, _img(sid, aid, key, shown), raw_link)
        case Marker(key, Note(notice, text)):
            css = f"step note {notice.name.lower()}"
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
                {"class": "step note hidden", "id": f"m{key}"},
                el(
                    "summary",
                    None,
                    f"{_plural(len(lines), 'hidden line')}: {counts}",
                    el("span", {"class": "badge"}, "hidden"),
                ),
                el("ul", None, *(el("li", None, el("a", {"href": href_raw(sid, aid, o)}, name)) for o, name in lines)),
            )


def _row(
    key: Key, css: str, name: str, gist: str, end: Html | None, panel: Sequence[Html | None], opened: bool, *after: Html
) -> Html:
    """One row of a box: a chevron, a name, a gist, and at its end a size, an
    error, or a link to the subagent; opening it shows the panel."""
    summary = el("summary", None, el("b", None, name), " ", el("span", {"class": "gist"}, human.clip(gist, 160)), end)
    details = el("details", {"open": opened}, summary, el("div", {"class": "panel"}, *panel))
    return el("div", {"class": f"step {css}", "id": f"m{key}"}, details, *after)


def _call(sid: SessionId, aid: AgentId | None, call: Call, at: Key | None, terms: tuple[str, ...]) -> Html:
    out = call.result
    given = json.dumps(json.loads(call.input), indent=2, ensure_ascii=False)
    raw_link = el("a", {"class": "raw", "href": href_raw(sid, aid, call.key.line_off)}, "raw")
    panel = [el("div", {"class": "part"}, el("pre", {"class": "input"}, _text(given, call.key == at, terms)), raw_link)]
    if out is not None:
        css = "part output error" if out.error else "part output"
        panel.append(el("div", {"class": css, "id": f"m{out.key}"}, *_output_body(sid, aid, out, at, terms)))
    opened = at is not None and at in (call.key, out and out.key)
    if call.spawned:
        spawned = el(
            "a", {"class": "spawned", "href": href_transcript(sid, call.spawned.id)}, f"↳ {call.spawned.id[:8]}"
        )
        return _row(call.key, "call spawns", call.tool, _gist(call.input), None, panel, opened, spawned)
    css = "call error" if out is not None and out.error else "call"
    return _row(call.key, css, call.tool, _gist(call.input), _size(out), panel, opened)


def _gist(given: str) -> str:
    value = json.loads(given)
    fields = value.values() if isinstance(value, dict) else ()
    first = next((v for v in fields if isinstance(v, str) and v.strip()), None)
    return " ".join(first.split()) if first else given


def _streams(out: Output) -> tuple[tuple[str, bool], ...]:
    streams = printed(out.text)
    return ((out.text, False),) if streams is None else streams


def _plain(out: Output) -> str:
    return "\n".join(text for text, _ in _streams(out))


def _size(out: Output | None) -> Html:
    if out is not None and out.error:
        return el("span", {"class": "size error"}, "error")
    shown = "" if out is None else _plain(out)
    lines = [_lines(shown)] if shown.strip() else []
    images = [_plural(len(out.pictures), "image")] if out is not None and out.pictures else []
    return el("span", {"class": "size"}, " and ".join(lines + images) or "no output")


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


def _prose(s: str, here: bool, terms: tuple[str, ...]) -> tuple[Html, ...]:
    return (_text(s, here, terms),)


def _text(s: str, here: bool, terms: tuple[str, ...]) -> Html:
    return _mark(_cut(s), here, terms)


def _cut(s: str) -> str:
    if len(s) > INLINE_CHARS:
        return s[:INLINE_CHARS] + f"\n… cut at {human.size(INLINE_CHARS)}; the raw line has all of it"
    return s


def _mark(s: str, here: bool, terms: tuple[str, ...]) -> Html:
    return marked(s, marks(s, terms)) if here and terms else text(s)


def _long(s: str) -> bool:
    return len(s) > FOLD_CHARS or s.count("\n") >= FOLD_LINES


def _lines(s: str) -> str:
    return _plural(len(s.splitlines()) or 1, "line")


def _plural(n: int, noun: str) -> str:
    return f"{n:,} {_noun(n, noun)}"


def _noun(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def _top(q: str) -> Html:
    glass = el(
        "svg",
        {"class": "glass", "viewBox": "0 0 16 16", "aria-hidden": "true"},
        el("circle", {"cx": "7", "cy": "7", "r": "5"}),
        el("path", {"d": "M11 11l3.5 3.5"}),
    )
    field = el(
        "input",
        {
            "type": "search",
            "name": "q",
            "value": q,
            "aria-label": "Search",
            "placeholder": 'Search sessions, words, "a phrase", deploy*',
        },
    )
    find = el(
        "form",
        {"id": "find", "class": "find", "method": "get", "action": "./", "role": "search"},
        glass,
        field,
        el("button", None, "Search"),
    )
    return el(
        "header", {"class": "top"}, el("div", None, el("a", {"class": "wordmark", "href": "./"}, "claudinator"), find)
    )


def _banner(swept: SweepReport | None, error: str) -> Html | None:
    notes = [error] if error else []
    if swept is not None and (n := len(swept.errors)):
        notes.append(f"{_plural(n, 'error')} while archiving new bytes, the first: {swept.errors[0]}")
    if swept is not None and swept.busy:
        notes.append("Another sweep is writing; this shows what is already archived.")
    return el("p", {"class": "banner"}, " ".join(notes)) if notes else None


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
        el("meta", {"name": "color-scheme", "content": "light dark"}),
        el("base", {"href": root}) if root else None,
        el("link", {"rel": "stylesheet", "href": "web.css"}) if root else None,
        el("title", None, human.printable(title)),
    )
    return el("html", {"lang": "en"}, head, el("body", None, *body))


def _document(markup: Html) -> bytes:
    return ("<!doctype html>\n" + markup).encode(errors="replace")
