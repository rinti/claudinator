import base64

from claudinator.archive import Query, Subagent
from claudinator.conversation import Call, Hidden, Image, Key, Marker, Output, Said, Thought, build
from claudinator.record import Kind, Note, Notice, Picture
from helpers import AID, SID, append, assistant, line, open_archive, reply, tool_call, tool_result, transcript, user
from test_queries import write_meta

EXPLORE = Subagent(AID, "Find the cursor", "Explore", 1790848810000, "toolu_77", None)


def steps(agent: str | None = None, sweep: bool = True) -> list:
    with open_archive() as archive:
        if sweep:
            archive.sweep()
        session = archive.session(SID)
        return build(archive.lines(SID, agent), session.agents, agent)


def offsets(*chunks: bytes) -> list[int]:
    out, at = [], 0
    for chunk in chunks:
        out.append(at)
        at += len(chunk)
    return out


def test_steps_come_from_the_archive_in_file_order_and_an_agent_call_holds_its_subagent():
    lines = (
        user("look into it"),
        assistant({"type": "thinking", "thinking": "hmm"}),
        tool_call("Agent", {"description": "Find the cursor"}, tool_use_id="toolu_77"),
        tool_result("done", tool_use_id="toolu_77"),
        reply("found it"),
    )
    append(transcript(), *lines)
    append(transcript(agent=AID), user("subagent task", ts="2026-10-01T10:00:10.000Z"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor", toolUseId="toolu_77")
    with open_archive() as archive:
        archive.sweep()
    transcript().unlink()
    at = offsets(*lines)
    ts = 1790848806000
    done = Output(Key(at[3], 0), ts + 1000, "done", False, (), "toolu_77")
    assert steps(sweep=False) == [
        Said(Key(0, 0), 1790848800000, Kind.PROMPT, "look into it"),
        Thought(Key(at[1], 0), 1790848805000, "hmm"),
        Call(Key(at[2], 0), ts, "Agent", 'Agent {"description":"Find the cursor"}', '{"description":"Find the cursor"}', "toolu_77", EXPLORE, done),
        Said(Key(at[4], 0), 1790848805000, Kind.REPLY, "found it"),
    ]


def test_each_result_moves_under_the_call_it_answers_when_calls_run_in_parallel():
    append(
        transcript(),
        tool_call("Read", {"file_path": "a"}, tool_use_id="toolu_1"),
        tool_call("Bash", {"command": "b"}, tool_use_id="toolu_2"),
        tool_call("Grep", {"pattern": "c"}, tool_use_id="toolu_3"),
        tool_result("two", tool_use_id="toolu_2"),
        tool_result("three", tool_use_id="toolu_3"),
        tool_result("one", tool_use_id="toolu_1"),
    )
    assert [(type(s).__name__, s.tool, s.result.text) for s in steps()] == [
        ("Call", "Read", "one"),
        ("Call", "Bash", "two"),
        ("Call", "Grep", "three"),
    ]


def test_a_result_with_no_earlier_call_stays_where_it_is_and_a_blank_one_still_answers():
    lines = (
        tool_result("a fork's directive", tool_use_id="toolu_parent"),
        tool_call("Read", {}, tool_use_id="toolu_1"),
        tool_result("", tool_use_id="toolu_1"),
        tool_result("a second answer", tool_use_id="toolu_1"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    found = steps()
    assert [(type(s).__name__, s.key) for s in found] == [("Output", Key(0, 0)), ("Call", Key(at[1], 0)), ("Output", Key(at[3], 0))]
    assert (found[1].result.text, found[1].result.key) == ("", Key(at[2], 0))


def test_a_fork_does_not_point_at_itself():
    append(transcript(), tool_call("Agent", {"description": "fork"}, tool_use_id="toolu_77"))
    append(
        transcript(agent=AID),
        line({"type": "fork-context-ref", "agentId": AID}),
        tool_call("Agent", {"description": "fork"}, tool_use_id="toolu_77"),
        tool_result("the directive", tool_use_id="toolu_77"),
    )
    write_meta(SID, AID, toolUseId="toolu_77", isFork=True)
    (main,) = steps()
    _, fork = steps(AID)
    assert (main.spawned.id, fork.spawned, fork.result.text) == (AID, None, "the directive")


def test_a_step_has_the_address_a_search_hit_reports():
    append(transcript(), user("first"), reply("the plan"), assistant({"type": "text", "text": "x"}, {"type": "text", "text": "a quokka"}))
    with open_archive() as archive:
        archive.sweep()
        (hit,) = archive.search(Query(("quokka",)))
        found = build(archive.lines(SID), (), None)
    (said,) = [s for s in found if isinstance(s, Said) and s.key == Key(hit.best.line_off, hit.best.block)]
    assert said.text == "a quokka"


def test_lines_that_show_nothing_fold_into_one_marker_and_a_notice_shows_on_its_own():
    queued = {"type": "queued_command", "prompt": "also the tail", "origin": {"kind": "human"}}
    lines = (
        line({"type": "mode", "mode": "normal"}),
        user("hello"),
        line({"type": "file-history-snapshot"}),
        line({"type": "ai-title", "aiTitle": "Quokka hunt"}),
        b"not json\n",
        line({"type": "hologram"}),
        line({"type": "attachment", "attachment": queued}),
        assistant({"type": "thinking", "thinking": "", "signature": "x"}),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    assert [s for s in steps() if isinstance(s, Marker)] == [
        Marker(Key(0, 0), Hidden(((0, "mode"),))),
        Marker(Key(at[2], 0), Hidden(((at[2], "file-history-snapshot"), (at[3], "ai-title"), (at[4], "unparsed"), (at[5], "hologram")))),
        Marker(Key(at[6], 0), Note(Notice.QUEUED, "also the tail")),
        Marker(Key(at[7], 0), Hidden(((at[7], "assistant"),))),
    ]


PNG = base64.b64encode(b"\x89PNG zebracorn").decode()


def test_pasted_images_are_steps_and_a_results_images_stay_with_it():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    pasted = line({"type": "user", "message": {"content": [{"type": "text", "text": "see"}, image]}})
    result = line(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": [image]}]},
        }
    )
    append(transcript(), pasted, tool_call("Read", {}, tool_use_id="toolu_1"), result)
    said, picture, call = steps()
    assert (said.text, picture) == ("see", Image(Key(0, 1), None, Picture("image/png", None, 14)))
    assert (call.result.error, call.result.pictures) == (True, (Picture("image/png", 0, 14),))
