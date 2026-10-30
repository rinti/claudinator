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
class Filter:
    name: str
    label: str
    scope: str
    placeholder: str


FILTERS = (
    Filter("project", "Project", "in", "any"),
    Filter("since", "Since", "since", "7d"),
    Filter("until", "Until", "until", "any"),
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
        return cls(query.get("q", ""), **{f.name: query.get(f.name, "") for f in FILTERS}, tools="tools" in query)

    def values(self) -> tuple[tuple[Filter, str], ...]:
        return tuple((f, getattr(self, f.name)) for f in FILTERS)

    def params(self) -> dict[str, str]:
        given = {"q": self.q, **{f.name: value for f, value in self.values()}, "tools": "on" if self.tools else ""}
        return {name: value for name, value in given.items() if value}

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
            *(_pill(f, value) for f, value in form.values()),
            el("label", {"class": "pill tools"}, tools, "Include tools"),
        ),
    )


def _pill(f: Filter, value: str) -> Html:
    field = el(
        "input", {"name": f.name, "form": "find", "value": value, "placeholder": f.placeholder, "spellcheck": "false"}
    )
    return el("label", {"class": "pill"}, f.label, field)


def _scope(form: Form) -> str:
    return " ".join(f"{f.scope} {value}" for f, value in form.values() if value)


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
    results = _back(href_list(search), f"Results for “{search.q}”") if search.q else None
    toggle = _toggle(href_transcript(s.id, aid, at, search, not hidden), sum(map(_folded, steps)), hidden)
    actions = el("div", {"class": "actions"}, _download(s.id, aid), toggle)
    if aid is None:
        heading, back = s.title or s.id[:8], results or _back("./", "All sessions")
        span, counts = (s.first_ts, s.last_ts), (s.prompts, s.subagents)
        aside = _session_aside(session, actions)
    else:
        agent = next((a for a in session.agents if a.id == aid), None)
        heading = (agent and agent.description) or f"Subagent {aid[:8]}"
        back = results or _back(href_transcript(s.id), s.title or s.id[:8])
        stamps = [step.ts for step in steps if not isinstance(step, Marker) and step.ts is not None]
        span, counts = (min(stamps, default=None), max(stamps, default=None)), None
        aside = _subagent_aside(s, agent, actions)
    header = el("header", {"class": "session"}, el("h1", None, heading), _facts(s.cwd, *span, counts))
    prompts = (step.key for step in steps if isinstance(step, Said) and step.kind is Kind.PROMPT)
    numbers = {key: n for n, key in enumerate(prompts, 1)}
    view = _View(s.id, aid, at, searchable(search.terms()), numbers)
    body = el("div", {"class": "steps"}, *_dated(view, [step for step in steps if hidden or not _folded(step)]))
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


def _facts(cwd: str | None, start: Millis | None, end: Millis | None, counts: tuple[int, int] | None) -> Html:
    first, last = _local(start), _local(end)
    span = duration = None
    if first and last:
        until = f"{last:%H:%M}" if last.date() == first.date() else _stamp(last)
        span, duration = f"{_stamp(first)} – {until}", _duration(last - first)
    prompts, subagents = counts or (0, 0)
    return el(
        "p",
        {"class": "facts"},
        el("span", None, *_where(cwd)),
        el("span", None, span) if span else None,
        el("span", {"class": "faint"}, duration) if duration else None,
        el("span", None, _plural(prompts, "prompt")) if counts else None,
        el("span", None, _plural(subagents, "subagent")) if subagents else None,
    )


def _stamp(when: datetime) -> str:
    return f"{when:%a} {when.day} {when:%b} · {when:%H:%M}"


def _duration(span: timedelta) -> str:
    hours, minutes = divmod(round(span.total_seconds() / 60), 60)
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _session_aside(session: Session, actions: Html) -> Html:
    s = session.summary
    command = el(
        "p",
        {"class": "command"},
        el("span", {"aria-hidden": "true"}, "$"),
        el("code", None, f"claudinator resume {s.id[:8]}"),
    )
    gone = None if s.on_disk else el("p", {"class": "gone"}, "Claude Code deleted its copy. Resuming puts it back.")
    agents = el(
        "section",
        None,
        el("h2", None, "Subagents ", el("span", {"class": "n"}, f"{len(session.agents):,}")),
        el("ul", {"class": "agents"}, *(_agent(s.id, a) for a in session.agents)),
    )
    resume = el("section", None, el("h2", None, "Resume"), command, gone, actions)
    return el("aside", None, resume, agents if session.agents else None)


