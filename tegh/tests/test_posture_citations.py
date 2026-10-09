"""Nothing checked a posture citation against what it cites.

`PostureLine` requires a `source`, and a sweep asserts every rendered line
carries one. That machinery guarantees a citation EXISTS. These tests are the
half that was missing: whether it is still TRUE.

The two rot directions are not equally bad. Toward pessimism — posture keeps
reporting a gap that has since been closed — is annoying and self-correcting.
Toward flattery — posture keeps reporting a property that stopped holding, or
stops reporting a gap that is still live — is the one failure mode the whole
surface exists to prevent, and it is silent, because the existing tests check
that lines are *cited* and not that they are *right*.

`citations.py` documents what is checkable and what deliberately is not,
including where the "no bare shell command" rule lands narrower than the
original proposal's wording and why that is faithful to its reason.

## Both branches, not just the one a default report renders

Every check here runs over `_static_lines()`, which assembles the invariant
lines from EVERY routing state: interposed, and the three that are not. A
default report in a tmp project only renders one of the unwrapped states, so a
citation inside the wrapped line — the one that reports the interposition, the
line the whole report hangs on — would otherwise never be checked by anything. A conformance check blind to the flattering branch is
the bug it is here to catch, one level up.

## The network

`test_every_cited_issue_is_still_open` needs GitHub, and it departs on purpose
from the usual convention for tests that need the network. Those default to SKIP
because they need credentials a human must supply; this one needs `gh`, which is either
present and authenticated or is not, so it detects rather than demands. It has
to default to running: the definition of done is that closing a cited issue
turns the suite RED, and a check gated behind an opt-in nobody sets would not do
that. Set `TEGH_CITATIONS_STRICT=1` to make an unavailable `gh` a failure
instead of a skip — "could not run" is not "clean".
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from tegh.cluster import (
    SENSITIVE_MOUNTS,
    PodFacts,
    cluster_lines,
    posture_note,
)
from tegh.harnesses import durability_line
from tegh.lock import Harness
from tegh.posture import (
    PostureLine,
    Routing,
    _invariant_gaps,
    _invariant_holds,
    _polarity_lines,
    _rung_reason,
    build_report,
)
from tegh.store import TeghStore, provision
from tegh.tests.citations import (
    Issue,
    RepoPath,
    CitedSymbol,
    defines,
    parse,
    search_command_prefix,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a `safe_agents/...` citation lives. tegh consumes the platform as the
#: pinned `safe-agents` dependency, so a citation of platform code names a file
#: in the INSTALLED distribution, and resolving it against this repository would
#: fail for every one of them (or, worse, pass against a stale copy if one ever
#: came back). Located by spec rather than `import safe_agents`, because the
#: import-boundary guard in test_lock.py forbids tegh importing the bare base.
_PLATFORM_PREFIX = "safe_agents/"
_PLATFORM_ROOT = Path(
    importlib.util.find_spec("safe_agents").submodule_search_locations[0]
).resolve().parent


#: The public reference implementation, and the release of it this repo pins. A
#: citation of a platform DOC (`broker/MCP-HOST.md`, `docs/GAL.md`) or a platform
#: TEST names a file that is in neither this repository nor the installed
#: distribution, since the wheel ships no docs or tests. It resolves in the RI at
#: exactly the release tag of the pinned version, `v<version>`, which is the tag
#: the RI's release workflow publishes the wheel from.
_RI_REPO = "wjatx/ptc-gal-reference"
_RI_PIN = "v" + re.search(
    r"safe-agents\[mcp\]==([0-9][0-9A-Za-z.]*)",
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
).group(1)
_RI_CACHE: dict[str, Path] = {}


def _unchecked(message: str) -> None:
    """Skip locally, fail under TEGH_CITATIONS_STRICT: 'could not run' is not 'clean'."""
    if os.environ.get("TEGH_CITATIONS_STRICT") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _from_ri(cited: str) -> Path:
    """Fetch one file from the RI at the pin into a local cache, once per session."""
    if cited in _RI_CACHE:
        return _RI_CACHE[cited]
    if shutil.which("gh") is None:
        _unchecked(f"gh is not installed, so {cited!r} could not be read from the RI.")
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["gh", "api", f"repos/{_RI_REPO}/contents/{cited}?ref={_RI_PIN}"],
        capture_output=True,
        text=True,
        check=False,
    )
    cache = Path(tempfile.gettempdir()) / f"tegh-ri-{_RI_PIN[:12]}" / cited
    if completed.returncode != 0:
        if "Not Found" in completed.stderr or "HTTP 404" in completed.stderr:
            _RI_CACHE[cited] = cache.with_name(cache.name + ".absent-upstream")
            return _RI_CACHE[cited]
        _unchecked(f"gh could not read {cited!r} from the RI ({completed.stderr.strip()}).")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(base64.b64decode(json.loads(completed.stdout)["content"]))
    _RI_CACHE[cited] = cache
    return cache


def _resolve(cited: str) -> Path:
    """The file a citation names: here if it is here, then the installed
    platform for `safe_agents/...` code, then the RI at the pin for platform
    docs and tests. A path absent from all three resolves to a nonexistent
    file, so the caller's assertion reports it as stale."""
    local = REPO_ROOT / cited
    if local.exists():
        return local
    if cited.startswith(_PLATFORM_PREFIX):
        installed = _PLATFORM_ROOT / cited
        if installed.exists():
            return installed
    return _from_ri(cited)


