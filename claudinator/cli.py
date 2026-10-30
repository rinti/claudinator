import argparse
import os
import re
import shlex
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TextIO

from claudinator import launchd
from claudinator.archive import (
    AmbiguousSession,
    Archive,
    ArchiveError,
    Filters,
    Query,
    Session,
    SessionSummary,
    Status,
    SweepReport,
    UnknownSession,
    now,
)
from claudinator.conversation import Call, Output, Said, build
from claudinator.layout import AGENT_ID, Config, staged
from claudinator.record import AgentId, Kind, Millis, cleanup_period_days

_NOTE_ARCHIVED_BYTES = 4 << 20
_LARGEST = 2**62
"""The largest number argv may give. SQLite stores integers in 64 bits, and
a time that far back can still be subtracted from now."""


def main(argv: list[str] | None = None) -> int:
    """claudinator <command> [...]. Exit 0 ok, 1 domain error (unknown or
    ambiguous session, unhealthy sweeper, resume impossible, an unreadable
    database or file), 2 usage error. No error ends in a traceback."""
    try:
        args = _parser(now()).parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    cfg = Config.from_env()
    try:
        code = args.run(cfg, args)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # Point stdout at /dev/null so the flush at exit does not fail again and turn exit 0 into 120.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except AmbiguousSession as e:
        _emit(f"claudinator: {e}; be more specific:", file=sys.stderr)
        for s in e.candidates:
            _emit(f"  {s.id}  {_when(s.last_ts)}  {_short(s.cwd)}  {s.title or ''}", file=sys.stderr)
        return 1
    except (ArchiveError, launchd.LaunchdError, OSError) as e:
        _emit(f"claudinator: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        _emit(f"claudinator: {cfg.db}: {e}", file=sys.stderr)
        return 1


@contextmanager
def _opened(cfg: Config, *, sweep_first: bool) -> Iterator[Archive]:
    with Archive.open(cfg) as archive:
        if sweep_first:
            _note(archive.sweep(wait=False))
        yield archive


def parse_when(text: str, now: Millis) -> Millis:
    """'2026-10-01' (local midnight), '2026-10-01T14:00' (local), '7d', '12h' (ago)."""
    if m := re.fullmatch(r"(\d+)([dh])", text):
        if (ago := int(m[1]) * {"d": 86_400_000, "h": 3_600_000}[m[2]]) > _LARGEST:
            raise ValueError(f"{text!r} is out of range")
        return Millis(now - ago)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            local = datetime.strptime(text, fmt)
        except ValueError:
            continue
        try:
            return Millis(int(local.timestamp()) * 1000)
        except (ValueError, OverflowError, OSError):
            raise ValueError(f"{text!r} is out of range") from None
    raise ValueError(f"expected 2026-10-01, 2026-10-01T14:00, 7d, or 12h, not {text!r}")


def parse_ref(text: str) -> str:
    if re.fullmatch(r"[0-9a-f-]{4,36}", ref := text.lower()):
        return ref
    raise ValueError(f"expected at least 4 characters of an id, not {text!r}")


def parse_agent(text: str) -> str:
    if AGENT_ID.fullmatch(text):
        return text
    raise ValueError(f"expected the start of an agent id (letters, digits, - and _), not {text!r}")


def cmd_search(cfg: Config, args: argparse.Namespace) -> int:
    query = Query(tuple(args.words), Filters(args.project, args.since, args.until), args.tools, args.limit)
    with _opened(cfg, sweep_first=True) as archive:
        hits = archive.search(query)
    tty = sys.stdout.isatty()
    for hit in hits:
        _emit(f"{_session_line(hit.session)}  ({hit.hits} hit{'' if hit.hits == 1 else 's'})")
        best = hit.best
        label = best.kind.value + (f" in agent {best.agent_id[:8]}" if best.agent_id else "")
        _emit(f"    {label}  ", *_marked(best.text, best.marks, tty))
    return 0


def cmd_sessions(cfg: Config, args: argparse.Namespace) -> int:
    with _opened(cfg, sweep_first=True) as archive:
        rows = archive.sessions(Filters(args.project, args.since, args.until), args.limit)
    width = max([len("PROJECT")] + [len(printable(_short(s.cwd))) for s in rows])
    _emit(f"SESSION   LAST ACTIVE       PROMPTS  AGENTS  {'PROJECT':<{width}}  TITLE")
    for s in rows:
        project = _short(s.cwd) + " " * (width - len(printable(_short(s.cwd))))
        title = _clip(s.title or "", 60) + ("" if s.on_disk else "  [gone]")
        _emit(f"{s.id[:8]}  {_when(s.last_ts)}  {s.prompts:>7}  {s.subagents:>6}  {project}  {title}")
    return 0


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    with _opened(cfg, sweep_first=True) as archive:
        session = archive.session(args.id)
        s = session.summary
        _emit(f'{s.id}  {_short(s.cwd)}  "{s.title or ""}"')
        where = "on disk" if s.on_disk else "deleted by Claude Code, kept in the archive"
        prompts = f"{s.prompts} prompt{'' if s.prompts == 1 else 's'}"
        _emit(f"{_when(s.first_ts)} to {_when(s.last_ts)}, {prompts}, {where} (claudinator resume {s.id[:8]})")
        agent = _agent(session, args.agent) if args.agent else None
        for a in session.agents:
            if agent is None or a.id == agent:
                _emit(f"agent {a.id[:8]}  {a.agent_type or ''}  {a.description or ''}".rstrip())
        speaker = None
        for step in build(archive.lines(s.id, agent), session.agents, agent):
            match step:
                case Said(ts=ts, kind=kind, text=text):
                    who, out = "you" if kind is Kind.PROMPT else "claude", [_indent(text)]
                case Call(ts=ts, text=text, spawned=spawned, result=result):
                    who, out = "claude", [f"  > {_clip(' '.join(text.split()), 200)}"]
                    if spawned:
                        out.append(f"    (subagent: claudinator show {s.id[:8]} --agent {spawned.id[:8]})")
                    if result:
                        out.append(_output(result.text, args.full))
                case Output(ts=ts, text=text):
                    who, out = "claude", [_output(text, args.full)]
                case _:
                    continue
            if who != speaker:
                _emit(f"\n{_when(ts)}  {who}")
                speaker = who
            for line in out:
                _emit(line)
    return 0


def _output(text: str, full: bool) -> str:
    lines = text.splitlines() or [""]
    if full:
        return "  < " + "\n    ".join(lines)
    more = f"  ({len(lines)} lines)" if len(lines) > 1 else ""
    return f"  < {_clip(lines[0], 160)}{more}"


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    """Restore the main transcript if Claude Code deleted it, then replace
    this process with `claude --resume` in the session's directory. Checks
    that both can work before it writes anything."""
    with _opened(cfg, sweep_first=True) as archive:
        s = archive.session(args.id).summary
        command = ["claude", "--resume", s.id]
        if not s.cwd or not os.path.isdir(s.cwd):
            _emit(f"claudinator: the session's directory {_short(s.cwd)} no longer exists. Resume it from another one with:")
            _emit(f"  {shlex.join(command)}")
            return 1
        if shutil.which("claude") is None:
            _emit(f"claudinator: claude is not on PATH. Run: cd {shlex.quote(s.cwd)} && {shlex.join(command)}")
            return 1
        try:
            _restore(archive, s)
        except OSError as e:
            _emit(f"claudinator: cannot restore {_short(str(s.transcript))}: {e.strerror or e}", file=sys.stderr)
            return 1
    _exec(s.cwd, command)
    return 0


def _restore(archive: Archive, s: SessionSummary) -> None:
    final = s.transcript
    if final.exists():
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    with staged(final) as scratch:
        try:
            with os.fdopen(os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
                archive.export(s.id, f)
                f.flush()
                os.fsync(f.fileno())
            os.link(scratch, final)
        except FileExistsError:
            _emit(f"{_short(str(final))} appeared while restoring it; resuming with that file as it is")
        else:
            _emit(f"restored {_short(str(final))} from the archive")


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    if sys.stdout.isatty():
        _emit("claudinator: export writes the raw transcript; redirect it to a file", file=sys.stderr)
        return 2
    with _opened(cfg, sweep_first=True) as archive:
        session = archive.session(args.id)
        agent = _agent(session, args.agent) if args.agent else None
        archive.export(session.summary.id, sys.stdout.buffer, agent, args.generation)
    sys.stdout.buffer.flush()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    with _opened(cfg, sweep_first=False) as archive:
        swept = archive.sweep(wait=False)
        return _print_status(archive.status(), swept, _cleanup_period(cfg))


def _print_status(st: Status, swept: SweepReport, cleanup_days: int | None) -> int:
    loaded = launchd.loaded()
    last = st.last_sweep
    at = st.checked_at
    stale = last is None or at - last.finished > 2 * launchd.SWEEP_INTERVAL_SECONDS * 1000
    failed = last is not None and bool(last.errors or last.refused)
    failed_now = bool(swept.errors or swept.refused)
    refused = st.last_refused if st.last_refused and (last is None or st.last_refused.at > last.finished) else None
    unarchived = sum(p.unarchived_bytes for p in st.pending)
    _emit(f"archive   {_short(str(st.db_path))}  {_size(st.db_bytes)} ({_size(st.uncompressed_bytes)} of transcripts)")
    _emit(
        f"coverage  {st.transcripts} transcripts in {st.sessions} sessions, {unarchived} unarchived bytes,"
        f" {st.unparsed_lines} unparsed lines, {st.multi_generation} rewritten files"
    )
    sweep = "never swept" if last is None else f"last sweep {_ago(at - last.finished)} ({_report(last)})"
    _emit(f"sweeper   {'loaded' if loaded else 'not loaded (run: claudinator install)'}, {sweep}")
    for error in last.errors[:5] if last else ():
        _emit(f"          {error}")
    if refused:
        _emit(
            f"refused   an older claudinator tried to sweep at {_when(refused.at)} and was refused:"
            f" {refused.reason} (reinstall: claudinator install)"
        )
    if failed_now or swept.transcripts_changed:
        _emit(f"swept     {_report(swept)}")
        for error in swept.errors[:5]:
            _emit(f"          {error}")
    if st.gone:
        _emit(f"gone      {st.gone} transcripts deleted by Claude Code live on in the archive")
    for p in st.pending:
        if p.settled:
            _emit(f"warning   {_short(str(p.path))} has bytes the sweeper has not archived for an hour")
    if n := len(st.unreadable):
        files = "transcript" if n == 1 else "transcripts"
        _emit(f"unread    {n} {files} could not be read to count unarchived bytes: {st.unreadable[0]}")
    if n := len(st.unclassified):
        files = "file under projects/ matches" if n == 1 else "files under projects/ match"
        _emit(f"unknown   {n} .jsonl {files} no known layout and {'is' if n == 1 else 'are'} not archived: {st.unclassified[0]}")
    setting, days = (f"is {cleanup_days}", cleanup_days) if cleanup_days is not None else ("is unset", 30)
    _emit(f"note      cleanupPeriodDays {setting}, so Claude Code deletes transcripts after {days} days.")
    _emit("          The archive keeps them, and `claudinator resume` restores one before resuming it.")
    return 1 if not loaded or failed or failed_now or refused or stale or st.unreadable or st.unclassified else 0


def cmd_install(cfg: Config, args: argparse.Namespace) -> int:
    executable = _executable()
    paths = {"CLAUDINATOR_DB": cfg.db, "CLAUDE_CONFIG_DIR": cfg.claude_dir}
    env = {var: str(path) for var, path in paths.items() if os.environ.get(var)}
    with _opened(cfg, sweep_first=False) as archive:
        changed = launchd.install(executable, cfg.db.parent / "sweep.log", env)
        state = "installed" if changed else "already installed"
        _emit(f"{state} {launchd.LABEL}: `{executable} sweep` hourly and at login")
        progress = _progress if sys.stderr.isatty() else None
        report = archive.sweep(wait=True, progress=progress)
        if progress:
            _emit(file=sys.stderr)
        return _print_status(archive.status(), report, _cleanup_period(cfg))


def _cleanup_period(cfg: Config) -> int | None:
    try:
        return cleanup_period_days((cfg.claude_dir / "settings.json").read_bytes())
    except OSError:
        return None


def cmd_uninstall(cfg: Config, args: argparse.Namespace) -> int:
    launchd.uninstall()
    kept = f"; the archive stays at {_short(str(cfg.db))}" if cfg.db.exists() else ""
    _emit(f"unloaded {launchd.LABEL}{kept}")
    return 0


def cmd_sweep(cfg: Config, args: argparse.Namespace) -> int:
    with _opened(cfg, sweep_first=False) as archive:
        report = archive.sweep(wait=True)
    _emit(f"{datetime.fromtimestamp(report.finished / 1000).isoformat(timespec='seconds')} {_report(report)}")
    for error in report.errors:
        _emit(f"  {error}")
    return 1 if report.errors or report.refused else 0


def cmd_rebuild(cfg: Config, args: argparse.Namespace) -> int:
    with _opened(cfg, sweep_first=False) as archive:
        replayed = archive.rebuild(force=True)
    _emit(f"rebuilt the search index from {replayed} archived transcript generations")
    return 0


def _parser(now: Millis) -> argparse.ArgumentParser:
    def checked(parse: Callable[[str], object]) -> Callable[[str], object]:
        def convert(text: str) -> object:
            try:
                return parse(text)
            except ValueError as e:
                raise argparse.ArgumentTypeError(str(e)) from None

        return convert

    def number(least: int) -> Callable[[str], object]:
        def parse(text: str) -> int:
            if not text.isdigit() or not least <= int(text) <= _LARGEST:
                raise ValueError(f"expected a number from {least} to 2**62, not {text!r}")
            return int(text)

        return checked(parse)

    when = checked(lambda text: parse_when(text, now))
    ref = checked(parse_ref)
    agent = checked(parse_agent)
    parser = argparse.ArgumentParser(
        prog="claudinator", description="A permanent, searchable archive of every Claude Code transcript."
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, run: Callable[[Config, argparse.Namespace], int], about: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=about)
        p.set_defaults(run=run)
        return p

    def window(p: argparse.ArgumentParser, limit: int) -> None:
        p.add_argument("--project", metavar="P", help="part of the session's directory")
        p.add_argument("--since", type=when, metavar="WHEN", help="2026-10-01, 2026-10-01T14:00, 7d, or 12h")
        p.add_argument("--until", type=when, metavar="WHEN")
        p.add_argument("--limit", type=number(1), default=limit, metavar="N")

    p = command("search", cmd_search, "find sessions where every word appears")
    p.add_argument("words", nargs="+", metavar="WORDS", help='"a phrase" in quotes, deploy* for a prefix')
    p.add_argument("--tools", action="store_true", help="also search tool calls and tool output")
    window(p, 20)
    window(command("sessions", cmd_sessions, "list sessions by last activity"), 30)
    p = command("show", cmd_show, "print a conversation from the archive")
    p.add_argument("id", type=ref, metavar="ID")
    p.add_argument("--agent", type=agent, metavar="AID", help="print one subagent's transcript")
    p.add_argument("--full", action="store_true", help="print tool output in full")
    p = command("resume", cmd_resume, "run claude --resume, restoring a deleted transcript first")
    p.add_argument("id", type=ref, metavar="ID")
    p = command("export", cmd_export, "write the archived transcript byte for byte")
    p.add_argument("id", type=ref, metavar="ID")
    p.add_argument("--agent", type=agent, metavar="AID")
    p.add_argument("--generation", type=number(0), metavar="N", help="an earlier generation of a rewritten file")
    command("status", cmd_status, "archive size, coverage, and sweeper health")
    command("sweep", cmd_sweep, "archive new bytes now")
    command("rebuild", cmd_rebuild, "re-derive the search index from the archive")
    command("install", cmd_install, "load the hourly sweeper and archive everything on disk")
    command("uninstall", cmd_uninstall, "unload the sweeper; the database stays")
    return parser


def _agent(session: Session, ref: str) -> AgentId:
    matches = [a.id for a in session.agents if a.id.startswith(ref)]
    if len(matches) != 1:
        found = "no" if not matches else f"{len(matches)}"
        raise UnknownSession(f"{found} subagents of {session.summary.id[:8]} start with {ref}")
    return matches[0]


def _exec(cwd: str, argv: list[str]) -> None:
    os.chdir(cwd)
    os.execvp(argv[0], argv)


def _executable() -> Path:
    running = Path(sys.argv[0])
    for candidate in (running if running.name == "claudinator" else None, shutil.which("claudinator")):
        if candidate and Path(candidate).is_file():
            return Path(candidate).absolute()
    raise launchd.LaunchdError(
        "cannot find the claudinator executable for the sweeper to run; install it with `uv tool install` first"
    )


def _note(report: SweepReport) -> None:
    if n := len(report.errors):
        _emit(
            f"claudinator: {n} error{'' if n == 1 else 's'} while archiving new bytes, the first: {report.errors[0]}",
            file=sys.stderr,
        )
    if report.refused:
        _emit(f"claudinator: {report.refused}; showing what it archived", file=sys.stderr)
    elif report.busy:
        _emit("claudinator: another sweep is writing; showing what is already archived", file=sys.stderr)
    if report.bytes_archived > _NOTE_ARCHIVED_BYTES:
        _emit(f"claudinator: archived {_size(report.bytes_archived)} of new transcript bytes", file=sys.stderr)


def _progress(path: Path) -> None:
    _emit(Markup("\r"), f"archiving {_clip(path.name, 60):<60}", end="", file=sys.stderr, flush=True)


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
    return f"{s.id[:8]}  {_when(s.last_ts)}  {_short(s.cwd)}  {_clip(s.title or '', 60)}{gone}"


def _marked(text: str, marks: tuple[tuple[int, int], ...], tty: bool) -> list[str]:
    start, end = (Markup("\x1b[1m"), Markup("\x1b[0m")) if tty else ("[", "]")
    out, at = [], 0
    for s, e in marks:
        out += [text[at:s], start, text[s:e], end]
        at = e
    return [*out, text[at:]]


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.strip("\n").splitlines())


class Markup(str):
    """Terminal sequences the CLI writes itself, such as bold for a search
    match, which _emit passes through as they are."""


def _emit(*parts: str, file: TextIO | None = None, end: str = "\n", flush: bool = False) -> None:
    text = "".join(part if isinstance(part, Markup) else printable(part) for part in parts)
    print(text, file=file or sys.stdout, end=end, flush=flush)


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[\u061c\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def printable(text: str) -> str:
    """text with every C0 and C1 control character except newline and tab
    written as a visible \\xNN escape, and every bidi control, zero-width
    character, line or paragraph separator, and byte-order mark as \\uNNNN."""
    text = _CONTROL.sub(lambda m: f"\\x{ord(m[0]):02x}", text)
    return _INVISIBLE.sub(lambda m: f"\\u{ord(m[0]):04x}", text)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _short(path: str | None) -> str:
    if not path:
        return "?"
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


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

