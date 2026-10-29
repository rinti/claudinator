import json
from dataclasses import replace

import pytest

import lines
from claudinator.record import (
    Block,
    EntryDraft,
    Facts,
    Kind,
    Other,
    Title,
    Turn,
    Unparsed,
    desktop_session,
    parse,
    project,
    subagent_meta,
    text_of,
)

DOTFILES = "/Users/andreas/dotfiles"
ROUNDUP = "/Users/andreas/dev/ai-roundup"
CLAUDINATOR = "/Users/andreas/dev/claudinator"
PSTACK = "/Users/andreas/dev/pstack-rinti"
SBR = "/Users/andreas/dev/SBR-Base"
AMEX_POC = "/Users/andreas/dev/AmericanExpress-SST-CmsPoc"


def prompt(text: str) -> Block:
    return Block(Kind.PROMPT, text)


def _unknown_type(line: str) -> str:
    obj = json.loads(line)
    obj["type"] = "hologram-transcript"
    return json.dumps(obj)


def _with_unknown_block(line: str, *, keep_text: bool) -> str:
    obj = json.loads(line)
    content = [{"type": "server_widget", "widget": {"text": "zebracorn"}}]
    if keep_text:
        content.append({"type": "text", "text": "the widget rendered"})
    obj["message"]["content"] = content
    return json.dumps(obj)


