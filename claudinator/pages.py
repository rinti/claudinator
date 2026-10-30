from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from itertools import groupby
from operator import itemgetter
from urllib.parse import quote, urlencode

from claudinator import human
from claudinator.archive import Filters, Session, SessionHit, SessionSummary, Subagent, SweepReport
from claudinator.conversation import Call, Hidden, Image, Key, Marker, Output, Said, Step, Thought
from claudinator.markup import Html, el, marked, page, text
from claudinator.record import AgentId, Kind, Millis, Note, Notice, Picture, SessionId, printed, task_summary
from claudinator.search_text import marks, searchable

SEARCH_LIMIT = 50
FOLD_LINES, FOLD_CHARS = 3, 300
INLINE_CHARS = 1 << 20


def href_transcript(
    sid: SessionId, aid: AgentId | None = None, at: Key | None = None, search: Form | None = None, hidden: bool = False
) -> str:
    path = f"s/{sid}" + (f"/{aid}" if aid else "")
    landed = {} if at is None else {"at": str(at)}
    query = landed | (search.params() if search else {}) | ({"hidden": "1"} if hidden else {})
    return path + (f"?{urlencode(query)}" if query else "") + ("" if at is None else f"#m{at}")


def href_call(sid: SessionId, parent: AgentId | None, tool_use_id: str) -> str:
    return f"s/{sid}" + (f"/{quote(parent, safe='')}" if parent else "") + f"?{urlencode({'call': tool_use_id})}"


def href_raw(sid: SessionId, aid: AgentId | None, line_off: int) -> str:
    return f"raw/{sid}" + (f"/{aid}" if aid else "") + f"/{line_off}"


def href_image(sid: SessionId, aid: AgentId | None, key: Key, part: int | None) -> str:
    return f"image/{sid}" + (f"/{aid}" if aid else "") + f"/{key}" + (f"/{part}" if part is not None else "")


def href_export(sid: SessionId, aid: AgentId | None) -> str:
    return f"export/{sid}" + (f"/{aid}" if aid else "")


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

    def params(self) -> dict[str, str]:
        given = {"q": self.q, "project": self.project, "since": self.since, "until": self.until}
        return {name: value for name, value in given.items() if value} | ({"tools": "on"} if self.tools else {})

    def terms(self) -> tuple[str, ...]:
        return tuple(phrase or word for phrase, word in re.findall(r'"([^"]*)"|(\S+)', self.q))

    def filters(self, at: Millis) -> Filters:
        def when(name: str, text: str) -> Millis | None:
            try:
                return human.parse_when(text, at) if text else None
            except ValueError as e:
                raise ValueError(f"{name}: {e}") from None

        return Filters(self.project or None, when("since", self.since), when("until", self.until))


def message_page(root: str | None, phrase: str, said: str) -> Html:
    shown = el(
        "main",
        {"class": "message"},
        el("h1", None, phrase),
        el("p", {"class": "banner"}, said),
        _back("./", "All sessions") if root else None,
    )
    return page(root, phrase, _top("") if root else None, shown)


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


def _title(s: SessionSummary) -> tuple[Html | str, ...]:
    title = s.title or s.id[:8]
    if m := COMMAND.match(title):
        return el("span", {"class": "cmd"}, m[0]), *_rest(title[m.end() :])
    if s.heading and title.startswith(f"# {s.heading}"):
        head = el("span", {"class": "head"}, s.heading)
        return el("span", {"class": "hash"}, "#"), head, *_rest(title[2 + len(s.heading) :])
    return (title,)


def _rest(s: str) -> tuple[Html | str, ...]:
    return (" ", el("span", {"class": "rest"}, s.strip())) if s.strip() else ()


def _where(cwd: str | None) -> tuple[Html | None, Html]:
    parent, slash, leaf = human.short(cwd).rpartition("/")
    return el("span", {"class": "dir"}, parent + slash) if slash else None, el("span", {"class": "leaf"}, leaf)


