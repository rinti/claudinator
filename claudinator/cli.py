"""claudinator's command line. The boundary: every string from argv and the
environment is parsed here, and every line a person reads is formatted here."""

import argparse
import os
import re
import shlex
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from claudinator import launchd
from claudinator.archive import (
    SETTLE_SECONDS,
    AmbiguousSession,
    Archive,
    ArchiveError,
    Config,
    Filters,
    Query,
    Session,
    SessionSummary,
    SweepReport,
    UnknownSession,
)
from claudinator.record import AgentId, Kind, Millis, cleanup_period_days

READ_COMMANDS = frozenset({"search", "sessions", "show", "resume", "export", "status"})
"""Commands that sweep with wait=False before answering, so results include
the turn that just finished in another terminal without any hook."""

_NOTE_ARCHIVED_BYTES = 4 << 20


def main(argv: list[str] | None = None) -> int:
    """claudinator <command> [...]. Exit 0 ok, 1 domain error (unknown or
    ambiguous session, unhealthy sweeper, resume impossible, an unreadable
    database or file), 2 usage error. No error ends in a traceback."""
    try:
        args = _parser(_now_ms()).parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    cfg = Config.from_env()
    try:
        with Archive.open(cfg) as archive:
            if args.command in READ_COMMANDS:
                _note(archive.sweep(wait=False))
            return HANDLERS[args.command](archive, args)
    except AmbiguousSession as e:
        print(f"claudinator: {e}; be more specific:", file=sys.stderr)
        for s in e.candidates:
            print(f"  {s.id}  {_when(s.last_ts)}  {_short(s.cwd)}  {printable(s.title or '')}", file=sys.stderr)
        return 1
    except (ArchiveError, launchd.LaunchdError, OSError) as e:
        print(f"claudinator: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"claudinator: {cfg.db}: {e}", file=sys.stderr)
        return 1


def parse_when(text: str, now: Millis) -> Millis:
    """'2026-10-01' (local midnight), '2026-10-01T14:00' (local), '7d', '12h' (ago)."""
    if m := re.fullmatch(r"(\d+)([dh])", text):
        return Millis(now - int(m[1]) * {"d": 86_400_000, "h": 3_600_000}[m[2]])
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            return Millis(int(datetime.strptime(text, fmt).timestamp()) * 1000)
        except ValueError:
            continue
    raise ValueError(f"expected 2026-10-01, 2026-10-01T14:00, 7d, or 12h, not {text!r}")


def parse_ref(text: str) -> str:
    """Session or agent id prefix: [0-9a-f-]{4,36}, lowercased."""
    if re.fullmatch(r"[0-9a-f-]{4,36}", ref := text.lower()):
        return ref
    raise ValueError(f"expected at least 4 characters of an id, not {text!r}")


def cmd_search(archive: Archive, args: argparse.Namespace) -> int:
    filters = Filters(args.project, args.since, args.until)
    tty = sys.stdout.isatty()
    for hit in archive.search(Query(tuple(args.words), filters, args.tools, args.limit)):
        print(f"{_session_line(hit.session)}  ({hit.hits} hit{'' if hit.hits == 1 else 's'})")
        best = hit.best
        label = best.kind.value + (f" in agent {best.agent_id[:8]}" if best.agent_id else "")
        print(f"    {label}  {_marked(best.text, best.marks, tty)}")
    return 0


def cmd_sessions(archive: Archive, args: argparse.Namespace) -> int:
    rows = archive.sessions(Filters(args.project, args.since, args.until), args.limit)
    width = max([len("PROJECT")] + [len(_short(s.cwd)) for s in rows])
    print(f"SESSION   LAST ACTIVE       PROMPTS  AGENTS  {'PROJECT':<{width}}  TITLE")
    for s in rows:
        title = printable(_clip(s.title or "", 60)) + ("" if s.on_disk else "  [gone]")
        print(f"{s.id[:8]}  {_when(s.last_ts)}  {s.prompts:>7}  {s.subagents:>6}  {_short(s.cwd):<{width}}  {title}")
    return 0


def cmd_show(archive: Archive, args: argparse.Namespace) -> int:
    session = archive.session(args.id)
    s = session.summary
    print(f'{s.id}  {_short(s.cwd)}  "{printable(s.title or "")}"')
    where = "on disk" if s.on_disk else "deleted by Claude Code, kept in the archive"
    prompts = f"{s.prompts} prompt{'' if s.prompts == 1 else 's'}"
    print(f"{_when(s.first_ts)} to {_when(s.last_ts)}, {prompts}, {where} (claudinator resume {s.id[:8]})")
    agent = _agent(session, args.agent) if args.agent else None
    for a in session.agents:
        if agent is None or a.id == agent:
            print(printable(f"agent {a.id[:8]}  {a.agent_type or ''}  {a.description or ''}".rstrip()))
    speaker = None
    for m in archive.conversation(s.id, agent):
        if not m.text.strip() and m.kind is not Kind.TOOL_OUTPUT:
            continue
        if (who := "you" if m.kind is Kind.PROMPT else "claude") != speaker:
            print(f"\n{_when(m.ts)}  {who}")
            speaker = who
        match m.kind:
            case Kind.TOOL_CALL:
                print(f"  > {printable(_clip(' '.join(m.text.split()), 200))}")
                if m.spawned:
                    print(f"    (subagent: claudinator show {s.id[:8]} --agent {m.spawned[:8]})")
            case Kind.TOOL_OUTPUT:
                lines = m.text.splitlines() or [""]
                if args.full:
                    print("  < " + "\n    ".join(map(printable, lines)))
                else:
                    more = f"  ({len(lines)} lines)" if len(lines) > 1 else ""
                    print(f"  < {printable(_clip(lines[0], 160))}{more}")
            case _:
                print(_indent(m.text))
    return 0


def cmd_resume(archive: Archive, args: argparse.Namespace) -> int:
    """Restore the main transcript if Claude Code deleted it (create-only,
    never overwriting a file), then replace this process with
    `claude --resume` in the session's directory. The only writer under
    ~/.claude."""
    s = archive.session(args.id).summary
    s.transcript.parent.mkdir(parents=True, exist_ok=True)
    try:
        restore = open(s.transcript, "xb")
    except FileExistsError:
        pass
    else:
        try:
            with restore:
                archive.export(s.id, restore)
        except BaseException:
            s.transcript.unlink()
            raise
        print(f"restored {_short(str(s.transcript))} from the archive")
    command = ["claude", "--resume", s.id]
    if not s.cwd or not os.path.isdir(s.cwd):
        print(f"claudinator: the session's directory {_short(s.cwd)} no longer exists. Resume it from another one with:")
        print(f"  {shlex.join(command)}")
        return 1
    try:
        _exec(s.cwd, command)
    except FileNotFoundError:
        print(f"claudinator: claude is not on PATH. Run: cd {shlex.quote(s.cwd)} && {shlex.join(command)}")
        return 1
    return 0


def cmd_export(archive: Archive, args: argparse.Namespace) -> int:
    if sys.stdout.isatty():
        print("claudinator: export writes the raw transcript; redirect it to a file", file=sys.stderr)
        return 2
    session = archive.session(args.id)
    agent = _agent(session, args.agent) if args.agent else None
    archive.export(session.summary.id, sys.stdout.buffer, agent, args.generation)
    sys.stdout.buffer.flush()
    return 0


def cmd_status(archive: Archive, args: argparse.Namespace) -> int:
    """Exit 1 when the sweeper is not loaded, the last sweep failed, the
    last sweep is older than two intervals, or a transcript sits in a layout
    claudinator does not know (so it is not being archived)."""
    st = archive.status()
    loaded = launchd.loaded()
    last = st.last_sweep
    now = _now_ms()
    stale = last is None or now - last.finished > 2 * launchd.SWEEP_INTERVAL_SECONDS * 1000
    failed = last is not None and bool(last.errors or last.refused)
    unarchived = sum(p.unarchived_bytes for p in st.pending)
    print(f"archive   {_short(str(st.db_path))}  {_size(st.db_bytes)} ({_size(st.archived_bytes)} of transcripts)")
    print(
        f"coverage  {st.transcripts} transcripts in {st.sessions} sessions, {unarchived} unarchived bytes,"
        f" {st.unparsed_lines} unparsed lines, {st.multi_generation} rewritten files"
    )
    sweep = "never swept" if last is None else f"last sweep {_ago(now - last.finished)} ({_report(last)})"
    print(f"sweeper   {'loaded' if loaded else 'not loaded (run: claudinator install)'}, {sweep}")
    for error in last.errors[:5] if last else ():
        print(f"          {error}")
    if st.gone:
        print(f"gone      {st.gone} transcripts deleted by Claude Code live on in the archive")
    for p in st.pending:
        if now - p.mtime > SETTLE_SECONDS * 1000:
            print(f"warning   {_short(str(p.path))} has {p.unarchived_bytes} bytes the sweeper has not archived")
    if n := len(st.unclassified):
        files = "file under projects/ matches" if n == 1 else "files under projects/ match"
        print(f"unknown   {n} .jsonl {files} no known layout and {'is' if n == 1 else 'are'} not archived: {st.unclassified[0]}")
    settings = archive.cfg.claude_dir / "settings.json"
    days = cleanup_period_days(settings.read_text()) if settings.exists() else None
    setting, days = (f"is {days}", days) if days is not None else ("is unset", 30)
    print(f"note      cleanupPeriodDays {setting}, so Claude Code deletes transcripts after {days} days.")
    print("          The archive keeps them, and `claudinator resume` restores one before resuming it.")
    return 1 if not loaded or failed or stale or st.unclassified else 0


def cmd_install(archive: Archive, args: argparse.Namespace) -> int:
    executable = _executable()
    changed = launchd.install(executable, archive.cfg.db.parent / "sweep.log")
    state = "installed" if changed else "already installed"
    print(f"{state} {launchd.LABEL}: `{executable} sweep` hourly and at login")
    progress = _progress if sys.stderr.isatty() else None
    report = archive.sweep(wait=True, progress=progress)
    if progress:
        print(file=sys.stderr)
    print(f"swept     {_report(report)}")
    return cmd_status(archive, args)


def cmd_uninstall(archive: Archive, args: argparse.Namespace) -> int:
    launchd.uninstall()
    print(f"unloaded {launchd.LABEL}; the archive stays at {_short(str(archive.cfg.db))}")
    return 0


def cmd_sweep(archive: Archive, args: argparse.Namespace) -> int:
    report = archive.sweep(wait=True)
    print(f"{datetime.now().isoformat(timespec='seconds')} {_report(report)}")
    for error in report.errors:
        print(f"  {error}")
    return 1 if report.errors or report.refused else 0


def cmd_rebuild(archive: Archive, args: argparse.Namespace) -> int:
    archive.rebuild(force=True)
    print(f"rebuilt the search index from {archive.status().transcripts} transcripts")
    return 0


HANDLERS: dict[str, Callable[[Archive, argparse.Namespace], int]] = {
    "search": cmd_search,
    "sessions": cmd_sessions,
    "show": cmd_show,
    "resume": cmd_resume,
    "export": cmd_export,
    "status": cmd_status,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "sweep": cmd_sweep,
    "rebuild": cmd_rebuild,
}


def _parser(now: Millis) -> argparse.ArgumentParser:
    def checked(parse: Callable[[str], object]) -> Callable[[str], object]:
        def convert(text: str) -> object:
            try:
                return parse(text)
            except ValueError as e:
                raise argparse.ArgumentTypeError(str(e)) from None

        return convert

    def positive(text: str) -> int:
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"expected a positive number, not {text!r}")
        return int(text)

    when = checked(lambda text: parse_when(text, now))
    ref = checked(parse_ref)
    parser = argparse.ArgumentParser(
        prog="claudinator", description="A permanent, searchable archive of every Claude Code transcript."
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def window(p: argparse.ArgumentParser, limit: int) -> None:
        p.add_argument("--project", metavar="P", help="part of the session's directory")
        p.add_argument("--since", type=when, metavar="WHEN", help="2026-10-01, 2026-10-01T14:00, 7d, or 12h")
        p.add_argument("--until", type=when, metavar="WHEN")
        p.add_argument("--limit", type=checked(positive), default=limit, metavar="N")

    p = sub.add_parser("search", help="find sessions where every word appears")
    p.add_argument("words", nargs="+", metavar="WORDS", help='"a phrase" in quotes, deploy* for a prefix')
    p.add_argument("--tools", action="store_true", help="also search tool calls and tool output")
    window(p, 20)
    window(sub.add_parser("sessions", help="list sessions by last activity"), 30)
    p = sub.add_parser("show", help="print a conversation from the archive")
    p.add_argument("id", type=ref, metavar="ID")
    p.add_argument("--agent", type=ref, metavar="AID", help="print one subagent's transcript")
    p.add_argument("--full", action="store_true", help="print tool output in full")
    p = sub.add_parser("resume", help="run claude --resume, restoring a deleted transcript first")
    p.add_argument("id", type=ref, metavar="ID")
    p = sub.add_parser("export", help="write the archived transcript byte for byte")
    p.add_argument("id", type=ref, metavar="ID")
    p.add_argument("--agent", type=ref, metavar="AID")
    p.add_argument("--generation", type=int, metavar="N", help="an earlier generation of a rewritten file")
    sub.add_parser("status", help="archive size, coverage, and sweeper health")
    sub.add_parser("sweep", help="archive new bytes now")
    sub.add_parser("rebuild", help="re-derive the search index from the archive")
    sub.add_parser("install", help="load the hourly sweeper and archive everything on disk")
    sub.add_parser("uninstall", help="unload the sweeper; the database stays")
    return parser


def _agent(session: Session, ref: str) -> AgentId:
    matches = [a.id for a in session.agents if a.id.startswith(ref)]
    if len(matches) != 1:
        found = "no" if not matches else f"{len(matches)}"
        raise UnknownSession(f"{found} subagents of {session.summary.id[:8]} start with {ref}")
    return matches[0]


def _exec(cwd: str, argv: list[str]) -> None:
    """Replace this process. The tests replace this."""
    os.chdir(cwd)
    os.execvp(argv[0], argv)


def _executable() -> Path:
    if Path(sys.argv[0]).name == "claudinator":
        return Path(sys.argv[0]).absolute()
    if found := shutil.which("claudinator"):
        return Path(found)
    raise launchd.LaunchdError("claudinator is not on PATH; install it with `uv tool install` first")


def _note(report: SweepReport) -> None:
    if report.refused:
        print(f"claudinator: {report.refused}; showing what it archived", file=sys.stderr)
    elif report.busy:
        print("claudinator: another sweep is writing; showing what is already archived", file=sys.stderr)
    if report.bytes_archived > _NOTE_ARCHIVED_BYTES:
        print(f"claudinator: archived {_size(report.bytes_archived)} of new transcript bytes", file=sys.stderr)


def _progress(path: Path, archived: int) -> None:
    print(f"\rarchiving {_clip(path.name, 60):<60}", end="", file=sys.stderr, flush=True)


def _report(r: SweepReport) -> str:
    text = f"{r.transcripts_changed} of {r.transcripts_seen} transcripts changed, {_size(r.bytes_archived)}"
    if r.new_generations:
        text += f", {r.new_generations} rewritten"
    if r.errors:
        text += f", {len(r.errors)} errors"
    if r.refused:
        text += f", refused: {r.refused}"
    return text


def _session_line(s: SessionSummary) -> str:
    gone = "" if s.on_disk else "  [gone]"
    return f"{s.id[:8]}  {_when(s.last_ts)}  {_short(s.cwd)}  {printable(_clip(s.title or '', 60))}{gone}"


def _marked(text: str, marks: tuple[tuple[int, int], ...], tty: bool) -> str:
    start, end = ("\x1b[1m", "\x1b[0m") if tty else ("[", "]")
    out, at = [], 0
    for s, e in marks:
        out += [printable(text[at:s]), start, printable(text[s:e]), end]
        at = e
    return "".join(out) + printable(text[at:])


def _indent(text: str) -> str:
    return "\n".join("  " + printable(line) for line in text.strip("\n").splitlines())


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """text with every C0 and C1 control character except newline and tab
    written as a visible \\xNN escape. Transcripts hold escape sequences
    that would recolour the terminal, move the cursor, or, through OSC 52,
    set the clipboard. Every transcript string the CLI prints goes through
    here; export does not, because it writes bytes, not a terminal view."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m[0]):02x}", text)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _short(path: str | None) -> str:
    if not path:
        return "?"
    home = str(Path.home())
    return printable("~" + path[len(home) :] if path == home or path.startswith(home + "/") else path)


def _when(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms is not None else "?"


def _ago(ms: int) -> str:
    minutes = ms // 60_000
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def _size(n: int) -> str:
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= scale:
            return f"{n / scale:.3g} {unit}"
    return f"{n} B"


def _now_ms() -> Millis:
    return Millis(time.time_ns() // 1_000_000)
