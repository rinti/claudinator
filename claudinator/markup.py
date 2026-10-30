from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from importlib import resources

from claudinator import human

FONTS = {
    name: resources.files("claudinator").joinpath(f"fonts/{name}-Variable.woff2").read_bytes()
    for name in ("Geist", "GeistMono")
}


class Html(str):
    pass


_VOID = frozenset({"meta", "base", "link", "img", "input"})


def text(s: str) -> Html:
    if not human.HIDDEN.search(s):
        return Html(html.escape(s))
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
    start = f"<{tag}"
    for name, value in (attrs or {}).items():
        if value is True:
            start += f" {name}"
        elif value:
            start += f' {name}="{html.escape(human.printable(value))}"'
    if tag in _VOID:
        return Html(start + ">")
    inner = "".join([child if isinstance(child, Html) else text(child) for child in children if child is not None])
    return Html(f"{start}>{inner}</{tag}>")


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
        *(_preload(name) for name in FONTS if root),
        el("title", None, human.printable(title)),
    )
    return el("html", {"lang": "en"}, head, el("body", None, *body))


def _preload(name: str) -> Html:
    return el(
        "link",
        {
            "rel": "preload",
            "href": f"fonts/{name}-Variable.woff2",
            "as": "font",
            "type": "font/woff2",
            "crossorigin": True,
        },
    )


def document(markup: Html) -> bytes:
    return ("<!doctype html>\n" + markup).encode(errors="replace")

