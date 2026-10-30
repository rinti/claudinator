import ast
import base64
import http.client
import json
import re
import sqlite3
import subprocess
import threading
from datetime import UTC, date, datetime
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
    assert {target.split("/")[2].partition("?")[0] for target in crawled} >= {"", "s", "raw", "image", "export", "web.css", "fonts"}


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
        "font": get(served, "/fonts/Geist-Variable.woff2"),
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
        "font": 200,
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


def test_the_stylesheet_loads_its_bundled_fonts_and_the_page_policy_lets_it(served):
    css = get(served, "/web.css")[2].decode()
    fonts = re.findall(r"url\(([^)]*)\)", css)
    assert fonts == ["fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2"]
    for name in fonts:
        status, headers, body = get(served, "/" + name)
        assert (status, headers["content-type"], body[:4]) == (200, "font/woff2", b"wOF2"), name
        assert body == (Path(web.__file__).parent / name).read_bytes(), name
    assert "font-src 'self'" in get(served, "/")[1]["content-security-policy"].split("; ")
    assert get(served, "/fonts/Inter-Variable.woff2")[0] == 404


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
    read, bash = (re.search(rf'<div class="step call[^"]*" id="m{at[n]}\.0">.*?</details></div>', shown, re.S)[0] for n in (1, 2))
    assert f'<div class="output" id="m{at[4]}.0"><pre>one</pre>' in read
    assert f'<div class="output error" id="m{at[3]}.0"><pre>exit 1</pre>' in bash
    assert bash.startswith('<div class="step call error"') and '<span class="size error">error</span>' in bash


def test_a_call_is_one_closed_row_that_opens_to_its_input_and_then_its_output(served):
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

    def row(n: int, given: str, size: str, opened: str = "") -> str:
        return (
            f'<div class="step call" id="m{at[n]}.0"><details{opened}><summary><b>Bash</b> <span class="gist">{given}</span>'
            f'<span class="size">{size}</span></summary><div class="panel">'
            f'<pre class="input">{{\n  &quot;command&quot;: &quot;{given}&quot;\n}}</pre><a class="raw" href="raw/{SID}/{at[n]}">raw</a>'
        )

    shown = page(served, f"/s/{SID}")
    assert row(1, "echo hi", "1 line") + f'<div class="output" id="m{at[2]}.0"><pre>hi</pre>' in shown
    assert row(3, "seq 4", "4 lines") + f'<div class="output" id="m{at[4]}.0"><pre>1\n2\n3\nquokka</pre>' in shown
    assert row(5, "true", "no output") + "</div></details></div>" in shown
    landed = page(served, f"/s/{SID}?at={at[4]}.0&q=quokka")
    assert row(3, "seq 4", "4 lines", " open") in landed and "<pre>1\n2\n3\n<mark>quokka</mark></pre>" in landed
    assert row(1, "echo hi", "1 line") in landed