#: Both sides of the boundary, because the mount line says different things from
#: each and only one of them is the flattering one.
_POD_HOLDING_NOTHING = PodFacts(
    namespace="safe-agents",
    service_account="safe-agents-agent",
    uid=1000700000,
    capabilities="0000000000000000",
    no_new_privs=True,
    present_mounts=(),
    absent_mounts=tuple(SENSITIVE_MOUNTS),
)
_POD_CARRYING_AUTHORITY = replace(
    _POD_HOLDING_NOTHING,
    service_account="safe-agents-broker",
    present_mounts=("/run/connector-secrets",),
    absent_mounts=tuple(x for x in SENSITIVE_MOUNTS if x != "/run/connector-secrets"),
)


class _NoManifest:
    """A store whose manifest is never there: the polarity lines read one field
    from it, and their citations are the same whatever it says."""

    def manifest_path(self, project) -> Path:
        return Path(project) / "no-manifest.yaml"


_NO_MANIFEST = _NoManifest()

#: One of each state `_routing` can report, so every branch's citations are read.
_ROUTINGS = (
    Routing("interposed"),
    Routing("beside-others", ("notes (user scope)",)),
    Routing("no-gateway"),
    Routing("unestablished"),
)


def _static_lines() -> list[PostureLine]:
    """Every line built from literals, across EVERY routing state.

    Deduplicated, because the states share every gap but the first one:
    without this a stale citation is reported once per state, and a failure
    message that says everything twice is one people learn to skim.

    The polarity lines are here too. Two of the three are literals that cite
    platform code, and a report in a tmp project is the only other thing that
    builds them.
    """
    lines = [*_invariant_holds()]
    for routing in _ROUTINGS:
        lines.append(_rung_reason(routing))
        lines.extend(_invariant_gaps(routing))
    lines.extend(_polarity_lines(Path("/nonexistent/project"), _NO_MANIFEST))
    lines.append(durability_line(Harness.CLAUDE_CODE))
    lines.append(durability_line("some-future-harness"))

    # The cluster claims (Phase 6.1) go through the same checks, and the
    # order matters: this checker landed FIRST so the cluster lines would be
    # written against it. A batch of new claims followed by a checker retrofitted
    # to accept them is how a checker gets shaped to pass what was already
    # written.
    for facts in (_POD_HOLDING_NOTHING, _POD_CARRYING_AUTHORITY):
        lines.extend(cluster_lines(facts))
        for interposed in (False, True):
            lines.append(posture_note(facts, interposed=interposed))
    lines.append(posture_note(None, interposed=False))

    seen: set[tuple[str, str]] = set()
    unique = []
    for line in lines:
        key = (line.claim, line.source)
        if key not in seen:
            seen.add(key)
            unique.append(line)
    return unique