def _subagent_aside(s: SessionSummary, agent: Subagent | None, actions: Html) -> Html:
    call = agent and agent.tool_use_id and href_call(s.id, agent.parent, agent.tool_use_id)
    return el(
        "aside",
        None,
        el(
            "section",
            None,
            el("h2", None, "Subagent"),
            el("p", {"class": "kind"}, agent.agent_type) if agent and agent.agent_type else None,
            el("p", None, "In ", el("a", {"href": href_transcript(s.id)}, s.title or s.id[:8])),
            el("p", None, el("a", {"href": call}, "See the call that started it")) if call else None,
            actions,
        ),
    )


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


def _dated(view: _View, steps: Sequence[Step]) -> Iterator[Html]:
    seen = None
    for minute, run in _minutes(steps):
        if minute is not None and minute.date() != seen:
            seen = minute.date()
            yield el("h2", {"class": "day"}, _day(seen, year=True))
        parts: list[Html] = []
        for machine, group in groupby(run, key=lambda step: isinstance(step, Call | Output | Thought)):
            made = [_step(view, step) for step in group]
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


@dataclass(frozen=True, slots=True)
class _View:
    sid: SessionId
    aid: AgentId | None
    at: Key | None
    terms: tuple[str, ...]
    numbers: Mapping[Key, int]

    def mark(self, key: Key, s: str) -> Html:
        return self._marked(key, _cut(s))

    def prose(self, key: Key, s: str) -> tuple[Html, ...]:
        s, out, at = _cut(s), [], 0
        for opener, closer in _fences(s):
            info, body = opener["info"].strip(), s[opener.end() + 1 : closer.start()].removesuffix("\n")
            label = el("span", {"class": "info"}, self._marked(key, info)) if info else None
            out += self._inline(key, s[at : opener.start()])
            out.append(el("pre", {"class": "code"}, label, self._marked(key, body)))
            at = closer.end() + 1
        return (*out, *self._inline(key, s[at:]))

    def raw(self, key: Key) -> Html:
        return el("a", {"class": "raw", "href": href_raw(self.sid, self.aid, key.line_off)}, "raw")

    def img(self, key: Key, shown: Picture) -> Html:
        about = f"image, {shown.media_type or 'no type'}, {human.size(shown.size)}"
        return el("img", {"loading": "lazy", "alt": about, "src": href_image(self.sid, self.aid, key, shown.part)})

    def frame(self, tag: str, key: Key, css: str, *inner: Html | str | None, raw: bool = True) -> Html:
        attrs = {"class": css, "id": f"m{key}", "open": tag == "details" and key == self.at}
        return el(tag, attrs, *inner, self.raw(key) if raw else None)

    def _inline(self, key: Key, s: str) -> list[Html]:
        out, at = [], 0
        for m in INLINE_CODE.finditer(s):
            out += (self._marked(key, s[at : m.start()]), el("code", None, self._marked(key, m["code"])))
            at = m.end()
        return [*out, self._marked(key, s[at:])]

    def _marked(self, key: Key, s: str) -> Html:
        return marked(s, marks(s, self.terms)) if key == self.at and self.terms else text(s)


def _step(view: _View, step: Step) -> Html:
    match step:
        case Said(key, _, Kind.PROMPT, said):
            return _prompt(view, key, said.strip())
        case Said(key, _, _, said):
            return view.frame("div", key, "step reply", el("div", {"class": "text"}, *view.prose(key, said.strip())))
        case Call():
            return _call(view, step)
        case Output(key):
            first = (_plain(step).strip().splitlines() or [""])[0]
            panel = (*_output_body(view, step), view.raw(key))
            css = "output error" if step.error else "output"
            return _row(view, key, css, "Output", first, panel, end=_size(step), opened=key == view.at)
        case Thought(key, _, said):
            first = (said.strip().splitlines() or [""])[0]
            panel = (el("div", {"class": "text"}, *view.prose(key, said.strip())), view.raw(key))
            size = el("span", {"class": "size"}, _lines(said))
            return _row(view, key, "thought", "Thinking", first, panel, end=size, opened=key == view.at)
        case Image(key, _, shown):
            return view.frame("div", key, "step image", view.img(key, shown))
        case Marker(key, Note() as note):
            return _note(view, key, note)
        case Marker(key, Hidden(lines)):
            counts = ", ".join(f"{n} {name}" for name, n in Counter(name for _, name in lines).most_common())
            summary = el(
                "summary",
                None,
                _plural(len(lines), "hidden line"),
                el("span", {"class": "badge"}, "hidden"),
                el("span", {"class": "kinds"}, counts),
            )
            listed = (el("li", None, el("a", {"href": href_raw(view.sid, view.aid, o)}, name)) for o, name in lines)
            return view.frame("details", key, "step note hidden", summary, el("ul", None, *listed), raw=False)


