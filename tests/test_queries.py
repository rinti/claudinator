import json
import os
from datetime import UTC, datetime

import pytest

from claudinator.archive import AmbiguousSession, EmptyQuery, Filters, Query, Subagent, UnknownSession
from claudinator.record import Kind
from helpers import (
    AID,
    SID,
    SID2,
    SLUG,
    ai_title,
    append,
    assistant,
    custom_title,
    found,
    line,
    open_archive,
    projects,
    reply,
    tool_call,
    tool_result,
    transcript,
    user,
)

OCT_1 = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()) * 1000
SEP_4 = int(datetime(2026, 9, 4, tzinfo=UTC).timestamp()) * 1000


def matching(archive, *terms, **query) -> list[str]:
    return [hit.session.id for hit in archive.search(Query(terms, **query))]


def write_meta(sid: str, aid: str, **meta) -> None:
    path = projects() / SLUG / sid / "subagents" / f"agent-{aid}.meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta))


def test_search_covers_conversation_and_adds_tools_on_request():
    append(
        transcript(),
        user("deploy the launchd agent"),
        reply("the plist lives in LaunchAgents"),
        tool_call("Bash", {"command": "launchctl print gui/501"}),
        tool_result("service com.claudinator.sweep is loaded"),
        ai_title("Sweeper setup"),
    )
    with open_archive() as archive:
        archive.sweep()
        assert [found(archive, w) for w in ("launchd", "launchagents", "sweeper", "launchctl", "loaded")] == [1, 1, 1, 0, 0]
        assert [found(archive, w, tools=True) for w in ("launchctl", "loaded")] == [1, 1]


def test_search_matches_every_word_phrases_prefixes_and_folded_accents():
    append(transcript(), user("Första coachmöte feedback"), reply("deploying the launchd-agent tonight"))
    append(transcript(SID2), user("the agent for launchd"))
    with open_archive() as archive:
        archive.sweep()
        assert matching(archive, "forsta", "COACHMOTE") == [SID]
        assert matching(archive, "launchd agent") == [SID]
        assert sorted(matching(archive, "launchd", "agent")) == sorted([SID, SID2])
        assert matching(archive, "deploy") == []
        assert matching(archive, "deploy*") == [SID]
        assert matching(archive, "launchd", "absent") == []


@pytest.mark.parametrize("term", ["NOT", "a OR b", "NEAR(x y)", "col:x", "^start", '"quoted"', 'say "hi'])
def test_search_input_is_never_fts_syntax(term):
    append(transcript(), user('this is NOT a drill: a or b, col x, start "quoted" (x y)'))
    with open_archive() as archive:
        archive.sweep()
        assert isinstance(archive.search(Query((term,))), list)
        assert matching(archive, "NOT") == [SID]
        assert matching(archive, "a OR b") == [SID]


@pytest.mark.parametrize("term", ['"', "(", "*", ""])
def test_search_input_without_a_token_is_an_empty_query_not_fts_syntax(term):
    with open_archive() as archive, pytest.raises(EmptyQuery):
        archive.search(Query((term,)))


def test_search_excerpt_marks_matches_around_the_first_hit():
    append(transcript(), user("x " * 200 + "the Launchd-Agent runs hourly, launchd agent " + "y " * 200))
    with open_archive() as archive:
        archive.sweep()
        (hit,) = archive.search(Query(("launchd agent",)))
    assert (hit.hits, hit.best.kind, hit.best.ts, hit.best.agent_id) == (1, Kind.PROMPT, 1790848800000, None)
    assert [hit.best.text[s:e] for s, e in hit.best.marks] == ["Launchd-Agent", "launchd agent"]
    assert hit.best.text.startswith("...x x") and hit.best.text.endswith("y y...")
    assert "\n" not in hit.best.text and len(hit.best.text) == 160 + 6


def test_search_filters_by_project_and_time():
    append(transcript(), user("zebracorn early", ts="2026-09-01T10:00:00.000Z", cwd="/Users/me/dev/alpha"))
    append(
        transcript(SID2, slug="-Users-me-dev-beta"),
        user("zebracorn late", ts="2026-10-20T10:00:00.000Z", cwd="/Users/me/dev/beta"),
    )
    with open_archive() as archive:
        archive.sweep()
        assert matching(archive, "zebracorn", filters=Filters(project="ALPHA")) == [SID]
        assert matching(archive, "zebracorn", filters=Filters(since=OCT_1)) == [SID2]
        assert matching(archive, "zebracorn", filters=Filters(until=OCT_1)) == [SID]


