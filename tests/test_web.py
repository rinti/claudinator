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
from urllib.parse import urlsplit

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


def get(server, target, *, cookie="signed in", host=None, headers=(), method="GET") -> tuple[int, dict[str, str], bytes]:
    if cookie == "signed in":
        cookie = f"claudinator-{server.port}={server.token}"
    sent = {"Host": host or f"127.0.0.1:{server.port}", **({"Cookie": cookie} if cookie else {}), **dict(headers)}
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.request(method, target, headers=sent)
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


def test_the_printed_url_signs_a_browser_in_and_takes_the_token_out_of_the_address(served):
    url = urlsplit(served.url)
    status, headers, _ = get(served, f"{url.path}?{url.query}", cookie=None)
    assert (status, headers["location"]) == (303, "/")
    assert headers["set-cookie"] == f"claudinator-{served.port}={served.token}; HttpOnly; SameSite=Strict; Path=/"
    assert get(served, "/", cookie=headers["set-cookie"].split(";")[0])[0] == 200
    status, headers, _ = get(served, f"/s/{SID}?{url.query}&at=0.0", cookie=None)
    assert (status, headers["location"]) == (303, f"/s/{SID}?at=0.0")


def test_a_wrong_token_or_a_missing_cookie_is_refused(served):
    status, headers, _ = get(served, "/?k=" + "x" * 43, cookie=None)
    assert (status, "set-cookie" in headers) == (403, False)
    assert get(served, "/", cookie=None)[0] == 403
    assert get(served, "/", cookie=f"claudinator-{served.port}={'x' * 43}")[0] == 403
    assert get(served, "/web.css", cookie=None)[0] == 403


def test_only_this_server_as_127_0_0_1_or_localhost_gets_an_answer(served):
    assert get(served, "/", host=f"evil.example:{served.port}")[0] == 403
    assert get(served, "/", host="127.0.0.1")[0] == 403
    assert get(served, "/", host=f"localhost:{served.port}")[0] == 200
    assert get(served, "/", headers={"Origin": "http://evil.example"})[0] == 403
    assert get(served, "/", headers={"Origin": f"http://localhost:{served.port}"})[0] == 200


