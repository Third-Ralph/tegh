"""The scrub check's rules, one class at a time.

`scripts/check_scrub.py` is the verifier for the names-and-prose scrub, so a
rule that quietly stops matching would let private vocabulary back in with a
green check. Each case below pins one string a class must catch or one it must
leave alone.

The strings that must hit are assembled from parts. Written whole, they would
be hits in this file, and the check scans its own tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_scrub.py"


def _load():
    # `scripts/` is not a package, so the module is loaded by path. It has to be
    # in sys.modules before it executes: its dataclasses resolve their own
    # annotations through that table.
    spec = importlib.util.spec_from_file_location("check_scrub", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_scrub = _load()

_W = "w" + "es"
_NAME = _W.capitalize()
_HOLDER = f"{_NAME} Jackson"
_ORG = "Third" + "-Ralph"
_SA = "s" + "a"
_USERS = "/Users/"
_HOME = "/home/"

IDENTITY = check_scrub.IDENTITY
HOME_PATH = check_scrub.HOME_PATH
PRIVATE_NAME = check_scrub.PRIVATE_NAME
ISSUE_REF = check_scrub.ISSUE_REF

MUST_HIT = [
    (IDENTITY, f"[ruling: {_W}, 2026-07-25]"),
    (IDENTITY, f"local:{_W}"),
    (IDENTITY, f"{_HOME}{_W}"),
    (IDENTITY, f"{_NAME}'s call"),
    (IDENTITY, f"{_NAME.upper()} decided"),
    (IDENTITY, f"{_NAME} Jacksonville"),
    (IDENTITY, f"{_HOLDER} [ruling: {_W}, 2026-09-25]"),
    (IDENTITY, f"{_USERS}{_W}jackson/Code"),
    (IDENTITY, f"someone@third{'ralph'}.com"),
    (HOME_PATH, f"{_HOME}{_W}"),
    (HOME_PATH, f"{_USERS}alice/Code/widget"),
    (HOME_PATH, f"{_USERS}younger/x"),
    (HOME_PATH, f"{_HOME}x1"),
    (PRIVATE_NAME, "the trading" + "-agent cutover"),
    (PRIVATE_NAME, "proven against Robin" + "hood"),
    (PRIVATE_NAME, "ROBIN" + "HOOD_TOKEN"),
    (PRIVATE_NAME, "a dev" + "-toolbox pattern"),
    (PRIVATE_NAME, "controlled" + "-agents/anything"),
    (PRIVATE_NAME, "see auto" + "-agents"),
    (PRIVATE_NAME, "safe-agents" + "-kitchen"),
    (PRIVATE_NAME, f"{_ORG}/other-repo"),
    (PRIVATE_NAME, f"{_ORG}/tegh-internal"),
    (PRIVATE_NAME, f"the rule ({_SA}#" + "214)"),
    (PRIVATE_NAME, "the bug CLAUDE" + ".md forbids"),
    (PRIVATE_NAME, "NEXT" + "_SESSION-tegh.md"),
    (PRIVATE_NAME, "session" + "-summaries/2026-07-25.md"),
    (ISSUE_REF, "the wart #" + "253 names"),
    (ISSUE_REF, "(#" + "197/#" + "199)"),
    (ISSUE_REF, "#" + "100"),
    (ISSUE_REF, "#" + "126"),
    (ISSUE_REF, "closed by #" + "1234."),
]

MUST_NOT_HIT = [
    (IDENTITY, f"Copyright 2026 {_HOLDER}"),
    (IDENTITY, f'authors = [{{ name = "{_HOLDER}" }}]'),
    (IDENTITY, "the western region"),
    (IDENTITY, "Wesley wrote this"),
    (IDENTITY, "[ruling: maintainer, 2026-07-25]"),
    (HOME_PATH, f"{_USERS}you/x"),
    (HOME_PATH, f"{_USERS}someone/Code/widget"),
    (HOME_PATH, f"{_HOME}x"),
    (HOME_PATH, f"{_HOME}broker"),
    (HOME_PATH, f"{_HOME}.claude.json"),
    (HOME_PATH, "the home/ directory and Users/ group"),
    (PRIVATE_NAME, "safe-agents[mcp]==0.73.0"),
    (PRIVATE_NAME, "wjatx/ptc-gal-reference"),
    (PRIVATE_NAME, f"{_ORG}/tegh"),
    (PRIVATE_NAME, f"github.com/{_ORG}/tegh.git"),
    (PRIVATE_NAME, "usa#1 seller"),
    (PRIVATE_NAME, "a consumer agent"),
    (ISSUE_REF, "wjatx/ptc-gal-reference#104"),
    (ISSUE_REF, "&#123;"),
    (ISSUE_REF, "https://example.com/page#253"),
    (ISSUE_REF, "color: #333"),
    (ISSUE_REF, 'fill="#0000"'),
    (ISSUE_REF, "#12ab34"),
    (ISSUE_REF, "#123456"),
    # Below the floor: an issue in tegh's own tracker, which starts at 1.
    (ISSUE_REF, "#1"),
    (ISSUE_REF, "tracked as #3."),
    (ISSUE_REF, "#42"),
    (ISSUE_REF, "#99"),
    (ISSUE_REF, "#099"),
    (ISSUE_REF, "#12345"),
    (ISSUE_REF, "# 253 lines of comment"),
]


def _classes(text: str) -> set[str]:
    return {hit.cls for hit in check_scrub.scan_line(text)}


@pytest.mark.parametrize(("cls", "text"), MUST_HIT)
def test_class_hits(cls: str, text: str) -> None:
    assert cls in _classes(text), f"[{cls}] should have matched {text!r}"


@pytest.mark.parametrize(("cls", "text"), MUST_NOT_HIT)
def test_class_does_not_hit(cls: str, text: str) -> None:
    assert cls not in _classes(text), f"[{cls}] should not have matched {text!r}"


def test_every_class_has_cases_in_both_directions() -> None:
    """A class with no negative case is a class whose false positives are untested."""
    for cases in (MUST_HIT, MUST_NOT_HIT):
        assert {cls for cls, _ in cases} == set(check_scrub.CLASSES)


def test_every_rule_states_a_reason_and_a_known_class() -> None:
    for rule in check_scrub.RULES:
        assert rule.cls in check_scrub.CLASSES, rule.name
        assert rule.reason.strip(), rule.name


def test_hit_renders_as_path_line_class_text() -> None:
    (hit,) = check_scrub.scan_text("clean\n" + "see #" + "253\n", "docs/a.md")
    assert hit.render() == "docs/a.md:2: [issue-ref] #" + "253"


def test_every_class_gates_by_default() -> None:
    assert check_scrub.REPORT_ONLY == set()
    assert check_scrub.gating_classes(strict=False) == set(check_scrub.CLASSES)


_PRIVATE_ISSUE = "see #" + "253"


@pytest.mark.parametrize(
    ("text", "report_only", "strict", "code", "last_line", "mode"),
    [
        ("nothing here", set(), False, 0, "scrub check: 0 hits", "gating"),
        (_PRIVATE_ISSUE, set(), False, 1, "scrub check: 1 hits", "gating"),
        ("see #1 and #99", set(), False, 0, "scrub check: 0 hits", "gating"),
        (f"local:{_W}", set(), False, 1, "scrub check: 1 hits", "gating"),
        # The report-only mechanism is kept: a class placed in the set is
        # counted and printed, and gates only under --strict.
        (_PRIVATE_ISSUE, {ISSUE_REF}, False, 0, "scrub check: 0 hits", "report-only"),
        (_PRIVATE_ISSUE, {ISSUE_REF}, True, 1, "scrub check: 1 hits", "gating"),
        (f"local:{_W}", {ISSUE_REF}, False, 1, "scrub check: 1 hits", "report-only"),
    ],
)
def test_a_report_only_class_counts_but_gates_only_when_strict(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    report_only: set[str],
    strict: bool,
    code: int,
    last_line: str,
    mode: str,
) -> None:
    monkeypatch.setattr(check_scrub, "REPORT_ONLY", report_only)
    got, lines = check_scrub.report(check_scrub.scan_text(text, "f"), strict=strict)
    assert (got, lines[-1]) == (code, last_line)
    count = 1 if text == _PRIVATE_ISSUE else 0
    assert f"issue-ref: {count} ({mode})" in lines


def test_the_issue_floor_is_stated_in_the_rule_reason() -> None:
    """The floor expires when tegh's own tracker reaches it, so the rule says so."""
    (rule,) = [rule for rule in check_scrub.RULES if rule.cls == ISSUE_REF]
    assert check_scrub.PRIVATE_ISSUE_FLOOR == 100
    assert str(check_scrub.PRIVATE_ISSUE_FLOOR) in rule.reason
    assert "Retire or replace" in rule.reason


def test_allowlist_exempts_one_substring_in_one_file(monkeypatch: pytest.MonkeyPatch) -> None:
    line = f"admitted_by=local:{_W} and again local:{_W}x {_NAME}"
    entry = check_scrub.Allow("a.py", f"local:{_W}", "test entry")
    monkeypatch.setattr(check_scrub, "ALLOWLIST", (entry,))
    assert [hit.text for hit in check_scrub.scan_line(line, "a.py")] == [_NAME]
    assert len(check_scrub.scan_line(line, "b.py")) == 2


def test_the_tree_itself_is_clean() -> None:
    """The scrub's own exit predicate, so a regression fails the ordinary test run."""
    root = _SCRIPT.parents[1]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout; the check scans `git ls-files`")
    gating = check_scrub.gating_classes(strict=False)
    leftover = [hit.render() for hit in check_scrub.scan_tree(root) if hit.cls in gating]
    assert leftover == []
