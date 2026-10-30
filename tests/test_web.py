import ast
import base64
import http.client
import json
import re
import sqlite3
import subprocess
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from claudinator import web
from claudinator.conversation import build
from claudinator.layout import Config
from claudinator.record import PROJECTION_VERSION
from helpers import (
    AID,
    SID,
    SID2,
    ai_title,
    append,
    assistant,
    line,
    open_archive,
    reply,
    settle,
    tool_call,
    tool_result,
    transcript,
    user,
)
from test_queries import write_meta

PNG = b"\x89PNG\r\n\x1a\n quokka pixels"


@pytest.fixture
def served():
    open_archive().close()
    server = web.bind(Config.from_env())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def get(server, target, *, prefix=None, host=None, headers=(), method="GET") -> tuple[int, dict[str, str], bytes]:
    sent = {"Host": host or f"127.0.0.1:{server.port}", **dict(headers)}
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request(method, (server.root[:-1] if prefix is None else prefix) + target, headers=sent)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def page(server, target) -> str:
    status, _, body = get(server, target)
    assert status == 200, body
    return body.decode()


def offsets(*chunks: bytes) -> list[int]:
    out, at = [], 0
    for chunk in chunks:
        out.append(at)
        at += len(chunk)
    return out


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.base = ""
        self.targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "base":
            self.base = found["href"]
        self.targets += [value for name, value in attrs if name in ("href", "src", "action")]