PARSE_CASES = [
    (
        "typed prompt",
        lines.HUMAN_PROMPT,
        Turn(1790611624057, DOTFILES, ((0, prompt("in herdr i want ctrl+w to switch to next pane")),)),
    ),
    ("slash command is the command as typed", lines.SLASH_COMMAND, Turn(1791442803638, DOTFILES, ((0, prompt("/clear")),))),
    (
        "slash command with arguments",
        lines.SLASH_COMMAND_WITH_ARGS,
        Turn(1792157281188, AMEX_POC, ((0, prompt("/pstack:arena Find a good solution to make them work")),)),
    ),
    ("shell command typed with !", lines.BASH_INPUT, Turn(1792408950689, SBR, ((0, prompt("! gs")),))),
    (
        "output of a ! command is tool output",
        lines.BASH_STDOUT,
        Turn(
            1792487532039,
            SBR,
            ((0, Block(Kind.TOOL_OUTPUT, "<bash-stdout>(Bash completed with no output)</bash-stdout><bash-stderr></bash-stderr>")),),
        ),
    ),
    (
        "output of a local command is tool output",
        lines.LOCAL_COMMAND_STDOUT,
        Turn(1793029986612, AMEX_POC, ((0, Block(Kind.TOOL_OUTPUT, "<local-command-stdout>(no content)</local-command-stdout>")),)),
    ),
    (
        "local command stderr (no real line on this Mac; the stdout line with its tag renamed)",
        lines.LOCAL_COMMAND_STDOUT.replace("local-command-stdout", "local-command-stderr"),
        Turn(1793029986612, AMEX_POC, ((0, Block(Kind.TOOL_OUTPUT, "<local-command-stderr>(no content)</local-command-stderr>")),)),
    ),
    ("interrupt marker", lines.INTERRUPTED, Other(1792586093562, "/Users/andreas/dev/SBR-stockholmbusinessregion.se/.audit/baseline-a.json.bodies")),
    ("interrupt marker for tool use", lines.INTERRUPTED_FOR_TOOL_USE, Other(1790928821424, "/Users/andreas/dev/amex/frontend")),
    (
        "text next to a tool result is tool output",
        lines.TOOL_RESULT_WITH_TEXT,
        Turn(
            1792062596335,
            "/Users/andreas/dev/amex",
            ((0, Block(Kind.TOOL_OUTPUT, "")), (1, Block(Kind.TOOL_OUTPUT, "Tool loaded."))),
        ),
    ),
    (
        "pasted image is dropped, its text placeholder kept",
        lines.PROMPT_WITH_IMAGE,
        Turn(1791821874491, DOTFILES, ((0, prompt("[Image #1]")),)),
    ),
    (
        "tool result string, not the toolUseResult copy",
        lines.TOOL_RESULT_ERROR,
        Turn(
            1790839073160,
            ROUNDUP,
            (
                (
                    0,
                    Block(
                        Kind.TOOL_OUTPUT,
                        "File does not exist. Note: your current working directory is /Users/andreas/dev/ai-roundup.",
                    ),
                ),
            ),
        ),
    ),
    (
        "tool result parts: image placeholder and text",
        lines.TOOL_RESULT_IMAGE,
        Turn(
            1791888231184,
            "/Users/andreas/dev/delight",
            (
                (
                    0,
                    Block(
                        Kind.TOOL_OUTPUT,
                        "[image]\n[Image: source: /Users/andreas/.claude/projects/"
                        "-Users-andreas-dev-delight/13a39598-a74e-48ba-9c46-8…",
                    ),
                ),
            ),
        ),
    ),
    (
        "tool result with only a tool reference has empty text",
        lines.TOOL_RESULT_REFERENCE,
        Turn(1791442850850, DOTFILES, ((0, Block(Kind.TOOL_OUTPUT, "")),)),
    ),
    (
        "subagent prompt written by the parent",
        lines.SUBAGENT_PROMPT,
        Turn(
            1793301166300,
            CLAUDINATOR,
            ((0, prompt("Poteto-mode SKILL.md: /Users/andreas/.claude/plugins/cache/pstack-rinti/pstack/0.1.19/skills/poteto-…")),),
        ),
    ),
    (
        "assistant text",
        lines.ASSISTANT_TEXT,
        Turn(1792071923037, PSTACK, ((0, Block(Kind.REPLY, "Now the repo docs, sync tooling, and precedent.")),)),
    ),
    (
        "bash call: name and compact input",
        lines.BASH_CALL,
        Turn(
            1792071934413,
            PSTACK,
            (
                (
                    0,
                    Block(
                        Kind.TOOL_CALL,
                        'Bash {"command":"cat tools/forks.json","description":"Read forks.json"}',
                        tool="Bash",
                        tool_use_id="toolu_01D6KuhxoikZtx8MhhsBuPGf",
                    ),
                ),
            ),
        ),
    ),
    (
        "agent call keeps the tool_use id that links its subagent",
        lines.AGENT_CALL,
        Turn(
            1793301159511,
            CLAUDINATOR,
            (
                (
                    0,
                    Block(
                        Kind.TOOL_CALL,
                        'Agent {"description":"Architect runner: opus","subagent_type":"pstack:poteto-agent",'
                        '"model":"opus","prompt":"Poteto-mode SKILL.md: /Users/andreas/.claude/plugins/cache/'
                        'pstack-rinti/pstack/0.1.19/skills/poteto-…","run_in_background":true}',
                        tool="Agent",
                        tool_use_id="toolu_01HjMUN1aqW8WdjsVoWyUTVQ",
                    ),
                ),
            ),
        ),
    ),
    ("thinking only", lines.THINKING, Other(1790751608199, ROUNDUP)),
    ("ai title", lines.AI_TITLE, Title("Commit changes to repo", custom=False)),
    ("custom title", lines.CUSTOM_TITLE, Title("Available skills", custom=True)),
    ("isMeta user line", lines.META_USER, Other(1791442803660, DOTFILES)),
    ("peer message", lines.PEER_MESSAGE, Other(1792075403126, PSTACK)),
    ("task notification origin", lines.TASK_NOTIFICATION, Other(1793301587383, CLAUDINATOR)),
    ("compaction summary", lines.COMPACT_SUMMARY, Other(1791270716245, ROUNDUP)),
    ("attachment", lines.ATTACHMENT, Other(1793300849954, CLAUDINATOR)),
    ("system", lines.SYSTEM, Other(1791821880719, DOTFILES)),
    ("api request", lines.API_REQUEST, Other(1792916111525, None)),
    ("queue operation", lines.QUEUE_OPERATION, Other(1793301245134, None)),
    ("mode", lines.MODE, Other(None, None)),
    ("permission mode", lines.PERMISSION_MODE, Other(None, None)),
    ("last prompt", lines.LAST_PROMPT, Other(None, None)),
    ("atis latch", lines.ATIS_LATCH, Other(None, None)),
    ("cost state", lines.COST_STATE, Other(None, None)),
    ("file history snapshot", lines.FILE_HISTORY_SNAPSHOT, Other(None, None)),
    ("api request blob", lines.API_REQUEST_BLOB, Other(None, None)),
    ("agent name", lines.AGENT_NAME, Other(None, None)),
    ("tools used", lines.TOOLS_USED, Other(None, None)),
    ("unknown record type", _unknown_type(lines.ASSISTANT_TEXT), Other(1792071923037, PSTACK)),
    (
        "unknown block type next to text",
        _with_unknown_block(lines.ASSISTANT_TEXT, keep_text=True),
        Turn(1792071923037, PSTACK, ((1, Block(Kind.REPLY, "the widget rendered")),)),
    ),
    (
        "only unknown block types",
        _with_unknown_block(lines.ASSISTANT_TEXT, keep_text=False),
        Other(1792071923037, PSTACK),
    ),
    ("empty ai title", '{"type":"ai-title","aiTitle":"  "}', Other(None, None)),
    ("torn line", lines.HUMAN_PROMPT[:57], Unparsed()),
    ("json array", "[1, 2, 3]", Unparsed()),
    ("plain text", "not json at all", Unparsed()),
    ("nesting past the recursion limit", '{"a":' * 1_000_000 + "1" + "}" * 1_000_000, Unparsed()),
]


