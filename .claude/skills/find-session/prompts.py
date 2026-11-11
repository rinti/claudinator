#!/usr/bin/env python3
"""Print the lines of the user's own prompts that match PATTERN, for each
session listed on stdin by `claudinator search` or `claudinator sessions`.
Each matching prompt prints as its time and length, then each matching line
with its line number in the prompt."""

import re
import subprocess
import sys

SESSION = re.compile(r"^([0-9a-f]{8})\s")
HEADING = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d)  (you|claude)$")
WIDTH = 400


def prompts(shown: list[str]) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    mine = False
    for line in shown[2:]:
        if heading := HEADING.match(line):
            mine = heading[2] == "you"
            if mine:
                found.append((heading[1], []))
        elif mine:
            found[-1][1].append(line.removeprefix("  "))
    return [(when, _trimmed(lines)) for when, lines in found]


def _trimmed(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _clip(line: str) -> str:
    line = line.strip()
    return line if len(line) <= WIDTH else line[:WIDTH] + "…"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: claudinator search WORDS | prompts.py PATTERN", file=sys.stderr)
        return 2
    pattern = re.compile(sys.argv[1], re.IGNORECASE)
    ids = dict.fromkeys(m[1] for line in sys.stdin if (m := SESSION.match(line)))
    for sid in ids:
        shown = subprocess.run(["claudinator", "show", sid], capture_output=True, text=True)
        if shown.returncode != 0:
            print(shown.stderr, end="", file=sys.stderr)
            return 1
        lines = shown.stdout.splitlines()
        if not any(HEADING.match(line) for line in lines):
            print(f"prompts.py: no speaker headings in `claudinator show {sid}`; its format changed", file=sys.stderr)
            return 1
        out = []
        for when, text in prompts(lines):
            hits = [f"    {n}: {_clip(line)}" for n, line in enumerate(text, 1) if line.strip() and pattern.search(line)]
            if hits:
                out += [f"  {when}  ({len(text)} line{'' if len(text) == 1 else 's'})", *hits]
        if out:
            print(lines[0], *out, "", sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
