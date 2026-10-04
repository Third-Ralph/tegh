#!/usr/bin/env python3
"""Check that no private vocabulary is left in the tracked files.

tegh was split out of a larger private repository. This script is the
verifier for the scrub that removed that repository's names: it scans every
tracked text file and reports each line that still carries one.

    python scripts/check_scrub.py            # gating classes decide the exit code
    python scripts/check_scrub.py --strict   # every class gates, report-only ones too

Each hit prints as `path:line: [class] matched text`. The exit code is 1 when
a gating class has a hit and 0 otherwise.

The sensitive literals below are assembled from parts so that this file does
not match its own rules. That keeps the allowlist free of a whole-file skip.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

IDENTITY = "identity"
HOME_PATH = "home-path"
PRIVATE_NAME = "private-name"
ISSUE_REF = "issue-ref"

CLASSES = (IDENTITY, HOME_PATH, PRIVATE_NAME, ISSUE_REF)

# Classes that are counted and printed but do not affect the exit code. Empty
# today: every class gates. A class goes here while what its hits become is
# still undecided, and `--strict` gates it anyway.
REPORT_ONLY: set[str] = set()

# The lowest issue number the bare-issue rule flags. Every private-tracker
# number that was cited in this tree was 126 or above, and tegh's own tracker
# starts at 1, so a bare number below this is an issue in this repository.
# Retire or replace the rule before tegh's own tracker reaches this number.
PRIVATE_ISSUE_FLOOR = 100

# Placeholder user names that may follow /Users/ or /home/. Each one is already
# used in the tree as an obviously fictional account.
HOME_PLACEHOLDERS = ("you", "someone", "x", "broker")

_FIRST = "w" "es"
_SURNAME = "Jackson"
_ORG = "Third" "-" "Ralph"
_TRACKER = "s" "a"


@dataclass(frozen=True)
class Rule:
    """One pattern. `unless_before` is matched against the text of the line up
    to the match (anchored at its end), and suppresses the hit when it matches."""

    cls: str
    name: str
    pattern: re.Pattern[str]
    reason: str
    unless_before: re.Pattern[str] | None = None


def _ci(*parts: str) -> re.Pattern[str]:
    return re.compile("".join(parts), re.IGNORECASE)


def _cs(*parts: str) -> re.Pattern[str]:
    return re.compile("".join(parts))


def _digits_from(floor: int, max_digits: int = 4) -> str:
    """A pattern for a decimal number from `floor` to the largest `max_digits`
    number. `floor` must be a power of ten, which keeps this a digit count."""
    width = len(str(floor))
    if floor != 10 ** (width - 1) or width > max_digits:
        raise ValueError(f"floor must be a power of ten of at most {max_digits} digits: {floor}")
    return rf"[1-9]\d{{{width - 1},{max_digits - 1}}}"


# The rule table. One reason per rule.
RULES: tuple[Rule, ...] = (
    Rule(
        IDENTITY,
        "first-name",
        # Whole token, any case, except the exact phrase naming the copyright holder.
        _cs(r"\b(?!", _FIRST.capitalize(), " ", _SURNAME, r"\b)(?i:", _FIRST, r")\b"),
        "the maintainer's first name as an identity token; only the full copyright name is public",
    ),
    Rule(
        IDENTITY,
        "login",
        _ci(_FIRST, _SURNAME),
        "a local account name, which appears in home paths",
    ),
    Rule(
        IDENTITY,
        "mail-domain",
        _ci("third", "ralph"),
        "the maintainer's private mail domain",
    ),
    Rule(
        HOME_PATH,
        "home-dir",
        _cs(
            r"/(?:Users|home)/(?!(?:",
            "|".join(HOME_PLACEHOLDERS),
            r")(?![A-Za-z0-9_.-]))[A-Za-z0-9_][A-Za-z0-9_.-]*",
        ),
        "a real account's home directory; placeholders are listed in HOME_PLACEHOLDERS",
    ),
    Rule(PRIVATE_NAME, "consumer-repo", _ci("trading", "-agent"), "a private consumer repository"),
    Rule(PRIVATE_NAME, "vendor", _ci("robin", "hood"), "the vendor a private consumer integrates with"),
    Rule(PRIVATE_NAME, "harness-repo", _ci("dev", "-toolbox"), "a private development repository"),
    Rule(PRIVATE_NAME, "private-org", _ci("controlled", "-agents"), "the private organisation"),
    Rule(PRIVATE_NAME, "corpus-repo", _ci("auto", "-agents"), "a private design-corpus repository"),
    Rule(
        PRIVATE_NAME,
        "parent-repo",
        _ci("safe-agents", "-kitchen"),
        "the private parent; `safe-agents` alone is the public package and is allowed",
    ),
    Rule(
        PRIVATE_NAME,
        "org-other-repo",
        _cs(_ORG, r"/(?!tegh(?![A-Za-z0-9_-]))"),
        "any repository in the organisation other than tegh itself",
    ),
    Rule(
        PRIVATE_NAME,
        "private-issue",
        _cs(r"\b", _TRACKER, r"#\d+"),
        "an issue in the private tracker, by its short prefix",
    ),
    Rule(
        PRIVATE_NAME,
        "agent-instructions",
        _cs("CLAUDE", r"\.md"),
        "the private parent's instruction file; tegh has none",
    ),
    Rule(PRIVATE_NAME, "plan-file", _cs("NEXT", "_SESSION"), "the private parent's plan files"),
    Rule(
        PRIVATE_NAME,
        "summaries-dir",
        _cs("session", "-summaries"),
        "the private parent's session history directory",
    ),
    Rule(
        ISSUE_REF,
        "bare-issue",
        # Not an HTML entity (&#123;), not glued to a word (owner/repo#12, page#12),
        # and not the head of a longer hex colour (#12ab34). Three or four digits
        # with no leading zero, which is PRIVATE_ISSUE_FLOOR to 9999.
        _cs(r"(?<![&\w/])#", _digits_from(PRIVATE_ISSUE_FLOOR), r"(?![0-9A-Za-z_])"),
        "a bare number of 100 or more is a private-tracker issue: every one cited here "
        "was 126 or above and tegh's own tracker starts at 1; owner/repo#N is public. "
        "Retire or replace this rule before tegh's own tracker reaches 100",
        # An all-digit hex colour is told apart only by what precedes it; so is
        # a fragment that follows whitespace-free URL text.
        unless_before=_ci(r"(?:\b(?:colou?r|background|fill|stroke)\b\s*[:=]\s*[\"']?|://\S*)$"),
    ),
)


@dataclass(frozen=True)
class Allow:
    """A narrow exemption: this exact substring, on a line of this one file."""

    path: str
    substring: str
    reason: str


# Never a whole-file skip. An entry exempts a hit only when the hit lies inside
# an occurrence of `substring` in `path`.
ALLOWLIST: tuple[Allow, ...] = ()


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    cls: str
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.cls}] {self.text}"


def _allowed(path: str, line: str, start: int, end: int) -> bool:
    for entry in ALLOWLIST:
        if entry.path != path:
            continue
        at = line.find(entry.substring)
        while at != -1:
            if at <= start and end <= at + len(entry.substring):
                return True
            at = line.find(entry.substring, at + 1)
    return False


def scan_line(line: str, path: str = "<text>", lineno: int = 1) -> list[Hit]:
    """Every hit on one line, in rule order."""
    hits: list[Hit] = []
    for rule in RULES:
        for match in rule.pattern.finditer(line):
            if rule.unless_before and rule.unless_before.search(line[: match.start()]):
                continue
            if _allowed(path, line, match.start(), match.end()):
                continue
            hits.append(Hit(path, lineno, rule.cls, rule.name, match.group(0)))
    return hits


def scan_text(text: str, path: str = "<text>") -> list[Hit]:
    hits: list[Hit] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits.extend(scan_line(line, path, lineno))
    return hits


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"scrub check: `git ls-files` failed in {root}: {result.stderr.strip()}")
    return sorted(name for name in result.stdout.split("\0") if name)


def read_text(path: Path) -> str | None:
    """The file's text, or None for a binary file (the one whole-file skip)."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SystemExit(f"scrub check: cannot read {path}: {error}") from error
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_tree(root: Path) -> Iterator[Hit]:
    for name in tracked_files(root):
        path = root / name
        if not path.is_file():
            continue  # deleted in the working tree but still in the index
        text = read_text(path)
        if text is None:
            continue
        yield from scan_text(text, name)


def gating_classes(strict: bool) -> set[str]:
    return set(CLASSES) if strict else set(CLASSES) - REPORT_ONLY


def report(hits: Iterable[Hit], strict: bool = False) -> tuple[int, list[str]]:
    """The exit code and the lines to print."""
    hits = list(hits)
    gating = gating_classes(strict)
    counts = Counter(hit.cls for hit in hits)
    lines = [hit.render() for hit in hits]
    gating_total = sum(counts[cls] for cls in gating)
    if hits:
        lines.append("")
    for cls in CLASSES:
        mode = "gating" if cls in gating else "report-only"
        lines.append(f"{cls}: {counts[cls]} ({mode})")
    if gating_total:
        lines.append(f"scrub check: {gating_total} hits")
        return 1, lines
    lines.append("scrub check: 0 hits")
    return 0, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict", action="store_true", help="make every class gating, including report-only ones"
    )
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)"
    )
    args = parser.parse_args(argv)
    code, lines = report(scan_tree(args.root), strict=args.strict)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