def _prompt(view: _View, key: Key, said: str) -> Html:
    command = COMMAND.match(said)
    rest = said[command.end() :].lstrip() if command else said
    shown = el(
        "div",
        {"class": "text"},
        el("span", {"class": "cmd"}, view.mark(key, command[0])) if command else None,
        " " if command and rest else None,
        *view.prose(key, rest),
    )
    label = el("p", {"class": "label"}, f"Prompt {view.numbers[key]}")
    return view.frame("div", key, "step prompt long" if _long(said) else "step prompt", label, shown)


def _note(view: _View, key: Key, note: Note) -> Html:
    css = f"step note {note.notice.name.lower()}"
    gist = task_summary(note.text)
    quoted = note.notice not in STOCK and note.text.strip()
    if _long(note.text) or gist is not None:
        first = gist or (note.text.strip().splitlines() or [""])[0]
        said = NOTICES[note.notice] + (f": {human.clip(first, 120)}" if quoted else "")
        return view.frame("details", key, css, el("summary", None, said), el("pre", None, note.text))
    shown = el("span", {"class": "text"}, note.text) if quoted else None
    return view.frame("div", key, css, NOTICES[note.notice] + (": " if shown else "."), shown)


def _row(
    view: _View,
    key: Key,
    css: str,
    name: str,
    gist: str,
    panel: Sequence[Html | None],
    *,
    end: Html | None = None,
    opened: bool = False,
    spawned: Html | None = None,
) -> Html:
    summary = el("summary", None, el("b", None, name), " ", el("span", {"class": "gist"}, human.clip(gist, 160)), end)
    details = el("details", {"open": opened}, summary, el("div", {"class": "panel"}, *panel))
    return view.frame("div", key, f"step {css}", details, spawned, raw=False)


def _call(view: _View, call: Call) -> Html:
    out = call.result
    given = json.dumps(json.loads(call.input), indent=2, ensure_ascii=False)
    panel = [el("pre", {"class": "input"}, view.mark(call.key, given)), view.raw(call.key)]
    if out is not None:
        panel.append(view.frame("div", out.key, "output error" if out.error else "output", *_output_body(view, out)))
    opened = view.at is not None and view.at in (call.key, out and out.key)
    failed = out is not None and out.error
    if call.spawned:
        href = href_transcript(view.sid, call.spawned.id)
        spawned = el("a", {"class": "spawned", "href": href}, f"↳ {call.spawned.id[:8]}")
        css = "call spawns error" if failed else "call spawns"
        end = _size(out) if failed else None
        return _row(view, call.key, css, call.tool, _gist(call.input), panel, end=end, opened=opened, spawned=spawned)
    css = "call error" if failed else "call"
    return _row(view, call.key, css, call.tool, _gist(call.input), panel, end=_size(out), opened=opened)


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


def _output_body(view: _View, out: Output) -> tuple[Html, ...]:
    streams = (_stream(view, out.key, said, stderr) for said, stderr in _streams(out))
    return (*streams, *(view.img(out.key, p) for p in out.pictures))


def _stream(view: _View, key: Key, said: str, stderr: bool) -> Html:
    shown = el("pre", None, view.mark(key, said))
    return el("div", {"class": "stderr"}, el("span", {"class": "label"}, "stderr"), shown) if stderr else shown


FENCE = re.compile(r"^```(?P<info>[^\n`]*)$", re.M)
INLINE_CODE = re.compile(r"`(?P<code>[^`\n]+)`")


def _fences(s: str) -> Iterator[tuple[re.Match[str], re.Match[str]]]:
    lines = FENCE.finditer(s)
    for opener in lines:
        closer = next((m for m in lines if not m["info"].strip(" \t")), None)
        if closer is None:
            return
        yield opener, closer


def _cut(s: str) -> str:
    if len(s) > INLINE_CHARS:
        return s[:INLINE_CHARS] + f"\n… cut at {human.size(INLINE_CHARS)}; the raw line has all of it"
    return s


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

