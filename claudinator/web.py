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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from io import BytesIO
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
from claudinator.record import AgentId, Kind, Millis, Note, Picture, SessionId, image, printed, task_summary
from claudinator.search_text import marks, searchable

PAGE_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'"
)
FILE_POLICY = "default-src 'none'; sandbox"
RASTER_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
SEARCH_LIMIT = 50
FOLD_LINES, FOLD_CHARS = 3, 300
INLINE_CHARS = 1 << 20
HTML = "text/html; charset=utf-8"
STYLE = resources.files("claudinator").joinpath("web.css").read_bytes()


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
        if not _same(url.path[: len(root)], root):
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
        return _message(404, "Nothing is here.", root)

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


def _same(given: str, root: str) -> bool:
    return hmac.compare_digest(given.encode(), root.encode())


def _message(status: int, text: str, root: str | None = None) -> Response:
    return Response(status, HTML, _document(page(root, HTTPStatus(status).phrase, el("p", {"class": "banner"}, text))))


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


ROUTES: tuple[tuple[re.Pattern[str], Callable[[Config, str, Mapping[str, str], re.Match[str]], Response]], ...] = (
    (HOME, home),
    (TRANSCRIPT, transcript),
    (RAW, raw),
    (IMAGE, picture),
    (EXPORT, export),
    (STYLESHEET, stylesheet),
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
        None if error else el("p", {"class": "muted"}, _plural(len(rows), "session")),
        el("table", {"class": "sessions"}, *(_session_row(s) for s in rows)),
    )


