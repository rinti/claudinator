from __future__ import annotations

import os
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

from claudinator.record import Record, Unparsed, parse

SEGMENT_BYTES = 1 << 20


def pread(fd: int, n: int, offset: int) -> bytes:
    """Exactly n bytes at offset, or fewer only at end of file."""
    parts = []
    while n > 0:
        part = os.pread(fd, n, offset)
        if not part:
            break
        parts.append(part)
        n -= len(part)
        offset += len(part)
    return b"".join(parts)


def next_segment(fd: int, start: int, size: int, settled: bool) -> tuple[bytes, bool]:
    """The bytes of the next segment of a file, from start: through the
    first newline at or past SEGMENT_BYTES, else through the last newline
    before size, else, once the file has settled, through size. So every
    segment ends with a newline except a settled tail, and no line is split
    across segments except a settled tail and its later continuation. Reads
    at most one line past SEGMENT_BYTES, so memory follows the longest line,
    not the file.

    Also whether another segment may follow before size: only when this one
    stopped at SEGMENT_BYTES with bytes left. Otherwise it read through
    size, and what it left is at most an unfinished line."""
    buf = bytearray()
    while start + len(buf) < size:
        searched = len(buf)
        part = pread(fd, min(SEGMENT_BYTES, size - start - len(buf)), start + len(buf))
        if not part:
            break
        buf += part
        if len(buf) >= SEGMENT_BYTES and (newline := buf.find(b"\n", max(searched, SEGMENT_BYTES - 1))) >= 0:
            return bytes(buf[: newline + 1]), start + newline + 1 < size
    return bytes(buf if settled else buf[: buf.rfind(b"\n") + 1]), False


def segments_end(fd: int, start: int, size: int, settled: bool) -> int:
    """Where the segments next_segment cuts one after another from start
    end: at size once the file has settled, else just past the last newline
    before size, else at start. Reads back from size only as far as that
    newline."""
    if settled:
        return size
    end = size
    while end > start:
        n = min(SEGMENT_BYTES, end - start)
        if (newline := pread(fd, n, end - n).rfind(b"\n")) >= 0:
            return end - n + newline + 1
        end -= n
    return start


@dataclass(frozen=True, slots=True)
class _Open:
    off: int
    data: bytes
    torn: bool


def records(
    segments: Iterable[tuple[int, bytes]], before: Iterable[tuple[int, bytes]] = ()
) -> Iterator[tuple[int, Record, bool]]:
    """Every line of consecutive segments as (file offset, record, mends),
    in file order. Empty lines are skipped.

    A line lies inside one segment unless a settled tail was archived
    before its writer finished it; the line then goes on at the start of
    the next segment. Its bytes so far and the continuation are parsed as
    one line when that makes a record: a writer that paused for an hour.
    Else the continuation alone, when it is a record: a restarted writer
    that appended a whole record onto torn bytes. Else the line stays the
    one Unparsed its tail was counted as. mends is True for a record that
    replaces that Unparsed.

    before is the line the archive already ends inside, as its segments
    hold it (line_at); its records were projected with those segments,
    so only what the new segments add is yielded."""
    left = None
    for start, chunk in before:
        _, left = _chunk_records(start, chunk, left)
    for start, chunk in segments:
        found, left = _chunk_records(start, chunk, left)
        yield from found


def record_at(line: Iterable[tuple[int, bytes]], offset: int) -> Record:
    """The record the pieces of a line (line_at) make at offset: the last
    one records yields there, because a line a settled tail left open
    yields its Unparsed tail first. Unparsed when it yields none there."""
    found = Unparsed()
    for off, rec, _ in records(line):
        if off == offset:
            found = rec
    return found


def record_bytes(line: Sequence[tuple[int, bytes]], offset: int) -> bytes | None:
    """The bytes of the record records yields at offset from the pieces of
    a line (line_at), without its newline: up to the next offset it yields
    there, else to the line's end. None when it yields none at offset."""
    starts = sorted({off for off, _, _ in records(line)})
    if offset not in starts:
        return None
    end = next((start for start in starts if start > offset), None)
    first, data = line[0][0], b"".join(piece for _, piece in line)
    return data[offset - first : None if end is None else end - first].removesuffix(b"\n")


def _chunk_records(
    start: int, chunk: bytes, left: _Open | None
) -> tuple[list[tuple[int, Record, bool]], _Open | None]:
    found = []
    pos = 0
    if left is not None:
        end = chunk.find(b"\n")
        rest = chunk[: end if end >= 0 else len(chunk)]
        pos = len(rest) + 1
        if not isinstance(whole := parse(left.data + rest), Unparsed):
            if left.torn:
                found.append((left.off, whole, True))
            left = _Open(left.off, left.data + rest, False)
        elif not isinstance(alone := parse(rest), Unparsed) or not left.torn:
            found.append((start, alone, False))
            left = _Open(start, rest, isinstance(alone, Unparsed))
        else:
            left = _Open(left.off, left.data + rest, True)
        if end >= 0:
            left = None
    while pos < len(chunk):
        end = chunk.find(b"\n", pos)
        if end < 0:
            rec = parse(chunk[pos:])
            found.append((start + pos, rec, False))
            left = _Open(start + pos, chunk[pos:], isinstance(rec, Unparsed))
            break
        if end > pos:
            found.append((start + pos, parse(chunk[pos:end]), False))
        pos = end + 1
    return found, left


def line_at(starts: Sequence[int], payload: Callable[[int], bytes], offset: int) -> list[tuple[int, bytes]]:
    """The pieces of the line that holds byte offset, as consecutive
    segments hold it: from the byte after the newline before offset through
    the newline at or after it, else through the last segment's end. starts
    are the segments' first bytes in file order, and payload(i) the bytes of
    segment i, called only for the segments the line touches. Usually one
    piece of one segment; a line a settled tail left open spans more."""
    i = bisect_right(starts, offset) - 1
    if i < 0:
        return []
    chunks = {i: payload(i)}
    first, before = i, offset - starts[i]
    while (begin := chunks[first].rfind(b"\n", 0, before) + 1) == 0 and first > 0:
        first -= 1
        chunks[first] = payload(first)
        before = len(chunks[first])
    pieces = []
    for k in range(first, len(starts)):
        chunk = chunks[k] if k in chunks else payload(k)
        at = begin if k == first else 0
        end = chunk.find(b"\n", max(at, offset - starts[k]))
        if piece := chunk[at : end + 1 if end >= 0 else len(chunk)]:
            pieces.append((starts[k] + at, piece))
        if end >= 0:
            break
    return pieces