def test_every_response_forbids_scripts_and_caching(served):
    pasted = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}
    append(transcript(), line({"type": "user", "message": {"content": [pasted]}}))
    open_archive().sweep()
    answers = {
        "page": get(served, f"/s/{SID}"),
        "stylesheet": get(served, "/web.css"),
        "raw": get(served, f"/raw/{SID}/0"),
        "image": get(served, f"/image/{SID}/0.0"),
        "download": get(served, f"/export/{SID}"),
        "sign-in": get(served, served.url.removeprefix(f"http://127.0.0.1:{served.port}"), cookie=None),
        "refused": get(served, "/", cookie=None),
        "missing": get(served, f"/s/{SID2}"),
        "unsupported": get(served, "/", method="POST"),
    }
    assert {name: status for name, (status, _, _) in answers.items()} == {
        "page": 200,
        "stylesheet": 200,
        "raw": 200,
        "image": 200,
        "download": 200,
        "sign-in": 303,
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
    read = re.search(rf'<section class="step call" id="m{at[1]}\.0">.*?</section>', shown, re.S)[0]
    bash = re.search(rf'<section class="step call" id="m{at[2]}\.0">.*?</section>', shown, re.S)[0]
    assert f'<div class="output" id="m{at[4]}.0"><pre>one</pre>' in read
    assert f'<div class="output error" id="m{at[3]}.0"><span class="label">error</span><pre>exit 1</pre>' in bash


def test_a_call_sums_up_its_input_by_its_first_string_field(served):
    calls = (
        ("Bash", {"command": "ls -la\n   /tmp", "description": "List tmp"}),
        ("Read", {"limit": 5, "file_path": "/a/b.py"}),
        ("Agent", {"description": "", "prompt": "Find the cursor"}),
        ("Grep", {"pattern": "x" * 200}),
        ("TodoWrite", {"todos": [{"content": "ship"}], "merge": True}),
    )
    append(transcript(), user("go"), *(tool_call(name, given, tool_use_id=f"toolu_{i}") for i, (name, given) in enumerate(calls)))
    summaries = re.findall(r'<details class="input"><summary>(.*?)</summary>', page(served, f"/s/{SID}"))
    assert summaries == [
        "<b>Bash</b> ls -la /tmp",
        "<b>Read</b> /a/b.py",
        "<b>Agent</b> Find the cursor",
        "<b>Grep</b> " + "x" * 159 + "…",
        "<b>TodoWrite</b> {&quot;todos&quot;:[{&quot;content&quot;:&quot;ship&quot;}],&quot;merge&quot;:true}",
    ]
    assert '<pre>{\n  &quot;command&quot;: &quot;ls -la\\n   /tmp&quot;,\n  &quot;description&quot;: &quot;List tmp&quot;\n}</pre>' in page(served, f"/s/{SID}")


def test_a_commands_output_shows_without_its_wrapper_tags_and_marks_land_on_the_words(served):
    lines = (
        user("<bash-input>uv sync</bash-input>"),
        user("<bash-stdout>Resolved 1 quokka</bash-stdout><bash-stderr>warning: a wombat</bash-stderr>"),
    )
    append(transcript(), *lines)
    at = offsets(*lines)[1]
    assert f'href="/s/{SID}?at={at}.0&amp;q=quokka+wombat#m{at}.0"' in page(served, "/?q=quokka+wombat&tools=on")
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
    assert '<summary><span class="label">notification</span> Build finished</summary>' in page(served, f"/s/{SID}")


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
    assert f'<section class="step call" id="m{call}.0">' in shown
    assert f'<a class="spawned" href="/s/{SID}/{AID}">subagent a7c9c203 · Explore · Find the cursor</a>' in shown
    assert f'<a href="/s/{SID}?call=toolu_77">the call that started it</a>' in page(served, f"/s/{SID}/{AID}")
    assert get(served, f"/s/{SID}?call=toolu_77")[1]["location"] == f"/s/{SID}?at={call}.0#m{call}.0"
    assert f'<a href="/s/{SID}/{AID}?call=toolu_88">the call that started it</a>' in page(served, f"/s/{SID}/{nested}")
    assert get(served, f"/s/{SID}/{AID}?call=toolu_88")[1]["location"] == f"/s/{SID}/{AID}?at={inner}.0#m{inner}.0"


def test_the_list_counts_its_sessions_and_reads_date_title_project_prompts_agents(served):
    append(transcript(), user("hello", ts="2026-10-01T10:00:00Z"))
    append(transcript(agent=AID), user("subagent"))
    append(transcript(SID2, slug="-tmp-elsewhere"), user("again", ts="2026-10-02T10:00:00Z", cwd="/tmp/elsewhere"), user("more"))
    shown = page(served, "/")
    assert shown.index("<p class=\"muted\">2 sessions</p>") < shown.index("<table")
    rows = [
        [re.sub(r"<[^>]*>", "", cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row)]
        for row in re.findall(r"<tr>(.*?)</tr>", shown)
    ]
    assert [row[1:] for row in rows] == [["again", "/tmp/elsewhere", "2 prompts", ""], ["hello", "/Users/me/dev/demo", "1 prompt", "1 agent"]]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", row[0]) for row in rows)


def test_a_search_hit_opens_its_transcript_at_the_matching_step_with_the_words_marked(served):
    lines = (user("first"), reply("the quokka line"))
    append(transcript(), *lines)
    append(transcript(agent=AID), user("a wombat in a subagent"))
    append(transcript(SID2), user("nothing to see"), ai_title("Zebracorn hunt"))
    at = offsets(*lines)[1]
    found = page(served, "/?q=quokka")
    assert f'href="/s/{SID}?at={at}.0&amp;q=quokka#m{at}.0"' in found
    assert f'id="m{at}.0"><div class="text">the <mark>quokka</mark> line</div>' in page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert f'href="/s/{SID}/{AID}?at=0.0&amp;q=wombat#m0.0"' in page(served, "/?q=wombat")
    assert f'<a class="excerpt" href="/s/{SID2}">' in page(served, "/?q=zebracorn")


def test_lines_that_show_nothing_fold_into_one_marker_with_a_raw_link_each(served):
    lines = (line({"type": "mode", "mode": "normal"}), line({"type": "file-history-snapshot"}), user("hello"))
    append(transcript(), *lines)
    at = offsets(*lines)
    assert (
        '<details class="step hidden" id="m0.0"><summary>2 hidden lines: 1 mode, 1 file-history-snapshot</summary>'
        f'<ul><li><a href="/raw/{SID}/0">mode</a></li><li><a href="/raw/{SID}/{at[1]}">file-history-snapshot</a></li></ul>'
        "</details>"
    ) in page(served, f"/s/{SID}?hidden=1")


def test_hidden_lines_stay_out_of_the_way_until_asked_for_and_notices_always_show(served):
    api_error = line({"type": "system", "subtype": "api_error", "error": {"message": "overloaded"}})
    lines = (line({"type": "mode"}), user("hello"), api_error, line({"type": "mode"}), reply("the quokka line"), line({"type": "mode"}))
    append(transcript(), *lines)
    at = offsets(*lines)[4]
    shown = page(served, f"/s/{SID}")
    assert 'class="step hidden"' not in shown
    assert '<span class="label">api error</span>' in shown
    assert f'<span> · 3 hidden lines · <a href="/s/{SID}?hidden=1">show</a></span>' in shown
    revealed = page(served, f"/s/{SID}?hidden=1")
    assert revealed.count('class="step hidden"') == 3
    assert '<span class="label">api error</span>' in revealed
    assert f'<span> · 3 hidden lines · <a href="/s/{SID}">hide</a></span>' in revealed
    landed = page(served, f"/s/{SID}?at={at}.0&q=quokka")
    assert f'<a href="/s/{SID}?at={at}.0&amp;q=quokka&amp;hidden=1#m{at}.0">show</a>' in landed
    revealed = page(served, f"/s/{SID}?at={at}.0&q=quokka&hidden=1")
    assert f'<a href="/s/{SID}?at={at}.0&amp;q=quokka#m{at}.0">hide</a>' in revealed
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
    get(served, served.url.removeprefix(f"http://127.0.0.1:{served.port}"), cookie=None)
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
        shown = web.transcript_page(session, None, build(archive.lines(SID), session.agents, None), None, "", False, None)
    assert (
        '<div class="text">run &lt;script&gt;alert(1)&lt;/script&gt; and &lt;img src=x onerror=alert(2)&gt; on '
        '<span class="esc">\\x1b</span>[31mred <span class="esc">\\u202e</span>gpj.exe</div>'
    ) in shown
    assert "<title>Invoice \\u202egpj.exe &lt;b&gt;</title>" in shown
    assert "<summary><b>&lt;b&gt;Tool</b> &lt;/pre&gt;&lt;script&gt;</summary>" in shown
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
    shown = web.sessions_page((), web.Form(q='"><script>alert(4)</script>', project="‮x"), None, "since: <b>")
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
def test_a_real_browser_signs_in_and_renders_a_hostile_transcript_as_text(served, tmp_path):
    append(transcript(), user(HOSTILE))
    signed = served.url.replace("/?k=", f"/s/{SID}?k=")
    flags = ["--headless=new", "--disable-gpu", "--no-first-run", "--use-mock-keychain", f"--user-data-dir={tmp_path}"]
    browser = [CHROME, *flags, "--dump-dom", signed]
    dom = subprocess.run(browser, capture_output=True, text=True, timeout=60).stdout
    found = Elements()
    found.feed(dom)
    assert (found.tags.count("main"), found.tags.count("script"), found.handlers) == (1, 0, [])
    assert "run &lt;script&gt;alert(1)&lt;/script&gt; and &lt;img src=x onerror=alert(2)&gt;" in dom
    assert '<span class="esc">\\u202e</span>gpj.exe' in dom