def _tally(n: int, noun: str) -> Html:
    return el("span", {"class": "count"}, el("span", {"class": "n"}, f"{n:,}"), " ", _noun(n, noun))


def search_page(root: str, hits: Sequence[SessionHit], form: Form, swept: SweepReport | None) -> Html:
    count = _plural(len(hits), "session") + (", the best of more" if len(hits) == SEARCH_LIMIT else "")
    about = " ".join(part for part in (f"matching “{form.q}”", _scope(form)) if part)
    if hits:
        found = el("ol", {"class": "hits"}, *(_hit(hit, form) for hit in hits))
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


def _hit(hit: SessionHit, form: Form) -> Html:
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
    title = el("a", {"class": "open", "href": href_transcript(s.id, best.agent_id, at, form)}, s.title or s.id[:8])
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
        el("a", {"class": "clear", "href": href_list(replace(form, q=""))}, "Clear") if clear else None,
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


def href_list(form: Form) -> str:
    query = urlencode(form.params())
    return "./" + (f"?{query}" if query else "")


def transcript_page(
    root: str,
    session: Session,
    aid: AgentId | None,
    steps: Sequence[Step],
    at: Key | None,
    search: Form,
    hidden: bool,
    swept: SweepReport | None,
) -> Html:
    s = session.summary
    terms = searchable(search.terms())
    agent = None if aid is None else next((a for a in session.agents if a.id == aid), None)
    if aid is None:
        heading = s.title or s.id[:8]
    else:
        heading = (agent and agent.description) or f"Subagent {aid[:8]}"
    if search.q:
        back = _back(href_list(search), f"Results for “{search.q}”")
    elif aid:
        back = _back(href_transcript(s.id), s.title or s.id[:8])
    else:
        back = _back("./", "All sessions")
    if aid is None:
        span = s.first_ts, s.last_ts
    else:
        stamps = [step.ts for step in steps if not isinstance(step, Marker) and step.ts is not None]
        span = min(stamps, default=None), max(stamps, default=None)
    header = el("header", {"class": "session"}, el("h1", None, heading), _facts(s, *span, main=aid is None))
    prompts = (step.key for step in steps if isinstance(step, Said) and step.kind is Kind.PROMPT)
    numbers = {key: n for n, key in enumerate(prompts, 1)}
    shown = [step for step in steps if hidden or not _folded(step)]
    toggle = _toggle(href_transcript(s.id, aid, at, search, not hidden), sum(map(_folded, steps)), hidden)
    aside = _aside(session, aid, agent, el("div", {"class": "actions"}, _download(s.id, aid), toggle))
    body = el("div", {"class": "steps"}, *_dated(s.id, aid, shown, at, terms, numbers))
    return page(
        root,
        heading,
        _top(search.q, replace(search, q="").params()),
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


def _facts(s: SessionSummary, start: Millis | None, end: Millis | None, *, main: bool) -> Html:
    first, last = _local(start), _local(end)
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
            rest = text[command.end() :].lstrip() if command else text
            said = el(
                "div",
                {"class": "text"},
                el("span", {"class": "cmd"}, _text(command[0], here, terms)) if command else None,
                " " if command and rest else None,
                *_prose(rest, here, terms),
            )
            label = el("p", {"class": "label"}, f"Prompt {numbers[key]}")
            css = "step prompt long" if _long(text) else "step prompt"
            return el("div", {"class": css, "id": f"m{key}"}, label, said, raw_link)
        case Said(key, _, _, text):
            said = el("div", {"class": "text"}, *_prose(text.strip(), here, terms))
            return el("div", {"class": "step reply", "id": f"m{key}"}, said, raw_link)
        case Call():
            return _call(sid, aid, step, at, terms, raw_link)
        case Output():
            first = (_plain(step).strip().splitlines() or [""])[0]
            body = _output_body(sid, aid, step, at, terms)
            return _row(step.key, "output error" if step.error else "output", "Output", first, _size(step), body, here)
        case Thought(key, _, text):
            first = (text.strip().splitlines() or [""])[0]
            body = (el("div", {"class": "text"}, *_prose(text.strip(), here, terms)), raw_link)
            return _row(key, "thought", "Thinking", first, el("span", {"class": "size"}, _lines(text)), body, here)
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
                    _plural(len(lines), "hidden line"),
                    el("span", {"class": "badge"}, "hidden"),
                    el("span", {"class": "kinds"}, counts),
                ),
                el("ul", None, *(el("li", None, el("a", {"href": href_raw(sid, aid, o)}, name)) for o, name in lines)),
            )