def test_calls_outputs_and_thoughts_of_one_minute_share_a_box_until_something_else_comes(served):
    thinking = assistant({"type": "thinking", "thinking": "first idea\nsecond idea"}, ts="2026-10-01T10:00:20.000Z")
    lines = (
        user("go", ts="2026-10-01T10:00:00.000Z"),
        tool_call("Bash", {"command": "a"}, tool_use_id="toolu_1", ts="2026-10-01T10:00:10.000Z"),
        tool_result("a", tool_use_id="toolu_1", ts="2026-10-01T10:00:11.000Z"),
        thinking,
        user("<bash-stdout>printed</bash-stdout>", ts="2026-10-01T10:00:30.000Z"),
        reply("between", ts="2026-10-01T10:00:40.000Z"),
        tool_call("Bash", {"command": "b"}, tool_use_id="toolu_2", ts="2026-10-01T10:00:50.000Z"),
        tool_call("Bash", {"command": "c"}, tool_use_id="toolu_3", ts="2026-10-01T10:01:05.000Z"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)
    steps = page(served, f"/s/{SID}").partition('<div class="steps">')[2]
    found = re.findall(r'<div class="(box|minute)">|<(?:div|details) class="step (\w+)[^"]*" id="m(\d+)\.0"', steps)
    assert [kind or f"{name}@{at.index(int(off))}" for kind, name, off in found] == [
        "minute", "prompt@0", "box", "call@1", "thought@3", "output@4", "reply@5", "box", "call@6",
        "minute", "box", "call@7",
    ]
    assert '<summary><b>Thinking</b> <span class="gist">first idea</span><span class="size">2 lines</span></summary>' in steps
    assert '<summary><b>Output</b> <span class="gist">printed</span><span class="size">1 line</span></summary>' in steps


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
    assert '<pre class="input">{\n  &quot;command&quot;: &quot;ls -la\\n   /tmp&quot;,\n  &quot;description&quot;: &quot;List tmp&quot;\n}</pre>' in page(served, f"/s/{SID}")


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
        f'<div class="step output" id="m{at}.0"><details><summary><b>Output</b> <span class="gist">Resolved 1 quokka</span>'
        '<span class="size">2 lines</span></summary><div class="panel"><pre>Resolved 1 quokka</pre>'
        '<div class="stderr"><span class="label">stderr</span><pre>warning: a wombat</pre></div>'
    ) in shown
    assert "bash-std" not in shown
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka+wombat")
    assert f'<div class="step output" id="m{at}.0"><details open>' in landed
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
    assert f'<div class="step note queued" id="m{at[1]}.0">You queued a prompt while Claude worked: <span class="text">also the quokka</span><a ' in shown
    assert f'<div class="step note interrupted" id="m{at[2]}.0">You interrupted Claude.<a ' in shown
    assert f'<div class="step note compacted" id="m{at[3]}.0">The conversation was compacted.<a ' in shown


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
    listed = f'<li><a href="s/{SID}/{AID}"><span class="name">Find the cursor</span><span class="id">a7c9c203</span><span class="type">Explore</span></a></li>'
    assert listed in shown
    assert f'<div class="step call spawns" id="m{call}.0"><details><summary><b>Agent</b> <span class="gist">Find the cursor</span></summary>' in shown
    assert f'</details><a class="spawned" href="s/{SID}/{AID}">↳ a7c9c203</a></div>' in shown
    subagent = page(served, f"/s/{SID}/{AID}")
    assert f'<a class="back" href="s/{SID}"><span aria-hidden="true">←</span>delegate</a>' in subagent
    started = f'<p>In <a href="s/{SID}">delegate</a></p><p><a href="s/{SID}?call=toolu_77">See the call that started it</a></p>'
    assert f'<h2>Subagent</h2><p class="kind">Explore</p>{started}' in subagent
    assert get(served, f"/s/{SID}?call=toolu_77")[1]["location"] == f"{served.root}s/{SID}?at={call}.0#m{call}.0"
    assert f'<a href="s/{SID}/{AID}?call=toolu_88">See the call that started it</a>' in page(served, f"/s/{SID}/{nested}")
    assert get(served, f"/s/{SID}/{AID}?call=toolu_88")[1]["location"] == f"{served.root}s/{SID}/{AID}?at={inner}.0#m{inner}.0"


def test_the_back_link_returns_to_the_results_a_page_was_opened_from(served):
    lines = (user("first"), reply("the quokka line"))
    append(transcript(), *lines)
    append(transcript(agent=AID), user("a quokka in a subagent"))
    write_meta(SID, AID, agentType="Explore", description="Find the cursor")
    at = offsets(*lines)[1]
    back = r'<a class="back" href="([^"]*)"><span aria-hidden="true">←</span>(.*?)</a>'
    assert re.findall(back, page(served, f"/s/{SID}")) == [("./", "All sessions")]
    assert re.findall(back, page(served, f"/s/{SID}?at={at}.0&q=quokka+%22a+line%22")) == [
        ("./?q=quokka+%22a+line%22", "Results for “quokka &quot;a line&quot;”")
    ]
    assert re.findall(back, page(served, f"/s/{SID}/{AID}")) == [(f"s/{SID}", "first")]
    assert re.findall(back, page(served, f"/s/{SID}/{AID}?at=0.0&q=quokka")) == [("./?q=quokka", "Results for “quokka”")]


def test_prompts_are_numbered_from_the_start_of_their_own_transcript(served):
    append(transcript(), user("one"), reply("ok"), user("/clear"), tool_call("Bash", {"command": "x"}), user("three\nfour\nfive\nsix"))
    append(transcript(agent=AID), user("the subagent's own first"))
    labels = r'<p class="label">(.*?)</p><div class="text">(.*?)</div>'
    assert re.findall(labels, page(served, f"/s/{SID}"), re.S) == [
        ("Prompt 1", "one"),
        ("Prompt 2", '<span class="cmd">/clear</span>'),
        ("Prompt 3", "three\nfour\nfive\nsix"),
    ]
    assert re.findall(r'<div class="(step prompt[^"]*)"', page(served, f"/s/{SID}")) == ["step prompt"] * 2 + ["step prompt long"]
    assert re.findall(labels, page(served, f"/s/{SID}/{AID}")) == [("Prompt 1", "the subagent&#x27;s own first")]


