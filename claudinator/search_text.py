from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def fts_expr(term: str) -> str:
    """One term -> an FTS5 MATCH expression that cannot be a syntax error.

    The term becomes a quoted string ("" escapes "), and a trailing * stays
    outside the quotes as a prefix query. So "launchd agent" -> '"launchd
    agent"' and sync* -> '"sync"*'. FTS5 operators are not exposed."""
    word = term.rstrip("*")
    return '"' + word.replace('"', '""') + '"' + ("*" if word != term else "")


def searchable(terms: Iterable[str]) -> tuple[str, ...]:
    """The terms that contain a token. FTS5 matches nothing for a phrase with
    no token, such as "*" or "→", so one of those would empty every search."""
    return tuple(term for term in terms if _TOKEN.search(term.rstrip("*").translate(_FOLD)))


class _FoldTable(dict[int, str]):
    def __missing__(self, codepoint: int) -> str:
        folded = unicodedata.normalize("NFKD", chr(codepoint))[:1].lower()[:1]
        self[codepoint] = folded
        return folded


_FOLD = _FoldTable()
_TOKEN = re.compile(r"[^\W_]+")


def _term_pattern(term: str) -> re.Pattern[str] | None:
    """A typed term as a regex over folded text that matches what FTS5
    matches: consecutive whole tokens, the last one a prefix if the term ends
    with *, which then marks the whole token. So "launchd agent" also matches
    "launchd-agent"."""
    tokens = _TOKEN.findall(term.rstrip("*").translate(_FOLD))
    if not tokens:
        return None
    end = r"[^\W_]*" if term.endswith("*") else r"(?![^\W_])"
    return re.compile(r"(?<![^\W_])" + r"[\W_]+".join(map(re.escape, tokens)) + end)


def excerpt(text: str, terms: tuple[str, ...], width: int = 160) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Window of `width` chars around the first match, whitespace collapsed,
    with the [start, end) spans of every match inside the window."""
    flat = " ".join(text.split())
    folded = flat.translate(_FOLD)
    spans = sorted(m.span() for t in terms if (p := _term_pattern(t)) for m in p.finditer(folded))
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    first = merged[0][0] if merged else 0
    start = max(0, min(first - width // 4, len(flat) - width))
    end = min(len(flat), start + width)
    lead = "..." if start > 0 else ""
    shift = len(lead) - start
    marks = tuple((s + shift, e + shift) for s, e in merged if start <= s and e <= end)
    return lead + flat[start:end] + ("..." if end < len(flat) else ""), marks
