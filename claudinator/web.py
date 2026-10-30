from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from io import BytesIO
from urllib.parse import parse_qsl, urlsplit

from claudinator import human
from claudinator.archive import Archive, ArchiveError, EmptyQuery, Query, SweepReport, UnknownSession, now
from claudinator.conversation import Call, Key, build
from claudinator.layout import AGENT_ID, Config
from claudinator.markup import FONTS, document
from claudinator.pages import (
    SEARCH_LIMIT,
    Form,
    href_transcript,
    message_page,
    search_page,
    sessions_page,
    transcript_page,
)
from claudinator.record import AgentId, SessionId, image

PAGE_POLICY = (
    "default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'self';"
    " frame-ancestors 'none'"
)
FILE_POLICY = "default-src 'none'; sandbox"
RASTER_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
HTML = "text/html; charset=utf-8"
STYLE = resources.files("claudinator").joinpath("web.css").read_bytes()


class Server(ThreadingHTTPServer):
    request_queue_size = 64

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
    cache: str = "no-store"


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
                ("Cache-Control", response.cache),
                *response.headers,
            ):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)
        except ConnectionError:
            pass


def _message(status: int, text: str, root: str | None = None) -> Response:
    return Response(status, HTML, document(message_page(root, HTTPStatus(status).phrase, text)))


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
                return Response(200, HTML, document(search_page(root, hits, form, swept)))
            rows = archive.sessions(filters, None)
            return Response(200, HTML, document(sessions_page(root, rows, form, swept, today)))
        except (ValueError, EmptyQuery) as e:
            return Response(400, HTML, document(sessions_page(root, (), form, swept, today, str(e))))


TRANSCRIPT = re.compile(rf"/s/({SID})(?:/({AID}))?")


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
    body = transcript_page(root, session, aid, steps, landed, Form.read(query), query.get("hidden") == "1", swept)
    return Response(200, HTML, document(body))


RAW = re.compile(rf"/raw/({SID})(?:/({AID}))?/({OFFSET})")


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


def picture(cfg: Config, root: str, query: Mapping[str, str], m: re.Match[str]) -> Response:
    with _reading(cfg, sweep=False) as (archive, _):
        line = archive.line(SessionId(m[1]), m[2] and AgentId(m[2]), int(m[3]))
    found = line and image(line, int(m[4]), m[5] and int(m[5]))
    if not found or found[0] not in RASTER_TYPES:
        return _message(404, "No image of a type this server shows is there.", root)
    return Response(200, found[0], found[1], FILE_POLICY)


EXPORT = re.compile(rf"/export/({SID})(?:/({AID}))?")


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
    return Response(200, "font/woff2", FONTS[m[1]], cache="max-age=31536000, immutable")


ROUTES: tuple[tuple[re.Pattern[str], Callable[[Config, str, Mapping[str, str], re.Match[str]], Response]], ...] = (
    (HOME, home),
    (TRANSCRIPT, transcript),
    (RAW, raw),
    (IMAGE, picture),
    (EXPORT, export),
    (STYLESHEET, stylesheet),
    (FONT, font),
)