@pytest.mark.parametrize(("line", "expected"), [c[1:] for c in PARSE_CASES], ids=[c[0] for c in PARSE_CASES])
def test_parse(line, expected):
    assert parse(line.encode()) == expected


def test_parse_invalid_utf8_is_unparsed():
    assert parse(b'{"type":"user","message":{"content":"\xff\xfe"}}') == Unparsed()


def test_project_folds_facts_in_file_order():
    records = [
        Unparsed(),
        Title("Draft title", custom=False),
        Other(2000, "/first/cwd"),
        Turn(1000, "/second/cwd", ((0, prompt("hello")), (1, Block(Kind.TOOL_OUTPUT, "  \n")))),
        Title("Draft title", custom=False),
        Title("Better title", custom=False),
        Title("Mine", custom=True),
        Other(None, None),
        Turn(3000, None, ((2, Block(Kind.REPLY, "hi")),)),
        Title("Mine", custom=True),
    ]
    facts, emitted = Facts(), []
    for record in records:
        facts, drafts = project(facts, record)
        emitted.append(drafts)

    assert facts == Facts(
        cwd="/first/cwd",
        first_ts=1000,
        last_ts=3000,
        ai_title="Better title",
        custom_title="Mine",
        unparsed=1,
    )
    assert emitted == [
        [],
        [EntryDraft(0, Kind.TITLE, None)],
        [],
        [EntryDraft(0, Kind.PROMPT, 1000)],
        [],
        [EntryDraft(0, Kind.TITLE, 2000)],
        [EntryDraft(0, Kind.TITLE, 2000)],
        [],
        [EntryDraft(2, Kind.REPLY, 3000)],
        [],
    ]


def test_text_of_finds_the_block_by_content_index():
    turn = Turn(1, None, ((0, Block(Kind.REPLY, "first")), (3, Block(Kind.TOOL_CALL, "Bash {}", tool="Bash"))))
    assert text_of(turn, 3) == "Bash {}"
    assert text_of(turn, 0) == "first"
    assert text_of(Title("A title", custom=True), 0) == "A title"
    with pytest.raises(LookupError):
        text_of(turn, 1)


