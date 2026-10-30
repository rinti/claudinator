import sqlite3

import claudinator.archive as archive_module
from claudinator.search_text import excerpt, marks, searchable

SAMPLE = "".join(map(chr, [*range(0x20, 0x7F), *range(0xA0, 0x180)])) + "åäöÅÄÖﬁﬂ"


def fts5_tokens(chars: str) -> list[str]:
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(archive_module._PROJECTION_DDL)
        db.executemany("INSERT INTO entry_fts (rowid, text) VALUES (?, ?)", enumerate(chars, 1))
        db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, entry_fts, instance)")
        tokens = dict(db.execute("SELECT doc, term FROM temp.vocab"))
    finally:
        db.close()
    return [tokens.get(i, "") for i in range(1, len(chars) + 1)]


def test_a_term_is_searchable_when_fts5_finds_a_token_in_it():
    tokens = fts5_tokens(SAMPLE)
    assert [c for c in SAMPLE if searchable([c])] == [c for c, token in zip(SAMPLE, tokens) if token]


def test_excerpt_marks_the_words_fts5_matches():
    words = [(c, token) for c, token in zip(SAMPLE, fts5_tokens(SAMPLE)) if token]
    text = " ".join(c for c, _ in words)
    expected, marked = {}, {}
    for term in sorted({token for _, token in words}):
        expected[term] = tuple((2 * i, 2 * i + 1) for i, (_, token) in enumerate(words) if token == term)
        marked[term] = excerpt(text, (term,), width=len(text))[1]
    assert marked == expected


def test_marks_spans_matches_in_text_as_written_across_lines_and_case():
    text = "Run  the\n\n  Launchd\nagent; launchd-agents differ, and so does Ådalen"
    assert [text[s:e] for s, e in marks(text, ("launchd agent", "adalen"))] == ["Launchd\nagent", "Ådalen"]
