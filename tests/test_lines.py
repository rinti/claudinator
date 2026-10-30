import os

import pytest

from claudinator.lines import SEGMENT_BYTES, line_at, next_segment, record_at, records, segments_end
from claudinator.record import Other, Turn, Unparsed
from helpers import line, user

PAUSED = user("the paused writer's quokka")
WHOLE = user("a whole wombat")


def shapes(found) -> list[tuple[int, type, bool]]:
    return [(off, type(rec), mends) for off, rec, mends in found]


def test_records_are_the_lines_of_a_segment_in_order_and_skip_empty_lines():
    first, second = line({"type": "mode"}), user("hello")
    chunk = first + b"\n" + second + b"not json\n"
    assert shapes(records([(1000, chunk)])) == [
        (1000, Other, False),
        (1000 + len(first) + 1, Turn, False),
        (1000 + len(first) + 1 + len(second), Unparsed, False),
    ]


@pytest.mark.parametrize(
    ("tail", "then", "expected"),
    [
        (PAUSED[:40], PAUSED[40:], [(0, Unparsed, False), (0, Turn, True)]),
        (PAUSED[:40], WHOLE, [(0, Unparsed, False), (40, Turn, False)]),
        (PAUSED[:40], b"zebracorn torn again\n", [(0, Unparsed, False)]),
        (PAUSED[:-1], WHOLE, [(0, Turn, False), (len(PAUSED) - 1, Turn, False)]),
    ],
    ids=[
        "the paused writer finishes the line",
        "a restarted writer glues a whole record on",
        "a restarted writer glues more torn bytes on",
        "a record that lacked only its newline gets one glued on",
    ],
)
def test_a_line_a_settled_tail_left_open_is_read_across_the_segment_boundary(tail, then, expected):
    assert shapes(records([(0, tail), (len(tail), then)])) == expected
    assert shapes(records([(len(tail), then)], before=[(0, tail)])) == [r for r in expected if r != expected[0]]


def test_the_record_at_an_offset_is_the_line_a_paused_writer_finished():
    assert isinstance(record_at([(0, PAUSED[:40]), (40, PAUSED[40:])], 0), Turn)
    assert isinstance(record_at([(0, PAUSED[:40]), (40, WHOLE)], 0), Unparsed)
    assert isinstance(record_at([(0, PAUSED)], 1), Unparsed)


@pytest.fixture
def file(tmp_path):
    def write(data: bytes) -> int:
        path = tmp_path / "t.jsonl"
        path.write_bytes(data)
        return os.open(path, os.O_RDONLY)

    return write


def test_a_segment_ends_at_the_first_newline_past_segment_bytes_and_says_more_may_follow(file):
    row = b"x" * 99 + b"\n"
    data = row * (SEGMENT_BYTES * 3 // 2 // len(row))
    fd = file(data)
    try:
        chunk, more = next_segment(fd, 0, len(data), settled=False)
        assert (len(chunk) >= SEGMENT_BYTES, len(chunk) % len(row), more) == (True, 0, True)
        assert len(chunk) < SEGMENT_BYTES + len(row)
        assert next_segment(fd, len(chunk), len(data), settled=False) == (data[len(chunk) :], False)
    finally:
        os.close(fd)


@pytest.mark.parametrize(("settled", "expected"), [(False, b"done\n"), (True, b"done\nunfinished")])
def test_an_unfinished_last_line_waits_until_the_file_settles(file, settled, expected):
    fd = file(b"done\nunfinished")
    try:
        assert next_segment(fd, 0, 15, settled) == (expected, False)
    finally:
        os.close(fd)


def test_a_line_longer_than_a_segment_is_one_segment(file):
    long = b"y" * (SEGMENT_BYTES + 10) + b"\n"
    fd = file(long + b"next\n")
    try:
        assert next_segment(fd, 0, len(long) + 5, settled=False) == (long, True)
    finally:
        os.close(fd)


ROW = b"x" * 99 + b"\n"


@pytest.mark.parametrize(
    "data",
    [b"", b"unfinished", b"done\nunfinished", ROW * (SEGMENT_BYTES * 5 // 2 // len(ROW)) + b"unfinished", b"y" * (SEGMENT_BYTES + 10) + b"\nz"],
    ids=["empty", "no newline", "an unfinished last line", "segments and an unfinished line", "a line longer than a segment"],
)
@pytest.mark.parametrize("settled", [False, True])
def test_segments_end_is_where_the_segments_a_sweep_cuts_end(file, data, settled):
    fd = file(data)
    try:
        end, more = 0, True
        while more:
            chunk, more = next_segment(fd, end, len(data), settled)
            end += len(chunk)
        assert segments_end(fd, 0, len(data), settled) == end
    finally:
        os.close(fd)


def segments(*chunks: bytes) -> tuple[list[int], list[bytes]]:
    starts, at = [], 0
    for chunk in chunks:
        starts.append(at)
        at += len(chunk)
    return starts, list(chunks)


@pytest.mark.parametrize(
    ("chunks", "offset", "expected", "read"),
    [
        ((b"a\nbb\nccc\n", b"dd\n"), 2, [(2, b"bb\n")], [0]),
        ((b"a\nbb\nccc\n", b"dd\n"), 3, [(2, b"bb\n")], [0]),
        ((b"a\nbb\n", b"ccc\n"), 5, [(5, b"ccc\n")], [1, 0]),
        ((b"a\nbb", b"b\nccc\n"), 2, [(2, b"bb"), (4, b"b\n")], [0, 1]),
        ((b"a\nbb", b"b\nccc\n"), 4, [(2, b"bb"), (4, b"b\n")], [1, 0]),
        ((b"x\nab", b"cd", b"ef\nz\n"), 5, [(2, b"ab"), (4, b"cd"), (6, b"ef\n")], [1, 0, 2]),
        ((b"a\nb", b"bb"), 4, [(2, b"b"), (3, b"bb")], [1, 0]),
        ((b"a\n",), 100, [], [0]),
        ((), 0, [], []),
    ],
    ids=[
        "a line inside one segment",
        "from inside a line",
        "a line that starts a segment",
        "a settled tail and its continuation",
        "from the continuation",
        "a line across three segments",
        "the line the archive ends inside",
        "past the archived end",
        "no segments",
    ],
)
def test_line_at_reads_the_pieces_of_the_line_that_holds_an_offset(chunks, offset, expected, read):
    starts, payloads = segments(*chunks)
    calls = []

    def payload(i: int) -> bytes:
        calls.append(i)
        return payloads[i]

    assert line_at(starts, payload, offset) == expected
    assert sorted(calls) == sorted(read)