def test_subagent_meta_reads_description_type_and_tool_use_id():
    body = (
        b'{"agentType":"pstack:poteto-agent","description":"Implement admin-side query fixes",'
        b'"toolUseId":"toolu_011xTaVKv5kz48q9dzvr8Twy","spawnDepth":1,"model":"opus","finished":true}'
    )
    meta = subagent_meta(body)
    assert (meta.description, meta.agent_type, meta.tool_use_id) == (
        "Implement admin-side query fixes",
        "pstack:poteto-agent",
        "toolu_011xTaVKv5kz48q9dzvr8Twy",
    )
    assert subagent_meta(b"{not json") == subagent_meta(b"{}")


def test_desktop_session_names_the_cli_session_and_title():
    body = (
        b'{"sessionId":"local_67f1cd3c-246f-4088-b0f5-b33d5cbd24ef",'
        b'"cliSessionId":"af2a2ef6-b9c1-442c-a2a6-25c22f620c42","title":"Alpha channel installation"}'
    )
    desktop = desktop_session(body)
    assert (desktop.cli_session_id, desktop.title) == ("af2a2ef6-b9c1-442c-a2a6-25c22f620c42", "Alpha channel installation")
    assert desktop_session(b'{"title":"no cli session yet"}') is None
    assert desktop_session(b"not json") is None


def test_parse_replaces_lone_surrogates_in_every_string_it_returns():
    turn = parse(
        rb'{"type":"assistant","cwd":"/d\udfff","message":{"content":['
        rb'{"type":"text","text":"a\ud800b"},'
        rb'{"type":"tool_use","id":"toolu_\ud801","name":"B\udc00","input":{"command":"x\ud802"}}]}}'
    )
    assert turn == Turn(
        None,
        "/d�",
        (
            (0, Block(Kind.REPLY, "a�b")),
            (1, Block(Kind.TOOL_CALL, 'B� {"command":"x�"}', tool="B�", tool_use_id="toolu_�")),
        ),
    )
    assert parse(rb'{"type":"user","message":{"content":"\ud83d"}}') == Turn(None, None, ((0, prompt("�")),))
    assert parse(
        rb'{"type":"user","message":{"content":[{"type":"tool_result","content":[{"type":"text","text":"\udbff"}]}]}}'
    ) == Turn(None, None, ((0, Block(Kind.TOOL_OUTPUT, "�")),))
    assert parse(rb'{"type":"ai-title","aiTitle":"t\ud800"}') == Title("t�", custom=False)
    assert subagent_meta(rb'{"description":"d\ud800"}').description == "d�"
    assert desktop_session(rb'{"cliSessionId":"s","title":"t\udc00"}').title == "t�"
    assert parse("😀".encode()) == Unparsed()


def test_a_title_entry_takes_its_own_timestamp_and_falls_back_to_the_last_activity():
    with_ts = parse(b'{"type":"custom-title","customTitle":"Mine","timestamp":"2026-10-01T10:00:00.000Z"}')
    assert with_ts == Title("Mine", custom=True, ts=1790848800000)
    assert project(Facts(last_ts=5), with_ts)[1] == [EntryDraft(0, Kind.TITLE, 1790848800000)]
    assert project(Facts(last_ts=5), Title("Other", custom=False))[1] == [EntryDraft(0, Kind.TITLE, 5)]


def test_a_dated_title_line_widens_the_transcripts_activity_like_any_dated_line():
    ten = 1790848800000
    custom = parse(b'{"type":"custom-title","customTitle":"Mine","timestamp":"2026-10-01T10:00:00.000Z"}')
    ai = parse(b'{"type":"ai-title","aiTitle":"Later","timestamp":"2026-10-01T11:00:00.000Z"}')
    facts, _ = project(Facts(first_ts=ten + 5000, last_ts=ten + 5000), custom)
    assert (facts.first_ts, facts.last_ts, facts.custom_title) == (ten, ten + 5000, "Mine")
    facts, _ = project(facts, ai)
    assert (facts.first_ts, facts.last_ts, facts.ai_title) == (ten, ten + 3_600_000, "Later")
    again = parse(b'{"type":"ai-title","aiTitle":"Later","timestamp":"2026-10-01T12:00:00.000Z"}')
    assert project(facts, again) == (replace(facts, last_ts=ten + 7_200_000), [])