def test_search_labels_hits_inside_subagents():
    append(transcript(), user("start"))
    append(transcript(agent=AID), user("subagent quokka task"))
    with open_archive() as archive:
        archive.sweep()
        hits = archive.search(Query(("quokka",)))
    assert [(h.session.id, h.best.agent_id, h.hits) for h in hits] == [(SID, AID, 1)]


def test_search_ranks_sessions_and_limits_their_number():
    worst, best, middle = (f"{n}0000000-0000-4000-8000-000000000000" for n in "abc")
    append(transcript(worst), user("a long note that mentions a quokka once among many other words " * 5))
    append(transcript(best), user("quokka quokka quokka"), reply("more about the quokka"))
    append(transcript(middle), user("quokka and quokka in a short note"))
    with open_archive() as archive:
        archive.sweep()
        hits = archive.search(Query(("quokka",)))
        assert [(h.session.id, h.hits) for h in hits] == [(best, 2), (middle, 1), (worst, 1)]
        assert matching(archive, "quokka", limit=1) == [best]


def test_sessions_newest_first_with_title_precedence(home):
    ids = [f"{n}0000000-0000-4000-8000-000000000000" for n in "abcd"]
    append(transcript(ids[0]), user("first", ts="2026-10-04T10:00:00.000Z"), ai_title("AI"), custom_title("Custom"))
    append(transcript(ids[1]), user("second", ts="2026-10-03T10:00:00.000Z"), ai_title("AI two"))
    append(transcript(ids[2]), user("third", ts="2026-10-02T10:00:00.000Z"), ai_title("AI three"))
    append(transcript(ids[3]), user("  the first\n prompt  ", ts="2026-10-01T10:00:00.000Z"), user("second prompt"))
    desktop = home / "Library/Application Support/Claude/claude-code-sessions/a/b"
    desktop.mkdir(parents=True)
    (desktop / "local_1.json").write_text(json.dumps({"cliSessionId": ids[1], "title": "Desktop title"}))
    (desktop / "local_0.json").write_text(json.dumps({"cliSessionId": ids[0], "title": "Loses to custom"}))
    with open_archive() as archive:
        archive.sweep()
        listed = [(s.id, s.title) for s in archive.sessions()]
    assert listed == [(ids[0], "Custom"), (ids[1], "Desktop title"), (ids[2], "AI three"), (ids[3], "the first prompt")]


def test_sessions_count_prompts_and_subagents_and_notice_deletion():
    append(transcript(), user("one"), reply("reply"), user("two"), tool_result("output"))
    append(transcript(agent=AID), user("subagent prompt"))
    append(transcript(agent="b0b0b0b0b0b0b0b0b"), user("another subagent"))
    with open_archive() as archive:
        archive.sweep()
        (summary,) = archive.sessions()
        assert (summary.prompts, summary.subagents, summary.on_disk, summary.cwd) == (2, 2, True, "/Users/me/dev/demo")
        transcript().unlink()
        assert archive.sessions()[0].on_disk is False


def test_sessions_since_until_select_overlapping_sessions():
    a, b, c = (f"{n}0000000-0000-4000-8000-000000000000" for n in "abc")
    append(transcript(a), user("a1", ts="2026-09-01T10:00:00.000Z"), user("a2", ts="2026-09-03T10:00:00.000Z"))
    append(transcript(b), user("b1", ts="2026-09-02T10:00:00.000Z"), user("b2", ts="2026-10-05T10:00:00.000Z"))
    append(transcript(c), user("c1", ts="2026-10-10T10:00:00.000Z"))
    with open_archive() as archive:
        archive.sweep()
        assert [s.id for s in archive.sessions(Filters(since=SEP_4))] == [c, b]
        assert [s.id for s in archive.sessions(Filters(until=SEP_4))] == [b, a]
        assert [s.id for s in archive.sessions(limit=1)] == [c]


