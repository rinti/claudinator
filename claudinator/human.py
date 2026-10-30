import re
from datetime import datetime
from pathlib import Path

from claudinator.record import Millis

LARGEST = 2**62
"""The largest number argv may give. SQLite stores integers in 64 bits, and
a time that far back can still be subtracted from now."""

HIDDEN = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def escape(char: str) -> str:
    n = ord(char)
    return f"\\x{n:02x}" if n < 0xA0 else f"\\u{n:04x}"


def printable(text: str) -> str:
    return HIDDEN.sub(lambda m: escape(m[0]), text)


def parse_when(text: str, now: Millis) -> Millis:
    if m := re.fullmatch(r"(\d+)([dh])", text):
        if (ago := int(m[1]) * {"d": 86_400_000, "h": 3_600_000}[m[2]]) > LARGEST:
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


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def short(path: str | None) -> str:
    if not path:
        return "?"
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def when(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms is not None else "?"


def size(n: int) -> str:
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= scale:
            return f"{n / scale:.3g} {unit}"
    return f"{n} B"
