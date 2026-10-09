"""Parsing a posture `source` into citations something can actually check.

A `PostureLine` cannot be built without a `source` — that is a constructor
invariant, and it guarantees a citation EXISTS. It does not guarantee the
citation is still true. The claims are hand-written prose, the code they describe
moves, and nothing connected the two. The dangerous direction is the flattering
one: a line keeps reporting a property that stopped holding, or stops reporting a
gap that is still live, and every test stays green because the tests checked that
lines were *cited*, not that they were *right*.

This module does the tractable half. It reads a `source` string and pulls out the
three citation kinds a machine can settle:

* `Issue` — `#1`. The claim is *that the issue is still unfixed*, so a CLOSED
  issue makes the line stale by construction. No judgment required, and it
  catches exactly "we fixed the thing and forgot the report".
* `CitedSymbol` — `path.py::name`, for any function or class, a test included.
  Assertable: the file exists and defines it. This is the citation to prefer for
  code, because it follows the symbol when the file is edited above it. A
  `safe_agents/...` path is checked in the installed platform package, the same
  place a plain path citation of platform code is.
* `RepoPath` — `path.py` or `path.py:12-19`. The path resolves, and a cited range
  ends inside the file.

Everything else is prose and is deliberately left alone. Whether "admission is
two-key" is semantically true of `store.py:3-11` is not derivable by a test, and
pretending otherwise would be its own overclaim.

That half is **#3, and it currently has no owner** — which is worth stating
plainly here rather than gesturing at a review cadence. An earlier draft of this
docstring pointed it at two existing review routines; neither covers it. One
audits the standing instructions for contributors and this is a product surface,
the other audits the backlog and never opens these claims. An assurance that reads as
coverage is the failure this module was written to end, so it must not be
reproduced in its own fix. Assume nothing re-reads these claims until #3 says what does.

## What is NOT checked, and why

**Line-range drift.** A range that resolves proves the file is long enough, not
that those lines still say what the claim says — they can renumber onto unrelated
code and still "resolve". Catching that needs content hashing of the cited range,
which turns every legitimate edit to `store.py` into a posture failure. That is
"integrity must indict tampering, never evolution" applied here: a check
that cries stale on honest change teaches people to stop changing the code. So
the range check is the weakest tier and is labelled as such rather than counted
as verification. This has happened: five ranges in `posture.py` were found
pointing at unrelated text while every check here passed. Ranges are now kept
only for a docstring paragraph, which has no symbol to name, and everything
that has a symbol is cited as `path.py::name`.

**What a symbol says.** A symbol citation that resolves proves the name is still
defined in that file. It does not prove the body still does what the claim
says, and a parenthesis after it (`::_make_pip (human_reachable hardcoded
True)`) is prose nothing reads.

**Absolute paths.** Runtime citations — a project's `tegh.lock.sig`, a store home
— name where something is on the machine the report ran on. They are not repo
claims and re-resolving them against the source tree is a category error.

## The one command rule, and where it lands differently from the proposal

The original proposal was "no `source` may be a bare shell command", from a live instance: a
gap line cited `grep -rniE 'bash|builtin|PreToolUse|hook' ...` and the citation's
own text contained the pattern, so re-running it verbatim returned hits and
contradicted the claim it was evidence for. Self-falsifying, and worse than an
uncited claim because it looks checkable.

The hazard in that instance is specific — it is a command that SEARCHES THE REPO,
which is the class of command whose output a stored citation can pollute. So that
is the class `SEARCH_COMMANDS` forbids. A command that PRODUCES the reported data
is a different animal: `_audit_lines` cites the argv it actually ran, the line
reports that command's output, and re-running it reproduces the line. It cannot
match itself because it does not read the repo, and it cannot rot unrun because
the module runs it at report time. Forbidding it would mean deleting honest
provenance and replacing it with a test reference that described a different
thing. The rule enforced here is therefore narrower than the proposal's wording and
faithful to its reason.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

#: Commands whose output a citation stored beside the claim can pollute. A source
#: clause may not begin with one of these — the self-falsifying grep is
#: the archetype. Commands that produce the reported data are fine; see the
#: module docstring for why that distinction is the load-bearing one.
SEARCH_COMMANDS = frozenset(
    {"grep", "rg", "ag", "ack", "find", "ast-grep", "sg", "sed", "awk", "git"}
)

#: Extensions that make a token a repo path rather than prose. Narrow on purpose:
#: a citation is expected to name a file we can open, and widening this to "any
#: dotted token with a slash" starts matching sentences.
_PATH_SUFFIXES = r"\.(py|md|ya?ml|toml|json|sh|ts)"

_ISSUE = re.compile(r"^(?:([\w.-]+/[\w.-]+))?#(\d+)$")
_TRAILING_RANGE = re.compile(r":(\d+)(?:-(\d+))?$")


@dataclass(frozen=True)
class Issue:
    """`#1` or `owner/repo#96` — an assertion that the issue is still unfixed.

    `repo` is None for an issue in this repository. Platform gaps are tracked in
    the reference implementation's public tracker, so a posture line about the
    platform cites `wjatx/ptc-gal-reference#N` and is checked there.
    """

    number: int
    repo: str | None = None


@dataclass(frozen=True)
class CitedSymbol:
    """`path.py::name` — an assertion that file defines a function or class `name`.

    A test is the common case (`test_x.py::test_name` says that test carries the
    evidence), and any other function or class is cited the same way in place
    of a line range.
    """

    path: str
    name: str


#: The earlier name, from when only tests were cited this way.
CitedTest = CitedSymbol


@dataclass(frozen=True)
class RepoPath:
    """`path.py`, or `path.py:12-19`.

    `last_line` is the END of a cited range (or a single cited line), and is
    None when the citation names no range. It supports the weakest tier of
    check: the file is at least that long.
    """

    path: str
    last_line: int | None = None


Citation = Issue | CitedSymbol | RepoPath


def parse(source: str) -> list[Citation]:
    """Pull every checkable citation out of one `source` string.

    Sources are mixed prose and citation by design — `"docs/GAL.md section 8;
    safe_agents/broker/ceremony_identity.py"` is a good citation and is not
    machine-shaped. So this extracts what it recognises and ignores the rest
    rather than imposing a grammar: a parser that rejected prose would push
    authors toward citations that satisfy the parser instead of the reader.
    """
    found: list[Citation] = []
    for raw in re.split(r"[;,\s]+", source):
        token = raw.strip().strip("()").rstrip(".")
        if not token:
            continue

        issue = _ISSUE.match(token)
        if issue:
            found.append(Issue(int(issue.group(2)), issue.group(1)))
            continue

        # Absolute paths are runtime citations, never repo claims.
        if token.startswith("/"):
            continue

        if "::" in token:
            path, _, name = token.partition("::")
            if re.search(_PATH_SUFFIXES, path):
                found.append(CitedSymbol(path, name))
            continue

        if "/" not in token:
            continue

        # A directory citation — `docs/references/harnesses/`.
        if token.endswith("/"):
            found.append(RepoPath(token))
            continue

        if not re.search(_PATH_SUFFIXES, token):
            continue

        span = _TRAILING_RANGE.search(token)
        if span:
            start, end = span.group(1), span.group(2)
            found.append(
                RepoPath(token[: span.start()], int(end) if end else int(start))
            )
        else:
            found.append(RepoPath(token))
    return found


def search_command_prefix(source: str) -> str | None:
    """The forbidden search command a source clause starts with, if any.

    Clause-leading only. A source is allowed to MENTION grep in prose — the
    posture module's own comments discuss the self-falsifying grep at length —
    and a check that fired on the word rather than on the position would
    reproduce the original bug in the checker.
    """
    for clause in source.split(";"):
        head = clause.strip().split()
        if head and head[0].lower() in SEARCH_COMMANDS:
            return " ".join(head[:3])
    return None


def defines(path: Path, name: str) -> bool:
    """True when `path` defines a function or class called `name`.

    AST rather than text search, for the reason the cited built-ins test already
    documents: a text match would find the name inside the citation quoting it.
    """
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == name
        for node in ast.walk(tree)
    )