def _row(
    key: Key, css: str, name: str, gist: str, end: Html | None, panel: Sequence[Html | None], opened: bool, *after: Html
) -> Html:
    summary = el("summary", None, el("b", None, name), " ", el("span", {"class": "gist"}, human.clip(gist, 160)), end)
    details = el("details", {"open": opened}, summary, el("div", {"class": "panel"}, *panel))
    return el("div", {"class": f"step {css}", "id": f"m{key}"}, details, *after)


def _call(
    sid: SessionId, aid: AgentId | None, call: Call, at: Key | None, terms: tuple[str, ...], raw_link: Html
) -> Html:
    out = call.result
    given = json.dumps(json.loads(call.input), indent=2, ensure_ascii=False)
    panel = [el("pre", {"class": "input"}, _text(given, call.key == at, terms)), raw_link]
    if out is not None:
        css = "output error" if out.error else "output"
        panel.append(el("div", {"class": css, "id": f"m{out.key}"}, *_output_body(sid, aid, out, at, terms)))
    opened = at is not None and at in (call.key, out and out.key)
    failed = out is not None and out.error
    if call.spawned:
        spawned = el(
            "a", {"class": "spawned", "href": href_transcript(sid, call.spawned.id)}, f"↳ {call.spawned.id[:8]}"
        )
        css = "call spawns error" if failed else "call spawns"
        return _row(call.key, css, call.tool, _gist(call.input), _size(out) if failed else None, panel, opened, spawned)
    return _row(call.key, "call error" if failed else "call", call.tool, _gist(call.input), _size(out), panel, opened)


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


FENCE = re.compile(r"^```(?P<info>[^\n`]*)$", re.M)
INLINE_CODE = re.compile(r"`(?P<code>[^`\n]+)`")


def _fences(s: str) -> Iterator[tuple[re.Match[str], re.Match[str]]]:
    lines = FENCE.finditer(s)
    for opener in lines:
        closer = next((m for m in lines if not m["info"].strip(" \t")), None)
        if closer is None:
            return
        yield opener, closer


def _prose(s: str, here: bool, terms: tuple[str, ...]) -> tuple[Html, ...]:
    s, out, at = _cut(s), [], 0
    for opener, closer in _fences(s):
        info, body = opener["info"].strip(), s[opener.end() + 1 : closer.start()].removesuffix("\n")
        label = el("span", {"class": "info"}, _mark(info, here, terms)) if info else None
        out += _inline(s[at : opener.start()], here, terms)
        out.append(el("pre", {"class": "code"}, label, _mark(body, here, terms)))
        at = closer.end() + 1
    return (*out, *_inline(s[at:], here, terms))


def _inline(s: str, here: bool, terms: tuple[str, ...]) -> list[Html]:
    out, at = [], 0
    for m in INLINE_CODE.finditer(s):
        out += (_mark(s[at : m.start()], here, terms), el("code", None, _mark(m["code"], here, terms)))
        at = m.end()
    return [*out, _mark(s[at:], here, terms)]


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


def _top(q: str, kept: Mapping[str, str] | None = None) -> Html:
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
        *(el("input", {"type": "hidden", "name": name, "value": value}) for name, value in (kept or {}).items()),
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