def test_prose_sets_fenced_and_inline_code_as_code_and_leaves_a_stray_backtick_as_it_is(served):
    said = 'Run `uv sync` then:\n```bash\necho "<b>quokka</b>"\n```\nA stray ` stays.\nSo does `this\none`.'
    lines = (user("/pstack:poteto-mode fix `web.py`"), reply(said))
    append(transcript(), *lines)
    at = offsets(*lines)[1]
    shown = page(served, f"/s/{SID}")
    assert '<div class="text"><span class="cmd">/pstack:poteto-mode</span>fix <code>web.py</code></div>' in shown
    assert (
        '<div class="text">Run <code>uv sync</code> then:\n<pre class="code">echo &quot;&lt;b&gt;quokka&lt;/b&gt;&quot;</pre>'
        "A stray ` stays.\nSo does `this\none`.</div>"
    ) in shown
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka+sync")
    assert 'Run <code>uv <mark>sync</mark></code> then:\n<pre class="code">echo &quot;&lt;b&gt;<mark>quokka</mark>&lt;/b&gt;&quot;</pre>' in landed


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
    steps = page(served, f"/s/{SID}").partition('<div class="steps">')[2]
    found = re.findall(r'<h2 class="day">(.*?)</h2>|<time>(.*?)</time>|<div class="step [^"]*" id="m(\d+)\.0"', steps)
    assert [day or time or int(step) for day, time, step in found] == [
        "Thursday 29 October 2026", "22:38", at[0], at[1], at[2], "22:39", at[3],
        "Friday 30 October 2026", "00:01", at[4],
        "Friday 1 January 2027", "09:30", at[5],
    ]


def test_a_transcript_says_where_when_how_long_how_much_and_how_to_resume(served):
    append(transcript(), user("hello", ts="2026-10-29T22:38:00Z"), reply("hi", ts="2026-10-30T10:59:00Z"))
    append(transcript(agent=AID), user("subagent", ts="2026-10-29T23:00:00Z"))
    append(transcript(SID2), user("short", ts="2026-10-29T08:00:00Z"), reply("done", ts="2026-10-29T08:20:00Z"))
    where = '<span><span class="dir">/Users/me/dev/</span><span class="leaf">demo</span></span>'
    shown = page(served, f"/s/{SID}")
    assert (
        f'<p class="facts">{where}<span>Thu 29 Oct · 22:38 – Fri 30 Oct · 10:59</span><span class="faint">12h 21m</span>'
        "<span>1 prompt</span><span>1 subagent</span></p>"
    ) in shown
    assert '<h2>Resume</h2><p class="command"><span aria-hidden="true">$</span><code>claudinator resume 8f2d43fe</code></p><div class="actions">' in shown
    transcript(SID2).unlink()
    gone = page(served, f"/s/{SID2}")
    assert f'<p class="facts">{where}<span>Thu 29 Oct · 08:00 – 08:20</span><span class="faint">20m</span><span>1 prompt</span></p>' in gone
    assert '<code>claudinator resume 075b60d8</code></p><p class="gone">Claude Code deleted its copy. Resuming puts it back.</p>' in gone
    subagent = page(served, f"/s/{SID}/{AID}")
    assert f'<p class="facts">{where}<span>Thu 29 Oct · 23:00 – 23:00</span><span class="faint">0m</span></p>' in subagent