def search_page(root: str, hits: Sequence[SessionHit], form: Form, swept: SweepReport | None) -> Html:
    found = _plural(len(hits), "session") + (" or more" if len(hits) == SEARCH_LIMIT else "")
    return page(
        root,
        f"{form.q} · claudinator",
        _top(form),
        _banner(swept, ""),
        el("p", {"class": "muted"}, found),
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
    title = s.title or s.id[:8]
    agent = next((a for a in session.agents if a.id == aid), None)
    where = "on disk" if s.on_disk else "deleted by Claude Code, kept in the archive"
    facts = (
        f"{human.short(s.cwd)} · {human.when(s.first_ts)} to {human.when(s.last_ts)}"
        f" · {_plural(s.prompts, 'prompt')} · {where} · "
    )
    if agent is None:
        about = el("ul", {"class": "agents"}, *(_agent(s.id, a) for a in session.agents)) if session.agents else None
    else:
        about = el(
            "p",
            {"class": "agent"},
            f"subagent {agent.id[:8]} · {agent.agent_type or '?'} · {agent.description or ''} · ",
            el("a", {"href": href_call(s.id, agent.parent, agent.tool_use_id)}, "the call that started it")
            if agent.tool_use_id
            else el("a", {"href": href_transcript(s.id)}, "main transcript"),
        )
    header = el(
        "header",
        {"class": "session"},
        el("a", {"href": "./"}, "all sessions"),
        el("h1", None, title + (f" · subagent {aid[:8]}" if aid else "")),
        el(
            "p",
            {"class": "muted"},
            facts,
            el("code", None, f"claudinator resume {s.id[:8]}"),
            " · ",
            el("a", {"href": href_export(s.id, aid)}, "download .jsonl"),
            el(
                "span",
                None,
                f" · {_plural(folded, 'hidden line')} · ",
                el("a", {"href": href_transcript(s.id, aid, at, q, not hidden)}, "hide" if hidden else "show"),
            )
            if (folded := sum(map(_folded, steps)))
            else None,
        ),
        about,
    )
    body, speaker = [], None
    for step in steps:
        if _folded(step) and not hidden:
            continue
        who = _speaker(step)
        if who and who != speaker:
            body.append(el("h2", {"class": f"speaker {who}"}, f"{human.when(step.ts)}  {who}"))
            speaker = who
        body.append(_step(s.id, aid, step, at, terms))
    return page(root, title, header, _banner(swept, ""), el("main", None, *body))


def _agent(sid: SessionId, a: Subagent) -> Html:
    link = el("a", {"href": href_transcript(sid, a.id)}, a.id[:8])
    return el("li", None, link, f" {a.agent_type or ''} ", a.description or "")


def _folded(step: Step) -> int:
    match step:
        case Marker(shows=Hidden(lines)):
            return len(lines)
    return 0


def _speaker(step: Step) -> str | None:
    match step:
        case Said(kind=Kind.PROMPT) | Image():
            return "you"
        case Said() | Call() | Output() | Thought():
            return "claude"
        case Marker():
            return None


def _step(sid: SessionId, aid: AgentId | None, step: Step, at: Key | None, terms: tuple[str, ...]) -> Html:
    raw_link = el("a", {"class": "raw", "href": href_raw(sid, aid, step.key.line_off)}, "raw")
    here = step.key == at
    match step:
        case Said(key, _, kind, text):
            who = "you" if kind is Kind.PROMPT else "claude"
            said = el("div", {"class": "text"}, _text(text, here, terms))
            return el("div", {"class": f"step said {who}", "id": f"m{key}"}, said, raw_link)
        case Call(key, _, tool, _, given, _, spawned, result):
            subagent = None
            if spawned is not None:
                about = f"subagent {spawned.id[:8]} · {spawned.agent_type or '?'} · {spawned.description or ''}"
                subagent = el("a", {"class": "spawned", "href": href_transcript(sid, spawned.id)}, about)
            return el(
                "section",
                {"class": "step call", "id": f"m{key}"},
                el(
                    "details",
                    {"class": "input", "open": here},
                    el("summary", None, el("b", None, tool), " ", _gist(given)),
                    el("pre", None, _text(json.dumps(json.loads(given), indent=2, ensure_ascii=False), here, terms)),
                ),
                subagent,
                _output(sid, aid, result, at, terms) if result else el("p", {"class": "label"}, "no output"),
                raw_link,
            )
        case Output():
            return _output(sid, aid, step, at, terms, "step ")
        case Thought(key, _, text):
            return el(
                "details",
                {"class": "step thought", "id": f"m{key}", "open": here},
                el("summary", None, f"thinking · {_count(text)}"),
                el("pre", None, _text(text, here, terms)),
                raw_link,
            )
        case Image(key, _, shown):
            return el("div", {"class": "step image", "id": f"m{key}"}, _img(sid, aid, key, shown), raw_link)
        case Marker(key, Note(notice, text)):
            label = el("span", {"class": "label"}, notice.value)
            css = f"step marker {notice.name.lower()}"
            gist = task_summary(text)
            if not _long(text) and gist is None:
                shown = el("span", {"class": "text"}, text)
                return el("div", {"class": css, "id": f"m{key}"}, label, " ", shown, raw_link)
            first = gist or (text.strip().splitlines() or [""])[0]
            return el(
                "details",
                {"class": css, "id": f"m{key}", "open": here},
                el("summary", None, label, " ", human.clip(first, 120)),
                el("pre", None, text),
                raw_link,
            )
        case Marker(key, Hidden(lines)):
            counts = ", ".join(f"{n} {name}" for name, n in Counter(name for _, name in lines).most_common())
            return el(
                "details",
                {"class": "step hidden", "id": f"m{key}"},
                el("summary", None, f"{_plural(len(lines), 'hidden line')}: {counts}"),
                el("ul", None, *(el("li", None, el("a", {"href": href_raw(sid, aid, o)}, name)) for o, name in lines)),
            )


def _gist(given: str) -> str:
    value = json.loads(given)
    fields = value.values() if isinstance(value, dict) else ()
    first = next((v for v in fields if isinstance(v, str) and v.strip()), None)
    return human.clip(" ".join(first.split()) if first else given, 160)


def _output(
    sid: SessionId, aid: AgentId | None, out: Output, at: Key | None, terms: tuple[str, ...], css: str = ""
) -> Html:
    here = out.key == at
    css += "output error" if out.error else "output"
    streams = printed(out.text)
    if streams is None:
        streams = ((out.text, False),)
    body = (
        *(_stream(text, stderr, here, terms) for text, stderr in streams),
        *(_img(sid, aid, out.key, p) for p in out.pictures),
        el("a", {"class": "raw", "href": href_raw(sid, aid, out.key.line_off)}, "raw"),
    )
    shown = "\n".join(text for text, _ in streams)
    if not _long(shown) and not out.pictures:
        label = el("span", {"class": "label"}, "error") if out.error else None
        return el("div", {"class": css, "id": f"m{out.key}"}, label, *body)
    first = (shown.splitlines() or [""])[0]
    flags = (" · error" if out.error else "") + (" · stderr" if any(stderr for _, stderr in streams) else "")
    summary = human.clip(first, 120) + f" · {_count(shown)}" + flags
    return el("details", {"class": css, "id": f"m{out.key}", "open": here}, el("summary", None, summary), *body)


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
        None,
        el("h1", None, el("a", {"href": "./"}, "claudinator")),
        el(
            "form",
            {"method": "get", "action": "./", "class": "find"},
            el("input", {"type": "search", "name": "q", "value": form.q, "placeholder": 'words, "a phrase", deploy*'}),
            el("input", {"name": "project", "value": form.project, "placeholder": "project"}),
            el("input", {"name": "since", "value": form.since, "placeholder": "since: 2026-10-01, 7d"}),
            el("input", {"name": "until", "value": form.until, "placeholder": "until"}),
            el("label", None, el("input", {"type": "checkbox", "name": "tools", "checked": form.tools}), " tools"),
            el("button", None, "search"),
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
    where = human.short(s.cwd)
    project = el("a", {"href": "./?" + urlencode({"project": s.cwd}), "title": where}, where) if s.cwd else where
    return el(
        "tr",
        None,
        el("td", {"class": "muted"}, human.when(s.last_ts)),
        el(
            "td",
            {"class": "title"},
            el("a", {"href": href_transcript(s.id)}, human.clip(s.title or s.id[:8], 120)),
            None if s.on_disk else el("span", {"class": "gone"}, " gone"),
        ),
        el("td", {"class": "project"}, project),
        el("td", {"class": "muted"}, _plural(s.prompts, "prompt")),
        el("td", {"class": "muted"}, _plural(s.subagents, "agent") if s.subagents else ""),
    )


def _hit(hit: SessionHit, q: str) -> Html:
    s, best = hit.session, hit.best
    at = None if best.kind is Kind.TITLE else Key(best.line_off, best.block)
    where = best.kind.value.replace("_", " ") + (f" in subagent {best.agent_id[:8]}" if best.agent_id else "")
    return el(
        "li",
        None,
        el("a", {"href": href_transcript(s.id)}, s.title or s.id[:8]),
        el("span", {"class": "muted"}, f" {human.when(s.last_ts)} · {human.short(s.cwd)} · {_plural(hit.hits, 'hit')}"),
        el(
            "a",
            {"class": "excerpt", "href": href_transcript(s.id, best.agent_id, at, q)},
            el("span", {"class": "label"}, where),
            " ",
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