@pytest.fixture
def store(tmp_path) -> TeghStore:
    return provision(tmp_path / "home")


def _rendered_lines(tmp_path, store, *, wrapped: bool, monkeypatch):
    """A real assembled report, so runtime-built lines are covered too."""
    monkeypatch.setattr(
        "tegh.posture._routing",
        lambda *a, **k: Routing("interposed" if wrapped else "no-gateway"),
    )
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    report = build_report(
        project, store, Harness.CLAUDE_CODE, lock_reader=lambda p, s: None
    )
    return report.all_lines


def _citations(lines, kind):
    for line in lines:
        for citation in parse(line.source):
            if isinstance(citation, kind):
                yield line, citation


# ---------------------------------------------------------------------------
# Tier 2 — the citation resolves to something that exists
# ---------------------------------------------------------------------------


def test_every_cited_repo_path_exists():
    """A citation that does not resolve is worse than none — it looks checkable.

    Proves the cited FILE exists, not that it says what the claim says. The
    second is what a human reviewer is for, and a test pretending to do it would
    be its own overclaim.
    """
    checked = []
    for line, citation in _citations(_static_lines(), RepoPath):
        resolved = _resolve(citation.path)
        assert resolved.exists(), (
            f"posture line {line.claim!r} cites {citation.path!r}, which does "
            f"not exist at {resolved} — the citation is stale"
        )
        checked.append(citation.path)

    assert len(checked) >= 10, f"expected many cited paths, matched {checked}"


def test_every_cited_line_range_ends_inside_its_file():
    """The WEAKEST tier, and labelled as such.

    It catches deletion and truncation. It does not catch drift: the lines can
    renumber onto unrelated code and still resolve. Catching that needs content
    hashing of the cited range, which would turn every honest edit to `store.py`
    into a posture failure — "integrity must indict tampering, never
    evolution" applied to a citation checker.
    """
    checked = []
    for line, citation in _citations(_static_lines(), RepoPath):
        if citation.last_line is None:
            continue
        resolved = _resolve(citation.path)
        length = len(resolved.read_text().splitlines())
        assert length >= citation.last_line, (
            f"posture line {line.claim!r} cites {citation.path}:"
            f"{citation.last_line}, but that file is only {length} lines — the "
            "cited range has been truncated away"
        )
        checked.append(f"{citation.path}:{citation.last_line}")

    assert len(checked) >= 4, f"expected several cited ranges, matched {checked}"


def test_every_cited_symbol_exists():
    """A source naming a function, class or test asserts that symbol carries the
    evidence. If it was renamed or deleted, the claim is left standing on nothing.

    Platform symbols (`safe_agents/...::name`) resolve the way platform paths
    do, in the installed package, so a pin move that renames one turns this red.
    """
    checked = []
    for line, citation in _citations(_static_lines(), CitedSymbol):
        resolved = _resolve(citation.path)
        assert resolved.exists(), (
            f"posture line {line.claim!r} cites file {citation.path!r}, "
            f"which does not exist"
        )
        assert defines(resolved, citation.name), (
            f"posture line {line.claim!r} cites "
            f"{citation.path}::{citation.name}, which that file does not "
            "define — the evidence for this claim has been renamed or removed"
        )
        checked.append(f"{citation.path}::{citation.name}")

    assert any(name.startswith("tegh/tests/") for name in checked), checked
    platform = [name for name in checked if name.startswith(_PLATFORM_PREFIX)]
    assert len(platform) >= 2, f"expected platform symbol citations, matched {checked}"


def test_a_symbol_citation_parses_for_any_function_or_class():
    """Teeth for the parser half: the form is read whatever the name is, and a
    name the file does not define is reported as undefined."""
    (cited,) = parse("tegh/tests/citations.py::defines (the AST lookup)")
    assert cited == CitedSymbol("tegh/tests/citations.py", "defines")
    assert defines(REPO_ROOT / cited.path, "defines")
    assert defines(REPO_ROOT / cited.path, "CitedSymbol")
    assert not defines(REPO_ROOT / cited.path, "no_such_symbol")