def test_every_link_on_every_page_resolves_under_the_secret_prefix_and_answers(served):
    pasted = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}
    append(
        transcript(),
        line({"type": "mode"}),
        user("delegate the quokka"),
        tool_call("Agent", {"description": "Find the cursor"}, tool_use_id="toolu_77"),
        line({"type": "user", "message": {"content": [pasted]}}),
    )
    append(transcript(agent=AID), user("find the cursor"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor", toolUseId="toolu_77")
    origin = f"http://127.0.0.1:{served.port}"
    crawled = set()
    for target in ("/", "/?q=quokka", f"/s/{SID}", f"/s/{SID}/{AID}", f"/s/{SID}?hidden=1"):
        links = Links()
        links.feed(page(served, target))
        document = origin + served.root + target[1:]
        for value in links.targets:
            if value.startswith("#"):
                continue
            resolved = urlsplit(urljoin(urljoin(document, links.base), value))
            assert (resolved.scheme, resolved.netloc) == ("http", f"127.0.0.1:{served.port}"), value
            assert resolved.path.startswith(served.root), value
            crawled.add(resolved.path + (f"?{resolved.query}" if resolved.query else ""))
    for target in crawled:
        status, headers, _ = get(served, target, prefix="")
        assert status in (200, 303), target
        assert headers.get("location", served.root).startswith(served.root), target
    assert {target.split("/")[2].partition("?")[0] for target in crawled} >= {"", "s", "raw", "image", "export", "web.css"}


def test_a_request_without_the_exact_prefix_gets_403_and_no_data(served):
    append(transcript(), user("the quokka line"))
    open_archive().sweep()
    token = served.root.strip("/")
    assert b"quokka" in get(served, f"/s/{SID}")[2]
    signed = {"Cookie": f"claudinator-{served.port}={token}"}
    for prefix in ("", f"/{'x' * len(token)}", f"/{token[:-1]}", f"/{token}x", f"/{token.swapcase()}"):
        for target in (f"/s/{SID}", f"/raw/{SID}/0", f"/export/{SID}", "/web.css", "/"):
            status, _, body = get(served, target, prefix=prefix, headers=signed)
            assert (status, b"quokka" in body, token.encode() in body) == (403, False, False), prefix + target
    assert get(served, served.root[:-1], prefix="")[0] == 403
    assert get(served, "/favicon.ico", prefix="")[:3:2] == (204, b"")


def test_no_response_sets_a_cookie_or_points_off_the_prefix(served):
    append(transcript(), user("delegate"), tool_call("Agent", {"description": "Find"}, tool_use_id="toolu_77"))
    printed = urlsplit(served.url)
    targets = (
        printed.path + (f"?{printed.query}" if printed.query else ""),
        f"{served.root}s/{SID}?call=toolu_77",
        f"{served.root}s/{SID}",
        f"{served.root}web.css",
        f"{served.root}nothing",
        "/",
        "/favicon.ico",
        f"/\\evil.example/?k={served.root.strip('/')}",
        f"//evil.example/{served.root}",
        f"{served.root}\\evil.example/",
    )
    answers = [get(served, target, prefix="") for target in targets]
    assert [status for status, _, _ in answers] == [200, 303, 200, 200, 404, 403, 204, 403, 403, 404]
    for target, (_, headers, _) in zip(targets, answers):
        assert "set-cookie" not in headers, target
        assert headers.get("location", served.root).startswith(served.root), target


def test_only_this_server_as_127_0_0_1_or_localhost_gets_an_answer(served):
    assert get(served, "/", host=f"evil.example:{served.port}")[0] == 403
    assert get(served, "/", host="127.0.0.1")[0] == 403
    assert get(served, "/", host=f"localhost:{served.port}")[0] == 200
    assert get(served, "/", headers={"Origin": "http://evil.example"})[0] == 403
    assert get(served, "/", headers={"Origin": f"http://localhost:{served.port}"})[0] == 200


def test_on_port_80_a_host_without_a_port_is_this_server(served, monkeypatch):
    monkeypatch.setattr(web.Server, "port", 80)
    for host in ("127.0.0.1", "localhost", "127.0.0.1:80", "localhost:80"):
        assert get(served, "/", host=host, headers={"Origin": f"http://{host}"})[0] == 200, host
    assert get(served, "/", host="evil.example")[0] == 403


def test_every_response_forbids_scripts_and_caching(served):
    pasted = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}
    append(transcript(), line({"type": "user", "message": {"content": [pasted]}}))
    open_archive().sweep()
    answers = {
        "page": get(served, f"/s/{SID}"),
        "stylesheet": get(served, "/web.css"),
        "favicon": get(served, "/favicon.ico", prefix=""),
        "raw": get(served, f"/raw/{SID}/0"),
        "image": get(served, f"/image/{SID}/0.0"),
        "download": get(served, f"/export/{SID}"),
        "refused": get(served, "/", prefix=""),
        "missing": get(served, f"/s/{SID2}"),
        "unsupported": get(served, "/", method="POST"),
    }
    assert {name: status for name, (status, _, _) in answers.items()} == {
        "page": 200,
        "stylesheet": 200,
        "favicon": 204,
        "raw": 200,
        "image": 200,
        "download": 200,
        "refused": 403,
        "missing": 404,
        "unsupported": 501,
    }
    for name, (_, headers, _) in answers.items():
        policy = headers["content-security-policy"]
        assert policy.startswith("default-src 'none';") and "script-src" not in policy, name
        assert (headers["x-content-type-options"], headers["cache-control"], headers["referrer-policy"]) == (
            "nosniff",
            "no-store",
            "no-referrer",
        ), name
    assert all(answers[name][1]["content-security-policy"].endswith("; sandbox") for name in ("raw", "image", "download"))
    assert (answers["favicon"][2], "content-length" in answers["favicon"][1]) == (b"", False)


def test_a_transcript_shows_each_result_inside_the_call_it_answers(served):
    lines = (
        user("read both"),
        tool_call("Read", {"file_path": "a"}, tool_use_id="toolu_1"),
        tool_call("Bash", {"command": "false"}, tool_use_id="toolu_2"),
        line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True, "content": "exit 1"}]}}),
        tool_result("one", tool_use_id="toolu_1"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    shown = page(served, f"/s/{SID}")
    read, bash = (re.search(rf'<div class="step call[^"]*" id="m{at[n]}\.0">.*?<a class="raw" href="raw/{SID}/{at[n]}">', shown, re.S)[0] for n in (1, 2))
    assert f'<div class="output" id="m{at[4]}.0"><pre>one</pre>' in read
    assert f'<div class="output error" id="m{at[3]}.0"><pre>exit 1</pre>' in bash
    assert bash.startswith('<div class="step call error"') and '<span class="size">error</span>' in bash


def test_a_call_is_one_row_with_a_short_output_below_it_and_a_long_one_inside(served):
    lines = (
        user("go"),
        tool_call("Bash", {"command": "echo hi"}, tool_use_id="toolu_1"),
        tool_result("hi", tool_use_id="toolu_1"),
        tool_call("Bash", {"command": "seq 4"}, tool_use_id="toolu_2"),
        tool_result("1\n2\n3\nquokka", tool_use_id="toolu_2"),
        tool_call("Bash", {"command": "true"}, tool_use_id="toolu_3"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    given = "<pre>{\n  &quot;command&quot;: &quot;%s&quot;\n}</pre>"
    shown = page(served, f"/s/{SID}")
    assert (
        f'<div class="step call" id="m{at[1]}.0"><details><summary><b>Bash</b> <span class="gist">echo hi</span> <span class="size">1 line</span></summary>'
        f'{given % "echo hi"}</details><div class="output" id="m{at[2]}.0"><pre>hi</pre>'
    ) in shown
    assert (
        f'<div class="step call" id="m{at[3]}.0"><details><summary><b>Bash</b> <span class="gist">seq 4</span> <span class="size">4 lines</span></summary>'
        f'{given % "seq 4"}<div class="output" id="m{at[4]}.0"><pre>1\n2\n3\nquokka</pre>'
    ) in shown
    assert f'<div class="step call" id="m{at[5]}.0"><details><summary><b>Bash</b> <span class="gist">true</span> <span class="size">no output</span></summary>' in shown
    landed = page(served, f"/s/{SID}?at={at[4]}.0&q=quokka")
    assert f'<div class="step call" id="m{at[3]}.0"><details open>' in landed and "<pre>1\n2\n3\n<mark>quokka</mark></pre>" in landed
    assert f'<div class="step call" id="m{at[1]}.0"><details>' in landed


def test_a_call_sums_up_its_input_by_its_first_string_field(served):
    calls = (
        ("Bash", {"command": "ls -la\n   /tmp", "description": "List tmp"}),
        ("Read", {"limit": 5, "file_path": "/a/b.py"}),
        ("Agent", {"description": "", "prompt": "Find the cursor"}),
        ("Grep", {"pattern": "x" * 200}),
        ("TodoWrite", {"todos": [{"content": "ship"}], "merge": True}),
    )
    append(transcript(), user("go"), *(tool_call(name, given, tool_use_id=f"toolu_{i}") for i, (name, given) in enumerate(calls)))
    summaries = re.findall(r'<summary><b>(.*?)</b> <span class="gist">(.*?)</span>', page(served, f"/s/{SID}"))
    assert summaries == [
        ("Bash", "ls -la /tmp"),
        ("Read", "/a/b.py"),
        ("Agent", "Find the cursor"),
        ("Grep", "x" * 159 + "…"),
        ("TodoWrite", "{&quot;todos&quot;:[{&quot;content&quot;:&quot;ship&quot;}],&quot;merge&quot;:true}"),
    ]
    assert '<pre>{\n  &quot;command&quot;: &quot;ls -la\\n   /tmp&quot;,\n  &quot;description&quot;: &quot;List tmp&quot;\n}</pre>' in page(served, f"/s/{SID}")


def test_a_commands_output_shows_without_its_wrapper_tags_and_marks_land_on_the_words(served):
    lines = (
        user("<bash-input>uv sync</bash-input>"),
        user("<bash-stdout>Resolved 1 quokka</bash-stdout><bash-stderr>warning: a wombat</bash-stderr>"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)[1]
    assert f'href="s/{SID}?at={at}.0&amp;q=quokka+wombat#m{at}.0"' in page(served, "/?q=quokka+wombat&tools=on")
    shown = page(served, f"/s/{SID}")
    assert (
        f'<div class="step output" id="m{at}.0"><pre>Resolved 1 quokka</pre>'
        '<div class="stderr"><span class="label">stderr</span><pre>warning: a wombat</pre></div>'
    ) in shown
    assert "bash-std" not in shown
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka+wombat")
    assert "<pre>Resolved 1 <mark>quokka</mark></pre>" in landed
    assert "<pre>warning: a <mark>wombat</mark></pre>" in landed


def test_a_task_notification_is_summed_up_by_its_summary(served):
    note = "<task-notification><task-id>b1</task-id><summary>Build finished</summary></task-notification>"
    append(transcript(), user("go"), user(note, origin={"kind": "task-notification"}))
    assert "<summary>A notification came in: Build finished</summary>" in page(served, f"/s/{SID}")


def test_a_notice_is_a_sentence_that_quotes_its_text_where_the_text_adds_to_it(served):
    queued = {"type": "queued_command", "prompt": "also the quokka", "origin": {"kind": "human"}}
    lines = (
        user("go"),
        line({"type": "attachment", "attachment": queued}),
        user("[Request interrupted by user]"),
        line({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"}),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    shown = page(served, f"/s/{SID}")
    assert f'<div class="step notice queued" id="m{at[1]}.0">You queued a prompt while Claude worked: <span class="text">also the quokka</span><a ' in shown
    assert f'<div class="step notice interrupted" id="m{at[2]}.0">You interrupted Claude.<a ' in shown
    assert f'<div class="step notice compacted" id="m{at[3]}.0">The conversation was compacted.<a ' in shown


def test_an_agent_call_links_its_subagent_and_the_subagent_links_back(served):
    nested = "b0b0b0b0b0b0b0b0b"
    main = (user("delegate"), tool_call("Agent", {"description": "Find the cursor"}, tool_use_id="toolu_77"))
    child = (user("find the cursor"), tool_call("Agent", {"description": "Look deeper"}, tool_use_id="toolu_88"))
    append(transcript(), *main)
    append(transcript(agent=AID), *child)
    append(transcript(agent=nested), user("look deeper"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor", toolUseId="toolu_77")
    write_meta(SID, nested, agentType="Explore", description="Look deeper", toolUseId="toolu_88", parentAgentId=AID)
    call, inner = offsets(*main)[1], offsets(*child)[1]
    shown = page(served, f"/s/{SID}")
    assert f'<li><span class="id">a7c9c203</span><a href="s/{SID}/{AID}">Find the cursor</a><span class="type">Explore</span></li>' in shown
    assert f'<div class="step call" id="m{call}.0"><details><summary><b>Agent</b> <span class="gist">Find the cursor</span></summary>' in shown
    assert f'</details><a class="spawned" href="s/{SID}/{AID}">subagent a7c9c203</a>' in shown
    started = f'<a href="s/{SID}">delegate</a>. <a href="s/{SID}?call=toolu_77">See the call that started it</a>.'
    assert f"<h1>Find the cursor</h1><p>Subagent a7c9c203, type Explore, in {started}</p>" in page(served, f"/s/{SID}/{AID}")
    assert get(served, f"/s/{SID}?call=toolu_77")[1]["location"] == f"{served.root}s/{SID}?at={call}.0#m{call}.0"
    assert f'<a href="s/{SID}/{AID}?call=toolu_88">See the call that started it</a>' in page(served, f"/s/{SID}/{nested}")
    assert get(served, f"/s/{SID}/{AID}?call=toolu_88")[1]["location"] == f"{served.root}s/{SID}/{AID}?at={inner}.0#m{inner}.0"


def test_a_transcript_shows_the_time_once_a_minute_and_a_heading_where_the_day_changes(served):
    lines = (
        user("first", ts="2026-10-29T22:38:05Z"),
        reply("same minute", ts="2026-10-29T22:38:50Z"),
        line({"type": "system", "subtype": "api_error", "error": {"message": "overloaded"}}),
        reply("next minute", ts="2026-10-29T22:39:01Z"),
        user("after midnight", ts="2026-10-30T00:01:00Z"),
        user("new year", ts="2027-01-01T09:30:00Z"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    main = page(served, f"/s/{SID}").partition("<main>")[2]
    found = re.findall(r'<h2 class="day">(.*?)</h2>|<time>(.*?)</time>|<div class="step [^"]*" id="m(\d+)\.0"', main)
    assert [day or time or int(step) for day, time, step in found] == [
        "Thursday 29 October 2026", "22:38", at[0], at[1], at[2], "22:39", at[3],
        "Friday 30 October", "00:01", at[4],
        "Friday 1 January 2027", "09:30", at[5],
    ]


def test_a_transcript_header_says_where_and_when_how_much_and_how_to_resume(served):
    append(transcript(), user("hello", ts="2026-10-29T22:38:00Z"), reply("hi", ts="2026-10-30T10:59:00Z"))
    append(transcript(agent=AID), user("subagent", ts="2026-10-29T23:00:00Z"))
    append(transcript(SID2), user("short", ts="2026-10-29T08:00:00Z"), reply("done", ts="2026-10-29T08:20:00Z"))
    shown = page(served, f"/s/{SID}")
    assert "<p>In /Users/me/dev/demo, 29 Oct 22:38 to 30 Oct 10:59.</p><p>1 prompt and 1 subagent. Resume it with <code>claudinator resume 8f2d43fe</code></p>" in shown
    transcript(SID2).unlink()
    gone = page(served, f"/s/{SID2}")
    assert "<p>In /Users/me/dev/demo, 29 Oct 08:00 to 08:20.</p><p>1 prompt. Claude Code has deleted its copy, and resuming puts it back. Resume it with " in gone


def test_the_list_groups_sessions_by_day_and_reads_time_title_project_and_counts(served):
    sid3 = "3c0ffee0-0000-4000-8000-000000000003"
    append(transcript(), user("hello", ts="2026-10-01T10:00:00Z"))
    append(transcript(agent=AID), user("subagent", ts="2026-10-01T10:00:00Z"))
    elsewhere = {"cwd": "/tmp/elsewhere"}
    append(transcript(SID2, slug="-tmp-elsewhere"), user("again", ts="2026-10-02T09:00:00Z", **elsewhere), user("more", ts="2026-10-02T09:30:00Z", **elsewhere))
    append(transcript(sid3), user("early", ts="2026-10-02T08:00:00Z"))
    shown = page(served, "/")
    assert shown.index('<p class="count">3 sessions</p>') < shown.index('<h2 class="day">')
    assert re.findall(r'<h2 class="day">(.*?)</h2>|<time>(.*?)</time>', shown) == [
        ("Friday 2 October 2026", ""), ("", "09:30"), ("", "08:00"), ("Thursday 1 October", ""), ("", "10:00"),
    ]
    assert '</ol><h2 class="day">Thursday 1 October</h2><ol class="sessions">' in shown
    assert (
        f'<li><time>10:00</time><span class="title"><a href="s/{SID}">hello</a></span>'
        '<a class="project" href="./?project=%2FUsers%2Fme%2Fdev%2Fdemo" title="/Users/me/dev/demo">/Users/me/dev/demo</a>'
        '<span class="count">1 prompt</span><span class="count">1 subagent</span></li>'
    ) in shown
    assert '>/tmp/elsewhere</a><span class="count">2 prompts</span><span class="count"></span></li>' in shown


def test_a_search_hit_says_when_and_where_and_where_its_best_hit_is(served):
    append(transcript(), user("first", ts="2026-10-01T10:00:00Z"), reply("the quokka line", ts="2026-10-01T10:05:00Z"), reply("another quokka", ts="2026-10-01T10:06:00Z"))
    append(transcript(agent=AID), user("a wombat in a subagent", ts="2026-10-01T10:06:00Z"))
    append(transcript(SID2), user("nothing to see", ts="2026-10-02T08:00:00Z"), ai_title("Zebracorn hunt"))
    assert "<p>10:06 on 1 Oct 2026 in /Users/me/dev/demo, 2 hits, best in a reply</p>" in page(served, "/?q=quokka")
    assert "<p>10:06 on 1 Oct 2026 in /Users/me/dev/demo, 1 hit, in a prompt in subagent a7c9c203</p>" in page(served, "/?q=wombat")
    assert "<p>08:00 on 2 Oct 2026 in /Users/me/dev/demo, 1 hit, in the title</p>" in page(served, "/?q=zebracorn")


def test_a_search_hit_opens_its_transcript_at_the_matching_step_with_the_words_marked(served):
    lines = (user("first"), reply("the quokka line"))
    append(transcript(), *lines)
    append(transcript(agent=AID), user("a wombat in a subagent"))
    append(transcript(SID2), user("nothing to see"), ai_title("Zebracorn hunt"))
    at = offsets(*lines)[1]
    found = page(served, "/?q=quokka")
    assert f'href="s/{SID}?at={at}.0&amp;q=quokka#m{at}.0"' in found
    assert f'id="m{at}.0"><div class="text">the <mark>quokka</mark> line</div>' in page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert f'href="s/{SID}/{AID}?at=0.0&amp;q=wombat#m0.0"' in page(served, "/?q=wombat")
    assert f'<a class="excerpt" href="s/{SID2}">' in page(served, "/?q=zebracorn")


def test_lines_that_show_nothing_fold_into_one_marker_with_a_raw_link_each(served):
    lines = (line({"type": "mode", "mode": "normal"}), line({"type": "file-history-snapshot"}), user("hello"))
    append(transcript(), *lines)
    at = offsets(*lines)
    assert (
        '<details class="step hidden" id="m0.0"><summary>2 hidden lines: 1 mode, 1 file-history-snapshot</summary>'
        f'<ul><li><a href="raw/{SID}/0">mode</a></li><li><a href="raw/{SID}/{at[1]}">file-history-snapshot</a></li></ul>'
        "</details>"
    ) in page(served, f"/s/{SID}?hidden=1")


def test_hidden_lines_stay_out_of_the_way_until_asked_for_and_notices_always_show(served):
    api_error = line({"type": "system", "subtype": "api_error", "error": {"message": "overloaded"}})
    lines = (line({"type": "mode"}), user("hello"), api_error, line({"type": "mode"}), reply("the quokka line"), line({"type": "mode"}))
    append(transcript(), *lines)
    at = offsets(*lines)[4]
    shown = page(served, f"/s/{SID}")
    assert 'class="step hidden"' not in shown
    error = 'The API answered with an error: <span class="text">overloaded</span>'
    assert error in shown
    assert f'<a href="s/{SID}?hidden=1">Show 3 hidden lines</a></p>' in shown
    revealed = page(served, f"/s/{SID}?hidden=1")
    assert revealed.count('class="step hidden"') == 3
    assert error in revealed
    assert f'<a href="s/{SID}">Hide hidden lines</a></p>' in revealed
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert f'<a href="s/{SID}?at={at}.0&amp;q=quokka&amp;hidden=1#m{at}.0">Show 3 hidden lines</a>' in landed
    revealed = page(served, f"/s/{SID}?at={at}.0&q=quokka&hidden=1")
    assert f'<a href="s/{SID}?at={at}.0&amp;q=quokka#m{at}.0">Hide hidden lines</a>' in revealed
    assert "the <mark>quokka</mark> line" in revealed


def test_a_raw_line_is_its_json_in_ascii(served):
    said = user("café ‮gpj.exe")
    append(transcript(), said, b"torn \x1b[2J \xe2\x80\xae line\n")
    open_archive().sweep()
    status, headers, body = get(served, f"/raw/{SID}/0")
    assert (status, headers["content-type"]) == (200, "text/plain; charset=utf-8")
    assert body.isascii() and json.loads(body) == json.loads(said)
    assert get(served, f"/raw/{SID}/{len(said)}")[2] == b"torn \\x1b[2J \\u202e line"
    assert get(served, f"/raw/{SID}/1")[0] == 404


def test_a_record_glued_onto_a_settled_torn_tail_has_its_raw_line_and_image(served):
    before, torn = user("before the crash"), user("torn by a crash")[:30]
    pasted = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}
    glued = line({"type": "user", "message": {"content": [{"type": "text", "text": "see"}, pasted]}})
    append(transcript(), before, torn)
    settle(transcript())
    open_archive().sweep()
    append(transcript(), glued)
    open_archive().sweep()
    at = len(before) + len(torn)
    shown = page(served, f"/s/{SID}?hidden=1")
    assert f'href="raw/{SID}/{at}"' in shown and f'src="image/{SID}/{at}.1"' in shown
    status, _, body = get(served, f"/raw/{SID}/{at}")
    assert status == 200
    assert json.loads(body) == json.loads(glued)
    assert get(served, f"/image/{SID}/{at}.1")[::2] == (200, PNG)
    assert get(served, f"/raw/{SID}/{len(before)}")[::2] == (200, torn)
    assert get(served, f"/raw/{SID}/{at + 1}")[0] == 404


def test_images_are_served_only_as_raster_types(served):
    data = base64.b64encode(PNG).decode()
    lines = [
        line({"type": "user", "message": {"content": [{"type": "text", "text": "see"}, {"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}}]}})
        for kind in ("image/png", "image/svg+xml")
    ]
    append(transcript(), *lines)
    open_archive().sweep()
    status, headers, body = get(served, f"/image/{SID}/0.1")
    assert (status, headers["content-type"], body) == (200, "image/png", PNG)
    assert get(served, f"/image/{SID}/{len(lines[0])}.1")[0] == 404


def test_the_download_is_the_archived_bytes(served):
    append(transcript(), user("main"))
    append(transcript(agent=AID), user("subagent"))
    open_archive().sweep()
    status, headers, body = get(served, f"/export/{SID}/{AID}")
    assert (status, body) == (200, transcript(agent=AID).read_bytes())
    assert headers["content-disposition"] == f'attachment; filename="{SID}-{AID}.jsonl"'


def test_a_page_archives_new_bytes_first_without_counting_as_the_sweeper(served):
    append(transcript(), user("archived by the sweeper"))
    with open_archive() as archive:
        archive.sweep(wait=True)
        before = archive.status().last_sweep
    append(transcript(), user("a zebracorn written since"))
    assert "a zebracorn written since" in page(served, f"/s/{SID}")
    with open_archive() as archive:
        assert archive.status().last_sweep == before


def test_a_server_left_running_across_an_upgrade_asks_for_a_restart_and_changes_nothing(served):
    db = Config.from_env().db
    with sqlite3.connect(db) as stamp:
        stamp.execute("UPDATE meta SET value = ? WHERE key = 'projection_version'", (str(PROJECTION_VERSION + 1),))
    status, _, body = get(served, "/")
    assert (status, b"restart serve" in body) == (500, True)
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT value FROM meta WHERE key = 'projection_version'").fetchone() == (str(PROJECTION_VERSION + 1),)


def test_the_server_writes_no_request_log(served, capfd):
    append(transcript(), user("quokka"))
    get(served, "/?q=quokka")
    get(served, "/s/" + SID, prefix="/" + "x" * 43)
    get(served, "/missing")
    get(served, "/", method="POST")
    assert capfd.readouterr() == ("", "")


HOSTILE = "run <script>alert(1)</script> and <img src=x onerror=alert(2)> on \x1b[31mred ‮gpj.exe"


def test_transcript_text_is_text_whatever_it_holds():
    picture = {"type": "image", "source": {"type": "base64", "media_type": 'x" onerror="alert(3)', "data": "AAAA"}}
    append(
        transcript(),
        user(HOSTILE),
        ai_title("Invoice ‮gpj.exe <b>"),
        tool_call("<b>Tool", {"x": "</pre><script>"}),
        line({"type": "user", "message": {"content": [picture]}}),
    )
    with open_archive() as archive:
        archive.sweep()
        session = archive.session(SID)
        shown = web.transcript_page("/T/", session, None, build(archive.lines(SID), session.agents, None), None, "", False, None)
    assert (
        '<div class="text">run &lt;script&gt;alert(1)&lt;/script&gt; and &lt;img src=x onerror=alert(2)&gt; on '
        '<span class="esc">\\x1b</span>[31mred <span class="esc">\\u202e</span>gpj.exe</div>'
    ) in shown
    assert "<title>Invoice \\u202egpj.exe &lt;b&gt;</title>" in shown
    assert '<summary><b>&lt;b&gt;Tool</b> <span class="gist">&lt;/pre&gt;&lt;script&gt;</span> <span class="size">no output</span></summary>' in shown
    assert 'alt="image, x&quot; onerror=&quot;alert(3), 3 B"' in shown
    assert "<script" not in shown and "<img src=x" not in shown and "‮" not in shown and "\x1b" not in shown


def test_only_text_el_and_marked_make_markup():
    tree = ast.parse(Path(web.__file__).read_text())
    builders = [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name in ("text", "el", "marked")]
    allowed = {id(node) for fn in builders for node in ast.walk(fn)}
    makers = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Html" and id(node) not in allowed
    ]
    assert (len(builders), makers) == (3, []), "build markup with el() and text(), which escape what they are given"


def test_what_was_typed_into_the_form_comes_back_as_text():
    shown = web.sessions_page("/T/", (), web.Form(q='"><script>alert(4)</script>', project="‮x"), None, "since: <b>")
    assert 'value="&quot;&gt;&lt;script&gt;alert(4)&lt;/script&gt;"' in shown
    assert 'name="project" value="\\u202ex"' in shown
    assert '<p class="banner">since: &lt;b&gt;</p>' in shown


CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.handlers += [name for name, _ in attrs if name.startswith("on")]


@pytest.mark.skipif(not CHROME.exists(), reason="needs Google Chrome in /Applications")
def test_a_real_browser_renders_a_hostile_transcript_as_text_and_refuses_nothing(served, tmp_path):
    append(transcript(), user(HOSTILE))
    signed = f"{served.url}s/{SID}"
    flags = ["--headless=new", "--disable-gpu", "--no-first-run", "--use-mock-keychain", f"--user-data-dir={tmp_path}"]
    browser = [CHROME, *flags, "--enable-logging=stderr", "--v=0", "--dump-dom", signed]
    run = subprocess.run(browser, capture_output=True, text=True, timeout=60)
    dom = run.stdout
    assert [entry for entry in run.stderr.splitlines() if ":CONSOLE" in entry] == []
    found = Elements()
    found.feed(dom)
    assert (found.tags.count("main"), found.tags.count("script"), found.handlers) == (1, 0, [])
    assert "run &lt;script&gt;alert(1)&lt;/script&gt; and &lt;img src=x onerror=alert(2)&gt;" in dom
    assert '<span class="esc">\\u202e</span>gpj.exe' in dom