def test_the_list_groups_sessions_by_day_and_reads_time_title_project_and_counts(served, monkeypatch):
    sid3, sid4 = "3c0ffee0-0000-4000-8000-000000000003", "4c0ffee0-0000-4000-8000-000000000004"
    append(transcript(), user("hello", ts="2026-10-01T10:00:00Z"))
    append(transcript(agent=AID), user("subagent", ts="2026-10-01T10:00:00Z"))
    elsewhere = {"cwd": "/tmp/elsewhere"}
    append(transcript(SID2, slug="-tmp-elsewhere"), user("again", ts="2026-10-02T09:00:00Z", **elsewhere), user("more", ts="2026-10-02T09:30:00Z", **elsewhere))
    append(transcript(sid3), user("early", ts="2026-10-02T08:00:00Z"))
    append(transcript(sid4), user("last year", ts="2025-12-31T23:00:00Z"))
    monkeypatch.setattr(web, "now", lambda: int(datetime(2026, 10, 2, 12, tzinfo=UTC).timestamp() * 1000))
    shown = page(served, "/")
    assert shown.index("<h1>4 sessions</h1>") < shown.index('<div class="day-head">')
    heads = re.findall(r'<div class="day-head"><h2>(.*?)</h2>(?:<span class="tag">(.*?)</span>)?<span class="n">(.*?)</span></div>', shown)
    assert heads == [
        ("Friday 2 October", "Today", "2 sessions"),
        ("Thursday 1 October", "Yesterday", "1 session"),
        ("Wednesday 31 December 2025", "", "1 session"),
    ]
    assert re.findall(r"<time>(.*?)</time>", shown) == ["09:30", "08:00", "10:00", "23:00"]
    assert (
        f'<li><time>10:00</time><span class="title"><a class="open" href="s/{SID}"><span class="head">hello</span></a></span>'
        '<a class="project" href="./?project=%2FUsers%2Fme%2Fdev%2Fdemo" title="/Users/me/dev/demo">'
        '<span class="dir">/Users/me/dev/</span><span class="leaf">demo</span></a>'
        '<span class="count"><span class="n">1</span> prompt</span><span class="count"><span class="n">1</span> subagent</span></li>'
    ) in shown
    assert '<span class="leaf">elsewhere</span></a><span class="count"><span class="n">2</span> prompts</span><span></span></li>' in shown


def test_a_title_sets_a_leading_slash_command_and_heading_apart_from_the_rest(served):
    titles = {
        SID: "/pstack:poteto-mode store every session",
        SID2: "/clear",
        "3c0ffee0-0000-4000-8000-000000000003": "# Sync and update\nYou are running unattended",
        "4c0ffee0-0000-4000-8000-000000000004": "/Users/me/notes is a path, not a command",
    }
    for sid, prompt in titles.items():
        append(transcript(sid), user(prompt))
    open_archive().sweep()
    transcript(SID2).unlink()
    shown = page(served, "/")
    parts = dict(re.findall(r'<a class="open" href="s/([^"]+)">(.*?)</a>', shown))
    assert parts == {
        SID: '<span class="cmd">/pstack:poteto-mode</span><span class="rest">store every session</span>',
        SID2: '<span class="cmd">/clear</span>',
        "3c0ffee0-0000-4000-8000-000000000003": '<span class="hash">#</span><span class="head">Sync and update</span><span class="rest">You are running unattended</span>',
        "4c0ffee0-0000-4000-8000-000000000004": '<span class="head">/Users/me/notes is a path, not a command</span>',
    }
    assert f'<a class="open" href="s/{SID2}"><span class="cmd">/clear</span></a><span class="badge">deleted</span></span>' in shown


def test_a_search_result_counts_its_hits_and_says_when_where_and_where_the_best_one_is(served):
    append(transcript(), user("first", ts="2026-10-01T10:00:00Z"), reply("the quokka line", ts="2026-10-01T10:05:00Z"), reply("another quokka", ts="2026-10-01T10:06:00Z"))
    append(transcript(agent=AID), user("a wombat in a subagent", ts="2026-10-01T10:06:00Z"))
    append(transcript(SID2), user("nothing to see", ts="2026-10-02T08:00:00Z"), ai_title("Zebracorn hunt"))
    where = '<span><span class="dir">/Users/me/dev/</span><span class="leaf">demo</span></span>'
    result = r'<p class="tally"><span class="n">(\d+)</span> <span>(\w+)</span></p>.*?<p class="meta"><span>(.*?)</span>(.*?)<span class="tag">(.*?)</span></p>'
    assert re.findall(result, page(served, "/?q=quokka")) == [("2", "hits", "Thu 1 Oct 2026 · 10:06", where, "best in a reply")]
    assert re.findall(result, page(served, "/?q=wombat")) == [("1", "hit", "Thu 1 Oct 2026 · 10:06", where, "in a prompt · subagent a7c9c203")]
    assert re.findall(result, page(served, "/?q=zebracorn")) == [("1", "hit", "Fri 2 Oct 2026 · 08:00", where, "in the title")]