# ---------------------------------------------------------------------------
# The command rule
# ---------------------------------------------------------------------------


def test_no_source_is_a_repo_searching_command(tmp_path, store, monkeypatch):
    """The instance that made this checker worth building.

    A gap line cited `grep -rniE 'bash|builtin|PreToolUse|hook' ...`, and the
    citation's own text contained the pattern — so re-running it returned hits
    and contradicted the claim it was evidence for. Self-falsifying, and worse
    than an uncited claim because it looks checkable.

    Clause-leading only: a source may MENTION grep in prose, and a check firing
    on the word rather than the position would reproduce the original bug inside
    the checker.
    """
    lines = [
        *_static_lines(),
        *_rendered_lines(tmp_path, store, wrapped=False, monkeypatch=monkeypatch),
    ]
    for line in lines:
        offender = search_command_prefix(line.source)
        assert offender is None, (
            f"posture line {line.claim!r} cites a repo-searching command "
            f"({offender!r}). A command stored beside the claim it evidences can "
            "match its own text — make it a test instead"
        )


def test_the_checker_catches_a_self_matching_grep():
    """Teeth. A rule with no demonstrated failure is a rule nobody has run."""
    assert search_command_prefix("grep -rn 'hook' safe_agents/") == "grep -rn 'hook'"
    assert search_command_prefix("docs/GAL.md; git grep foo") == "git grep foo"
    # ...and the prose case the check must NOT fire on.
    assert search_command_prefix("the first cut of this line cited a grep") is None


# ---------------------------------------------------------------------------
# Tier 1 — the highest-value check: a cited gap that has been fixed
# ---------------------------------------------------------------------------


def _gh_issue_state(number: int, repo: str | None = None) -> dict:
    repo_args = ["-R", repo] if repo else []
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["gh", "issue", "view", str(number), *repo_args, "--json", "state,title"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "gh failed")
    return json.loads(completed.stdout)


def test_every_cited_issue_is_still_open():
    """A posture line citing an issue asserts that issue is STILL UNFIXED.

    So a CLOSED issue makes the line stale by construction — no judgment, `gh`
    settles it. This is the cheapest check here and the most valuable: it catches
    exactly "we fixed the thing and forgot the report", which is the flattering
    direction the whole surface exists to prevent.

    Note what it does NOT check: that the line's PROSE still describes the
    issue. An issue that stays open while its scope narrows leaves a line that
    resolves and misleads, and that is a review cadence's job, not a test's.
    """
    cited = list(_citations(_static_lines(), Issue))
    assert cited, "expected at least one issue-backed gap line"

    strict = os.environ.get("TEGH_CITATIONS_STRICT") == "1"
    if shutil.which("gh") is None:
        message = (
            f"gh is not installed — {len(cited)} issue-backed posture citation(s) "
            "went unchecked. 'Could not run' is not 'clean'."
        )
        if strict:
            pytest.fail(message)
        pytest.skip(message)

    stale = []
    for line, citation in cited:
        try:
            issue = _gh_issue_state(citation.number, citation.repo)
        except OSError as exc:
            message = (
                f"gh could not read {citation.repo or ''}#{citation.number} ({exc}) — "
                f"{len(cited)} issue-backed citation(s) went unchecked. "
                "'Could not run' is not 'clean'."
            )
            if strict:
                pytest.fail(message)
            pytest.skip(message)
        if issue["state"] != "OPEN":
            stale.append(
                f"  {citation.repo or ''}#{citation.number} is {issue['state']} ({issue['title']})\n"
                f"    cited by: {line.claim!r}\n"
                f"    source:   {line.source}"
            )

    assert not stale, (
        "posture reports a gap whose issue has been CLOSED — the claim is stale "
        "in the flattering direction, which is the one direction that matters:\n"
        + "\n".join(stale)
    )