def test_session_resolves_unique_prefixes():
    other = "8f2d9999-0000-4000-8000-000000000000"
    append(transcript(), user("one"))
    append(transcript(other), user("two"))
    with open_archive() as archive:
        archive.sweep()
        assert archive.session("8f2d43").summary.id == SID
        assert archive.session(SID).summary.id == SID
        with pytest.raises(AmbiguousSession) as ambiguous:
            archive.session("8f2d")
        assert sorted(s.id for s in ambiguous.value.candidates) == sorted([SID, other])
        with pytest.raises(UnknownSession):
            archive.session("ffff")


def test_session_lists_subagents_with_their_meta():
    append(transcript(), user("delegate it"))
    append(transcript(agent=AID), user("find the cursor", ts="2026-10-01T10:00:09.000Z"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor", toolUseId="toolu_77")
    with open_archive() as archive:
        archive.sweep()
        agents = archive.session(SID[:8]).agents
    assert agents == (Subagent(AID, "Find the cursor", "Explore", 1790848809000),)


def test_conversation_reads_the_archive_in_file_order_and_links_spawned_agents():
    append(
        transcript(),
        user("look into it"),
        assistant({"type": "thinking", "thinking": "hmm"}),
        tool_call("Agent", {"description": "Find the cursor"}, tool_use_id="toolu_77"),
        tool_result("done", tool_use_id="toolu_77"),
        reply("found it"),
    )
    append(transcript(agent=AID), user("subagent task"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor", toolUseId="toolu_77")
    with open_archive() as archive:
        archive.sweep()
        transcript().unlink()
        messages = [(m.kind, m.text, m.tool, m.spawned) for m in archive.conversation(SID)]
        assert [m.text for m in archive.conversation(SID, AID)] == ["subagent task"]
    assert messages == [
        (Kind.PROMPT, "look into it", None, None),
        (Kind.TOOL_CALL, 'Agent {"description":"Find the cursor"}', "Agent", AID),
        (Kind.TOOL_OUTPUT, "done", None, None),
        (Kind.REPLY, "found it", None, None),
    ]


def test_search_finds_a_session_whose_words_are_in_different_turns():
    append(transcript(), user("the zebracorn escaped", ts="2026-10-01T10:00:00.000Z"), reply("found it by the quokka"))
    append(transcript(SID2), user("only a zebracorn here"))
    with open_archive() as archive:
        archive.sweep()
        assert matching(archive, "zebracorn", "quokka") == [SID]
        assert matching(archive, "escaped zebracorn") == []


def test_search_every_word_must_match_an_eligible_entry():
    append(
        transcript(),
        user("the zebracorn escaped", ts="2026-10-01T10:00:00.000Z"),
        tool_result("quokka in the logs", ts="2026-10-20T10:00:00.000Z"),
        reply("a quokka after all", ts="2026-10-20T10:00:05.000Z"),
    )
    with open_archive() as archive:
        archive.sweep()
        assert matching(archive, "zebracorn", "logs") == []
        assert matching(archive, "zebracorn", "logs", tools=True) == [SID]
        assert matching(archive, "zebracorn", "quokka", filters=Filters(since=OCT_1 + 10 * 86_400_000)) == []
        assert matching(archive, "zebracorn", "quokka", filters=Filters(until=OCT_1 + 25 * 86_400_000)) == [SID]


def test_search_shows_the_entry_that_matches_the_most_words():
    append(transcript(), user("alpha"), reply("beta"), user("alpha and beta together"), reply("beta again"))
    with open_archive() as archive:
        archive.sweep()
        (hit,) = archive.search(Query(("alpha", "beta")))
    assert (hit.hits, hit.best.text) == (4, "alpha and beta together")
    assert [hit.best.text[s:e] for s, e in hit.best.marks] == ["alpha", "beta"]


def test_search_drops_words_without_letters_or_digits_and_rejects_a_query_of_only_those():
    append(transcript(), user("the quokka → the zebracorn"))
    with open_archive() as archive:
        archive.sweep()
        assert matching(archive, "quokka", "→", "*") == [SID]
        with pytest.raises(EmptyQuery):
            archive.search(Query(("→", "*", "")))


def test_a_slash_command_titles_a_session_as_typed_and_shell_lines_are_not_prompts():
    command = (
        "<command-message>pstack:arena</command-message>\n<command-name>/pstack:arena</command-name>\n"
        "<command-args>Find a good solution</command-args>"
    )
    append(
        transcript(),
        user(command),
        user("<bash-input>git status</bash-input>"),
        user("<bash-stdout>On branch main</bash-stdout><bash-stderr></bash-stderr>"),
        line({"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}),
    )
    with open_archive() as archive:
        archive.sweep()
        (summary,) = archive.sessions()
        assert (summary.title, summary.prompts) == ("/pstack:arena Find a good solution", 2)
        assert [(m.kind, m.text) for m in archive.conversation(SID)] == [
            (Kind.PROMPT, "/pstack:arena Find a good solution"),
            (Kind.PROMPT, "! git status"),
            (Kind.TOOL_OUTPUT, "<bash-stdout>On branch main</bash-stdout><bash-stderr></bash-stderr>"),
        ]
        assert found(archive, "branch") == 0
        assert found(archive, "interrupted") == 0


def test_a_title_entry_carries_the_title_lines_own_timestamp():
    append(
        transcript(),
        user("start", ts="2026-10-01T10:00:00.000Z"),
        ai_title("Quokka hunt"),
        line({"type": "ai-title", "aiTitle": "Zebracorn hunt", "timestamp": "2026-10-20T10:00:00.000Z"}),
    )
    with open_archive() as archive:
        archive.sweep()
        late = Filters(since=OCT_1 + 10 * 86_400_000)
        assert matching(archive, "zebracorn", filters=late) == [SID]
        assert matching(archive, "quokka", filters=Filters(until=OCT_1 + 86_400_000)) == [SID]
        assert archive.sessions()[0].last_ts == OCT_1 + 19 * 86_400_000 + 10 * 3_600_000


def test_project_filter_ignores_case_beyond_ascii():
    append(transcript(), user("hej", cwd="/Users/me/dev/första-projektet"))
    append(transcript(SID2), user("hej", cwd="/Users/me/dev/andra"))
    with open_archive() as archive:
        archive.sweep()
        assert [s.id for s in archive.sessions(Filters(project="FÖRSTA"))] == [SID]
        assert matching(archive, "hej", filters=Filters(project="Första-Projektet")) == [SID]


def test_search_prefers_an_excerpt_with_every_word_over_a_stronger_match_of_one():
    filler = " ".join(f"word{i}" for i in range(200))
    append(transcript(), user("agent agent agent agent"), reply(f"the launchd agent {filler}"))
    with open_archive() as archive:
        archive.sweep()
        (hit,) = archive.search(Query(("launchd", "agent")))
    assert hit.best.text.startswith("the launchd agent")


@pytest.mark.parametrize("read", ["sessions", "search"])
def test_titles_stay_right_while_a_rebuild_renumbers_entries_partway_through_a_read(monkeypatch, read):
    newer, older = transcript(SID2), transcript()
    with open_archive() as archive:
        append(newer, user("the newer quokka session", ts="2026-10-02T10:00:00.000Z"))
        archive.sweep()
        append(older, user("the older quokka session", ts="2026-10-01T10:00:00.000Z"))
        archive.sweep()
        append(newer, user("a later prompt in the newer", ts="2026-10-02T11:00:00.000Z"))
        archive.sweep()
    other, real_exists, armed = open_archive(), os.path.exists, [True]

    def exists(path):
        if armed[0] and path == str(newer):
            armed[0] = False
            other.rebuild(force=True)
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", exists)
    try:
        with open_archive() as archive:
            if read == "sessions":
                titles = {s.id: s.title for s in archive.sessions()}
            else:
                titles = {hit.session.id: hit.session.title for hit in archive.search(Query(("quokka",)))}
    finally:
        other.close()
    assert not armed[0]
    assert titles == {SID2: "the newer quokka session", SID: "the older quokka session"}


def test_status_reads_one_snapshot_while_a_sweep_commits_partway(monkeypatch):
    append(transcript(), user("archived before status"))
    with open_archive() as archive:
        before = archive.sweep()
    other, real_walk, armed = open_archive(), os.walk, [True]

    def walk(*args, **kwargs):
        if armed[0]:
            armed[0] = False
            append(transcript(SID2), user("archived while status reads"))
            other.sweep()
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    try:
        with open_archive() as archive:
            status = archive.status()
    finally:
        other.close()
    assert not armed[0]
    assert (status.transcripts, status.last_sweep) == (1, before)