def test_the_toolbar_names_the_filters_and_clear_drops_only_the_words(served):
    append(transcript(), user("the quokka", ts="2026-10-01T10:00:00Z"))
    listed = page(served, "/?project=demo&since=2026-09-01")
    assert '<h1>1 session</h1><span class="scope">in demo since 2026-09-01</span></div>' in listed
    found = page(served, "/?q=quokka&project=demo&tools=on")
    assert '<h1>1 session</h1><span class="scope">matching “quokka” in demo</span><a class="clear" href="./?project=demo&amp;tools=on">Clear</a>' in found
    assert '<input class="switch" type="checkbox" name="tools" form="find" checked>' in found
    assert 'Nothing matches that.' in page(served, "/?q=zebracorn")
    refused = get(served, "/?since=today")[2].decode()
    assert '<h1>No sessions</h1><span class="scope">since today</span></div>' in refused
    assert '</div><p class="banner">since: expected ' in refused


def test_a_search_hit_opens_its_transcript_at_the_matching_step_with_the_words_marked(served):
    lines = (user("first"), reply("the quokka line"))
    append(transcript(), *lines)
    append(transcript(agent=AID), user("a wombat in a subagent"))
    append(transcript(SID2), user("nothing to see"), ai_title("Zebracorn hunt"))
    at = offsets(*lines)[1]
    found = page(served, "/?q=quokka").partition('<ol class="hits">')[2]
    assert re.findall(r'<a [^>]*href="([^"]*)"', found) == [f"s/{SID}?at={at}.0&amp;q=quokka#m{at}.0"]
    assert f'id="m{at}.0"><div class="text">the <mark>quokka</mark> line</div>' in page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert f'href="s/{SID}/{AID}?at=0.0&amp;q=wombat#m0.0"' in page(served, "/?q=wombat")
    assert f'<a class="open" href="s/{SID2}">Zebracorn hunt</a>' in page(served, "/?q=zebracorn")


def test_lines_that_show_nothing_fold_into_one_marker_with_a_raw_link_each(served):
    lines = (line({"type": "mode", "mode": "normal"}), line({"type": "file-history-snapshot"}), user("hello"))
    append(transcript(), *lines)
    at = offsets(*lines)
    assert (
        '<details class="step note hidden" id="m0.0"><summary>2 hidden lines<span class="badge">hidden</span>'
        '<span class="kinds">1 mode, 1 file-history-snapshot</span></summary>'
        f'<ul><li><a href="raw/{SID}/0">mode</a></li><li><a href="raw/{SID}/{at[1]}">file-history-snapshot</a></li></ul>'
        "</details>"
    ) in page(served, f"/s/{SID}?hidden=1")


def test_hidden_lines_stay_out_of_the_way_until_asked_for_and_notices_always_show(served):
    api_error = line({"type": "system", "subtype": "api_error", "error": {"message": "overloaded"}})
    lines = (line({"type": "mode"}), user("hello"), api_error, line({"type": "mode"}), reply("the quokka line"), line({"type": "mode"}))
    append(transcript(), *lines)
    at = offsets(*lines)[4]
    toggle = '<a class="button" href="{}"><span>Hidden lines <span class="n">3</span></span><span class="switch" role="img" aria-label="{}"{}></span></a>'
    shown = page(served, f"/s/{SID}")
    assert 'class="step note hidden"' not in shown
    error = 'The API answered with an error: <span class="text">overloaded</span>'
    assert error in shown
    assert toggle.format(f"s/{SID}?hidden=1", "not shown", "") in shown
    revealed = page(served, f"/s/{SID}?hidden=1")
    assert revealed.count('class="step note hidden"') == 3
    assert error in revealed
    assert toggle.format(f"s/{SID}", "shown", " data-on") in revealed
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert toggle.format(f"s/{SID}?at={at}.0&amp;q=quokka&amp;hidden=1#m{at}.0", "not shown", "") in landed
    revealed = page(served, f"/s/{SID}?at={at}.0&q=quokka&hidden=1")
    assert toggle.format(f"s/{SID}?at={at}.0&amp;q=quokka#m{at}.0", "shown", " data-on") in revealed
    assert "the <mark>quokka</mark> line" in revealed
    append(transcript(SID2), user("nothing hidden"))
    assert "Hidden lines" not in page(served, f"/s/{SID2}")


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
    assert '<summary><b>&lt;b&gt;Tool</b> <span class="gist">&lt;/pre&gt;&lt;script&gt;</span><span class="size">no output</span></summary>' in shown
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
    typed = web.Form(q='"><script>alert(4)</script>', project="‮x")
    shown = web.sessions_page("/T/", (), typed, None, date(2026, 10, 30), "since: <b>")
    assert 'value="&quot;&gt;&lt;script&gt;alert(4)&lt;/script&gt;"' in shown
    assert 'name="project" form="find" value="\\u202ex"' in shown
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
