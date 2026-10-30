# claudinator serve: the archive in a browser

## Problem

The user wants to inspect any archived conversation in a browser on this Mac. They find it by project, date, or words, then read it with each tool call next to its output and each subagent one click away. Four facts from the grounding shape the design. `Archive.open` can create a database or rebuild the projection under the write lock, and a process of another `PROJECTION_VERSION` that does so stops the hourly sweeper. `record.py` is the only reader of Claude Code fields, and `Turn.blocks` is both what `show` prints and what the index holds. A localhost port goes around the database's 0600 mode, and transcripts hold markup, 5,356 CSI sequences, and bidi overrides. `archive.py` is at 995 lines and was split before at 1,000.

The design arena measured the live archive read-only through an immutable URI. The 78.9 MB session is 81% `api-request` lines. It shows 1.3 MB of text in 1,693 blocks and parses in 0.33 s. The heaviest transcript to read holds 2.9 MB of text, and no block in the archive exceeds 75 KB. Images are the bulk. 1,019 of them hold 237 MB of base64, and one session holds 84 MB.

## Usage (caller's view)

```
$ claudinator serve                     # --port N, --no-browser
claudinator: serving the archive at http://127.0.0.1:52817/Xq3.../  until Ctrl-C
```

The browser opens that URL. It lists every session newest first under a form for words, project, since and until (the CLI's WHEN syntax), and a tools box. Clicking a project filters by it. Words show the hits with the matches marked. A hit opens `/<token>/s/<sid>[/<aid>]?at=<off>.<block>&q=...#m<off>.<block>`, which is the transcript scrolled to the matching message, open and marked. The link also carries every filter the search had, so the page's back link and its header search return to the same results. A tool call is one closed row that opens to its input and then its output, and errors are red. A switch in the aside hides every tool call and thinking row at once. An Agent call links its subagent, and the subagent's page links back to that call. Every step has a `raw` link to its JSON line, and every transcript has a `.jsonl` download. The section "The look" below says how the pages look.

```python
# cli.py
def cmd_serve(cfg, args):
    server = web.bind(cfg, args.port)        # ArchiveError if missing or another version's: exit 1
    _emit(f"claudinator: serving the archive at {server.url} until Ctrl-C")
    if not args.no_browser:
        _browse(server.url)
    return web.serve(server)                 # 0 on Ctrl-C
```

```python
# web.transcript; cmd_show iterates the same steps
with _reading(cfg, sweep=True) as (archive, swept):
    session = archive.session(sid)
    steps = conversation.build(archive.lines(sid, aid), session.agents, aid)
return Response(200, HTML, document(transcript_page(root, session, aid, steps, at, Form.read(query), hidden, swept)))
```

```python
# a search hit's link, from the address search() no longer drops, with the whole search
href_transcript(hit.session.id, hit.best.agent_id, Key(hit.best.line_off, hit.best.block), form)
```

## Shape

Data first. `conversation.build(lines, agents, agent_id) -> list[Step]` is a pure function over `Archive.lines()`. A `Step` is `Said | Call | Output | Thought | Image | Marker`, and each carries a `Key(line_off, block)`, the address `entry` and the search excerpt already use. A `Call` holds its `result` and its `spawned` subagent, so pairing and subagent links are decided once, in Python, for both `show` and the pages (per model-the-domain). A step's text is the Record's own string, the one `text_of` indexed, so a page cannot drift from what search matched.

| Module | Owns | Change |
|---|---|---|
| `record.py` | Claude Code fields | `tool_use_id` and `error` on result blocks; `Turn.extras` (`Thinking`, `Picture`); `Other.note` (five `Notice`s); `SubagentMeta.parent`; `tool_input(block)`; `image(line, block, part)` |
| `queries.py` | new, moved out of `archive.py` | `SESSIONS_SQL`, `SEARCH_SQL`, unchanged |
| `archive.py` | storage | `open(readonly=True)`; `lines()` replaces `conversation()` and `Message`; `line()`; `Subagent.tool_use_id`, `.parent`; `Excerpt.line_off`, `.block` |
| `conversation.py` | new: what a transcript shows | `Key`, `Step`, `build` |
| `human.py` | new, moved out of `cli.py` | `HIDDEN`, `escape`, `printable`, `parse_when`, `when`, `short`, `clip`, `size`, `ago` |
| `search_text.py` | FTS dialect | `marks(text, terms)`, which `excerpt` reuses |
| `markup.py` | new: the markup kernel | `Html`, `text`, `marked`, `el`, `page`, `document`, the bundled fonts |
| `pages.py` | new: what each page shows | the page views, the `href_*` builders, `Form` and its filter table |
| `web.py`, `web.css` | new: HTTP | `bind`, `serve`, the handler and routes, `Response`; the CSS sits flat beside `schema.sql`, which ships |
| `cli.py` | argv | `cmd_serve`; `cmd_show` reads `build` |

**1. Process and transport.** `claudinator serve` runs a stdlib `ThreadingHTTPServer` in the foreground until Ctrl-C. There is no build step and no JavaScript. The server writes every page as HTML. `<details>` collapses, a URL fragment lands, a checkbox hides the tool calls, and a GET form searches and filters.

**2. Reading the archive.** Every request opens its own `Archive.open(cfg, readonly=True)` on its own thread, so threads share no connection, cache, or state (per separate-before-serializing-shared-state). A read-only open never creates a database. It connects with `?mode=ro`, so SQLite refuses every write, and it refuses any stamp but its own instead of rebuilding. On a WAL test database, a `mode=ro` connection failed every write with "attempt to write a readonly database", saw another connection's commits, and did not create a missing file. The check runs per request, so a server left running across a reinstall says "restart serve" instead of reading a newer projection with older SQL. The list, search, and transcript pages sweep first, as every CLI read command does, with `sweep(wait=False)` on a second connection opened only after the read-only open accepted the stamps. That sweep gives up at a busy lock and is never recorded, so `last_sweep` stays the launchd sweeper's and `status` still flags a dead one.

**3. New read capability.** Field knowledge goes into `record.py`, storage access into `archive.py`, and presentation into `conversation.py` and `pages.py` (per boundary-discipline). `project()` reads `Turn.blocks` only, so thinking, pictures, and notes are shown and never indexed. `PROJECTION_VERSION` stays 8, and the unchanged golden test proves it. The pages show thinking that has text, pasted and tool-result images, and five notices: queued prompts (68 of 111 are otherwise invisible), compaction, interrupts, API errors, and background notifications. A run of consecutive lines that make no step folds into one collapsed `Marker` that counts its lines by record type, unknown types by name, and lists each line with its raw link. So every archived line is one click from the page, and a record type Claude Code adds later shows up instead of vanishing. A search hit on a unit that makes no step, such as a title, opens the transcript at its top. Older generations get no view, because none exist and `export --generation` reaches them. The back link from a nested subagent comes from meta's `parentAgentId`, which all 20 nested subagents carry, so no transcript is scanned. Neither the list nor a transcript is paged. `archive.py` first loses its two SQL statements to `queries.py`, a pure move no test patches, so it has room before it gains `lines()`, `line()`, and the read-only open.

**4. Security.** The server binds 127.0.0.1 on a free port. Each run makes a 256-bit token and serves everything under the path prefix `/<token>/`, where the printed URL points. A request whose path does not start with that exact prefix gets 403 and no data, and the prefix is compared with `hmac.compare_digest`. Only `/favicon.ico` answers outside it, with an empty 204. No response sets or reads a cookie. Cookies are scoped by host, not by port (RFC 6265 §8.5), and SameSite counts every port of 127.0.0.1 as one site, so a cookie would reach every other local server the browser visits, which could replay it. A path prefix goes only to this port. Each page carries one `<base href="/<token>/">`, and every link, form action, stylesheet, and image on it is relative to that base. So the secret sits in one element per page, the link builders never see it, and a page that lost its base would break its links but still never point outside the prefix. The one redirect, from a subagent's back link to its call, is the prefix plus a link built from validated ids, never from the request path. `Referrer-Policy: no-referrer` keeps the prefix out of every request a page could make elsewhere. The default browser gets the URL from osascript on stdin and never from a command line, where `ps` would show it to other users, so `serve` ignores `BROWSER`. Every request also needs a Host of `127.0.0.1:<port>` or `localhost:<port>`, which stops DNS rebinding. On port 80 the bare name counts too, because a browser leaves the default port out of Host, though binding 127.0.0.1:80 takes root on this Mac. A request with an `Origin` header that is not the server's own is refused. Element ids come only from a `Key`'s integers, never from transcript text. Nothing is logged, because paths carry search words and the token. Pages go out with `default-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'`, so no script runs whatever a transcript says, and a base element can point only at this server. Text becomes markup only through `el()` and `text()`. They escape it and show each `HIDDEN` character as a visible `\u202e` span, and CSS isolates bidi text in its element. An AST test walks `markup.py`, `pages.py`, and `web.py` and fails on any other `Html(...)`, the guard `cli._emit` already has (per encode-lessons-in-structure). Raw lines are ASCII-escaped `text/plain`, images are raster types only, and both carry `sandbox`.

**5. Large sessions.** A page holds the whole transcript with full text, which the measurements allow. A throwaway prototype read and paired the 78.9 MB session and the transcript with the most text in 0.42 and 0.15 s and wrote their 1.8 and 3.6 MB of HTML in under 0.03 s. Headless Chrome loaded and dumped them in 0.5 and 1.8 s past its 0.74 s startup. Images load lazily by URL, tool-result images only once their output is opened. A block past 1 MiB is cut with a raw link, 14 times the largest today.

**6. Tests.** Each test names the break that turns it red. Pure tests cover `build` (parallel pairing, forks, blank answers) and the pages (exact escaped fragments). A `Server` on port 0 driven by `http.client` covers the prefix, Host, headers, raw lines, images, the download, and a sweep-first page that leaves `last_sweep` unchanged. Archive tests cover the read-only open. The real-browser check runs Chrome from `/Applications` headless with `--dump-dom` on a hostile transcript and asserts the DOM has no script element. The prototype ran that flow on this Mac, cookie redirect included. For the large sessions, drive Chrome over CDP with the `web-browser` skill against a copy of the database.

Interface depth. `cli.py` calls two functions, `bind` and `serve`, which hide the prefix, headers, routing, pages, pairing, sweeping, and the version check. `conversation` exposes one function, and `archive` gains two methods and a flag.

## Synthesis decision

Six runners designed this (opus, fable, sonnet, haiku, and Codex gpt-6.1-sol and gpt-6.1-terra), and a fable cross-judge scored them blind against a six-point rubric. Five converged on a JSON API with a vanilla-JS page, a second display parser beside `parse`, and paged transcripts. The opus design broke from them after measuring that the largest transcript shows 1.3 MB of text, and it became the base. The judge picked it independently for the smallest public interface (`bind`, `serve`, `build`, two archive methods and a flag), one markup sink under an AST test, one transcript reader shared by `show` and the pages, and measured claims that held when the judge rechecked them against the live archive.

Grafts, each folded into the base by hand:

- From sonnet, the SQL move to `queries.py` before anything grows `archive.py`, and the `Origin` check.
- From haiku and sonnet, folding runs of hidden lines into one counted marker with raw links, with unknown record types named, so the page itself reaches every line.
- From terra, opening a hit on a title or another unit with no step at the transcript's top, not at a dead fragment.

Rejected: fable's move of the write path to `sweep.py` (tests patch `archive.project`), sol's worker threads, cursors, and download tickets (the largest interface, built for cases the archive does not have), haiku's per-request `Archive.open` (it can rebuild the live database), sonnet's second reader kept beside `conversation()` by a parity test, fable's token file on disk, and the generation pickers (no source has a generation above 0, and `export --generation` reaches them).

## Tradeoffs accepted

- We accept no JavaScript in exchange for a page policy that forbids scripts outright and every rule in tested Python. Expanding is per element, and new turns need a reload.
- We accept whole-transcript pages, 3.6 MB at most today, in exchange for no body endpoint, find-in-page over every step, and deep links without client code.
- We accept a sweep (about 57 ms) and a full reparse (up to 0.42 s) per page view in exchange for a server with no cache and no shared state (per laziness-protocol).
- We accept a new token per run, so old tabs and bookmarks stop working after a restart, in exchange for no secret stored on disk.
- We accept that `show` prints a tool's output right after its own call when Claude ran calls in parallel. Only that case changes the CLI's output.
- We accept that thinking, queued prompts, and notices are visible and not searchable, in exchange for no projection change.
- We accept times in the server's local zone, which on a single-user Mac is the browser's.

## Alternatives considered

- **A JSON API with a vanilla-JS page that renders through `textContent`.** Inline expanding would be smoother. It moves pairing and presentation into a language pytest does not reach, adds a JSON schema both sides must keep in step, needs `script-src 'self'` plus discipline at every DOM sink, and must convert excerpt marks from code points to UTF-16. Its interface is larger and hides nothing more, since whole pages measured under 4 MB.
- **Paged transcripts with bodies fetched on demand.** I started here. The measurements removed its reason. Displayable text is small, and the large images already load lazily. Paging would break find-in-page, pairing across page edges, and fragment landing.
- **A static HTML export.** It writes every secret into files outside the 0600 database, goes stale between runs, and cannot search.
- **An always-on server under launchd.** A listener that never stops widens exposure for a tool used now and then, and it would need a stored token.

## Implementation reconciliation

- **Blank text is dropped in `parse`, not in `build`.** `parse` already drops what a Turn cannot show, so it now drops prompt and reply blocks whose text is blank as well. A blank tool result stays, because it still answers its call. Every Turn then makes at least one step, and whether a line folds into a hidden run follows from its record alone: an `Other` with no note, a `Title`, or an `Unparsed`. `project()` emits no entry for blank text either way, so the golden digest is unchanged.
- **`Marker` holds `Note | Hidden`, and torn lines are counted, not marked.** A notice and a hidden run are the two things a `Marker` shows, so it carries one field of that union instead of two optional ones. A torn line counts as `unparsed` inside its hidden run, beside the other types, rather than taking a marker of its own. `record.line_type()` names each counted line: an `Other`'s own `type`, which `Other` now keeps, `ai-title`, `custom-title`, or `unparsed`.
- **Queued attachments from agents and background tasks are notifications.** On this Mac 113 `queued_command` attachments come from a person and 701 from a peer or a task notification, and none of the 701 appears again as a user line (a structure-only scan). So `QUEUED` covers the ones a person typed, and the rest are `NOTIFICATION`, beside the user lines with those origins.
- **`Output` keeps `tool_use_id`.** `build` pairs a result with its call by that id, so the step carries it.
- **`human.py` takes only what the pages use.** `ago` stays in `cli.py`, because only `status` prints it. `LARGEST` moves with `parse_when`, which checks against it, and `cli.py` reads both as `human.*`.
- **`show` changes in one more case than the design names.** A result now prints after its own call whenever anything came between them, a prompt included, not only when calls ran in parallel. A fork's copy of the call that started it no longer points at the fork.
- **A back link resolves its call on the server.** Element ids come only from `Key` integers, so the sketch's `#call-<tool_use_id>` anchor is gone. A subagent page links `/s/<sid>[/<parent>]?call=<tool_use_id>`, and that transcript answers with a 303 to `?at=<key>#m<key>` of the call. A click on a back link therefore builds the parent's steps twice.
- **No banner for an `at` that names no step.** Title hits link without `at`, and no source has a generation above 0, so the case has no source today. The page then opens at its top.
- **Search shows at most 50 sessions.** The page says "50 sessions, the best of more" at that limit. A bad since or until, or words with no letter or digit, answer 400 with the form and the reason.
- **A result starts collapsed.** Every call is one closed row, so a result's images load only when the reader opens it.
- **Every response goes through one sender.** `http.server` answers unsupported methods and malformed requests through `send_error`, which `web.py` routes to the same headers as a page.
- **`web.css` ships.** A wheel built from this branch with `uv build` holds `claudinator/web.css` beside `schema.sql`.
- **Hidden runs start hidden.** In a 25-prompt session 138 of 312 steps were hidden-run markers, one between almost every call. A transcript leaves them out unless its URL has `hidden=1`, and its aside counts the hidden lines with a link that toggles them and keeps `at`, `q`, and the fragment. So a hidden line is two clicks from the page. Notices always show.
- **A command's output shows without Claude Code's wrappers.** `record.printed()` splits the indexed text of a `!` command or a slash command into its stdout and stderr, and `record.task_summary()` reads a task notification's `<summary>` for its collapsed line. Both are display functions, so the index and `text_of` keep the wrapped text and the golden digest is unchanged. A page marks words in what it shows, so a hit still marks the matched words.
- **The token is a path prefix, not a cookie.** Two security reviews found that the `claudinator-<port>` cookie went to every other server on 127.0.0.1 the browser visited, because a cookie does not isolate ports. Every route now sits under `/<token>/`, and no response sets a cookie. The prefix reaches the links through one `<base href>` per page with relative links, not a root argument threaded through every link builder. Only the routes, the page functions, and `page()` take the root. The policy's `base-uri` is `'self'` instead of `'none'`, which a base element needs. The token now shows in the address bar and the history, which the cookie's sign-in redirect avoided, and a new token per run bounds how long it works.
- **A raw line is the record `records()` read there.** When a restarted writer appends a whole record onto a torn tail that settled, the two share one physical line. `line()` splits it the way `records()` does, so the torn bytes and the glued record each have their own raw line and images, and an offset inside either still answers 404.
- **Code fences pair in one pass.** A regex that matched each opening fence to its closer rescanned the rest of the text for every opener without one: 19 s for a 100 KB reply of unclosed openers, with the GIL held so every other request waited. The fence lines are now listed once and each opener takes the next bare closer, and a 1 MiB reply of unclosed openers renders in about 30 ms. Search marks in prose are found once over the whole text and split across its plain and code parts, so a phrase that runs into a code span is marked on both sides.
- **Every step is laid out.** With `content-visibility: auto` and a 2.5rem placeholder per step, a search hit landed out of view in 6 of 6 headless Chrome runs on the real copy (target top 2,287 px or 809 px in a 900 px viewport), because the steps above it changed height as they rendered. Without it the same hit landed at 705 px in all 6 runs, at the page's end, and a mid-page hit at 0 px. The largest transcript's load event moved from about 45 ms to 75 to 100 ms after its last byte.

## The look

The pages follow the user's mockup: `Claudinator.dc.html` for the dark theme (Graphite) and `Paper.dc.html` for the light one, with real data and still no JavaScript. The departures from it are listed below with their reasons. Sizes, colors, and breakpoints live in `web.css` and are not repeated here.

- **Fonts.** Geist and Geist Mono ship in `claudinator/fonts/` with their OFL license, and `fonts/<file>` serves the bytes read once at import for the two files it knows and 404s on anything else, so no request path reaches the filesystem. The page policy gains `font-src 'self'` and nothing else. A wheel from `uv build` holds both fonts and the license. Without a preload, headless Chrome painted the first one or two frames of a page with no text at all, because the page's boxes ended Chrome's paint holding before the fonts arrived. Pages now preload both fonts and use `font-display: optional`, and 6 of 6 recorded navigations painted text in their first frame. Fonts are the one response the browser may keep (`max-age`, `immutable`): they are no secret, the token in their URL changes every run, and with `no-store` every page fetched both again and its load event waited on them.
- **Themes.** One set of tokens in `oklch` through `light-dark()` and `color-scheme: light dark`. The mockup's faintest ink fell under 4.5:1 on the raised backgrounds (4.09 and 4.39 on bg3, 4.48 on dark bg2), so it is a step lighter in dark and darker in light, enough to pass AA on every background in both themes.
- **CSS.** Sizes are in rem, and the h1, the page padding, the gaps, and the time gutter grow with `clamp()`. Properties are logical. Hover styles apply only to fine pointers, every focusable thing has a `:focus-visible` ring, everything pressable has an `:active` state, and motion runs only under `prefers-reduced-motion: no-preference`. A whole list row or search result is one link, with the project link above it (the clickable-card pattern).
- **List.** One section per local day under a sticky head with Today or Yesterday. A title splits into a leading slash command as an accent pill, or a leading `# ` heading, and the rest in a lighter ink. `archive.SessionSummary.heading` is the first prompt's `# ` line, set only when the one-line title starts with `# ` and all of it, so the list takes it off the title without checking. A real space separates the parts, so a link's name and a copied title read as words.
- **Transcript.** A back link to the results with every filter they had (when the URL has `q`), to the parent session (on a subagent page), or to all sessions. The facts row, then two columns: the steps and the aside, which sits on the inline end and stays in view on a wide screen, and comes first, full width, on a narrow one. While it stays in view it fits the viewport, and a long subagent list scrolls inside it. Day dividers, the time once per minute in a gutter, prompts numbered from the start of each transcript, and consecutive calls, outputs, and thoughts in one box. A fenced code block shows its info string as a faint first line. A landed hit gets an accent bar in the gutter, and the scroll padding clears the sticky header, so the hit lands just under it.
- **Icons.** A ⌨️ sits beside each prompt and a 🤖 beside each reply, in a narrow column of their own between the time and the steps. Tool boxes and notes move to the message column, so they line up with the text. A session's prompts are always typed by the user (`record.Kind.PROMPT`), but a subagent's prompts come from the agent that started it, so they get 🤖. The page's `main` carries the class `subagent` for that. The icons are CSS generated content, so copying a message never picks one up. Their alt text is empty, because the prompt label already names the speaker. They use `::after`, because `:target::before` already draws a landed hit's bar on the same steps.
- **Tool switch.** The aside's "Tool calls and thinking" switch hides every box. Boxes hold calls, their outputs, standalone outputs, and thinking rows, so the label names thinking too. It counts those rows, one per call, output, or thought step, and a transcript without any has no switch. It starts on, and on means shown. Its state is the checked state of one checkbox in the browser, with no URL parameter, server state, or cookie. A CSS rule with `:has()` on the transcript hides the boxes, so a flip is instant and keeps the reader near their place. A URL toggle like the hidden lines' would reload, about 0.65 s on the largest session, and land at the page's top. On the 2,071-step session (12,592 elements) a flip takes about 33 ms, two frames. A box that holds the landed hit stays, and so does its minute. A minute left with only boxes goes, time and all. One limit is known and left alone. Chrome anchors scrolling to the first minute at the viewport's top, so the text being read moves up by the height of the tool rows that disappear inside that minute, 85 to 515 px in three sessions. `overflow-anchor: none` on the boxes, the minutes, the aside, or the header did not change it.

Departures from the mockup:

- No Copy button beside the resume command and no `/` key hint, because both need script. The command's text selects as a whole on one click.
- Download says `.jsonl ↓`, not `.md`, because it downloads the JSONL.
- A failed call says `error`, not `exit 2`: the archive keeps no exit code. A failed Agent call says it beside its subagent link.
- No `fork` badge, because there is no fork data. A session Claude Code deleted gets a badge in that style that says `deleted`.
- An opened row shows the input JSON above the output, where the mockup shows only the output, so a call reads with what it was given. Each part has its own `raw` link.
- A prompt longer than three lines or 300 characters is set at body size and weight. The mockup's 18 px medium face reads as a heading over a page of text.
- A long note, or a task notification, opens to its full text, with a chevron after its line. The mockup's notes do not open.
- On a narrow screen a list row takes two lines (the title, then the project and counts), and the h1 shrinks with the viewport. The mockup keeps its five columns and 40 px h1 at 390 px, where both overflow.
- The search pills show their placeholders (`any`, `7d`) until the form has values. The mockup shows values it set itself.

The rebuild costs the 78.9 MB transcript some time. It has 12,589 elements where main had 9,984: inline code pills, panels, boxes, prompt labels, and fence labels. On the frozen copy its page build takes 44 ms against 38 to 41 ms on main, beside about 350 ms to parse the transcript on both. Over 5 interleaved loads of each after a warm-up, the load event's median was 646 ms on the branch and 655 ms on main, with overlapping ranges, because the first byte (the sweep and the parse) dominates and varies by more than the gap. The time after the last byte, which covers layout and the web fonts, went from 72 to 77 ms to 86 to 92 ms.

## Open questions and risks

- Should queued prompts and thinking text become searchable in a follow-up, with the version bump and reinstall that takes?
- Should a page show sweeper health? `status()` takes about 234 ms because it walks `~/.claude`, too slow for every view.
- The list loads every session, 120 ms for 292. At a few thousand, first-prompt titles cost about a second. Is keyset paging worth adding then?
- Prose sets fenced and inline code as code and leaves the rest as wrapped text. Are lists, headings, or links worth their code?
- A raw view of the 15 MB line pretty-prints to roughly 30 MB of text in a tab. Should raw views cap?
- Does `uv tool install --reinstall` ship `web.css` as it ships `schema.sql`? The first served page from the installed tool should confirm it.

## Next implementation step

Move the two SQL statements to `queries.py`, then build `conversation.build` over `Archive.lines()` and move `cmd_show` onto it, because both front ends rest on it and the existing exact-output `show` tests check it.
