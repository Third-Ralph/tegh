"""`tegh posture` — the honesty surface, and the rules that keep it honest.

Most of this file tests one property: **a claim cannot be made without a
citation**. That is the item's real difficulty. The rendering is easy and the
vocabulary was ruled in advance, which is exactly the setup where a confident
ladder gets written from the vocabulary rather than from the code — so the rule
is a constructor invariant with a sweep behind it, not a review habit.

The rest tests that posture reports the PRESENT tense: the four live gaps are
named unconditionally, an unreadable thing is `unknown` rather than rounded to
clean, and no line claims a runtime protection that does not exist yet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tegh.harnesses import claude_code, durability_line
from tegh.lock import Harness
from tegh.posture import (
    PostureLine,
    _is_interposed,
    build_report,
    render,
    to_dict,
)
from tegh.store import TeghStore, provision


@pytest.fixture
def store(tmp_path) -> TeghStore:
    return provision(tmp_path / "home")


@pytest.fixture
def project(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _no_lock(project, store):
    return None


def _report(project, store, **kwargs):
    return build_report(
        project, store, Harness.CLAUDE_CODE, lock_reader=_no_lock, **kwargs
    )


# ---------------------------------------------------------------------------
# The one rule
# ---------------------------------------------------------------------------


def test_a_line_cannot_be_built_without_a_source():
    with pytest.raises(ValueError, match="overclaim"):
        PostureLine(claim="everything is fine", holds="yes", source="")


def test_whitespace_is_not_a_source():
    """The obvious way to satisfy a required field without satisfying the rule."""
    with pytest.raises(ValueError, match="overclaim"):
        PostureLine(claim="everything is fine", holds="yes", source="   ")


def test_every_line_in_a_real_report_carries_a_source(project, store):
    report = _report(project, store)

    assert report.all_lines, "a report with no lines would pass this vacuously"
    for line in report.all_lines:
        assert line.source.strip(), f"uncited claim: {line.claim!r}"


def test_every_rendered_line_shows_its_source(project, store):
    """The citation is part of the OUTPUT, not metadata behind a flag: a reader
    who cannot see the basis cannot weigh the claim."""
    text = render(_report(project, store))

    assert text.count("source:") == len(_report(project, store).all_lines)


def test_json_carries_the_source_too(project, store):
    payload = to_dict(_report(project, store))

    for section in ("holds", "gaps", "project", "store"):
        for line in payload[section]:
            assert line["source"].strip()
    assert payload["rung_reason"]["source"].strip()


# Whether a citation still RESOLVES — and whether a cited issue is still open,
# which is the check that catches a gap line rotting in the flattering direction
# — lives in `test_posture_citations.py`. The tests here assert a source
# is present; that file asserts it is true.


# ---------------------------------------------------------------------------
# The present tense
# ---------------------------------------------------------------------------


def test_the_rung_is_pre_1_while_nothing_is_interposed(project, store):
    """`pre-1` rather than `1`, because posture 1 means deterministic GATING and
    nothing routes a call through tegh yet. Calling this posture 1 would be the
    first overclaim in the file.

    The gateway landing did NOT move this, deliberately: a gateway that
    exists but that no harness config points at gates exactly nothing, and a
    posture is about where the boundary IS rather than about what has been built.
    The interposition is what `tegh wrap` adds, and THAT is what may move this line."""
    report = _report(project, store)

    assert report.posture == "pre-1"
    assert report.rung_reason.holds == "partial"


def _gateway(project: Path) -> dict:
    return claude_code.gateway_entry(project, launcher=["/venv/bin/tegh"], home="/h/.tegh")


@pytest.mark.parametrize(
    ("servers", "interposed"),
    [
        pytest.param(lambda project: {"tegh": _gateway(project)}, True, id="as-a-wrap-writes-it"),
        # Read from what the entry runs, as wrap and unwrap read it.
        pytest.param(lambda project: {"broker": _gateway(project)}, True, id="renamed"),
        pytest.param(
            lambda project: {"tegh": {"command": "npx", "args": ["-y", "a-server"]}},
            False,
            id="a-server-only-called-tegh",
        ),
        pytest.param(
            lambda project: {"tegh": _gateway(project.parent / "other")},
            False,
            id="another-projects-gateway",
        ),
        pytest.param(
            lambda project: {"tegh": _gateway(project), "notes": {"command": "notes"}},
            False,
            id="the-gateway-and-a-server-beside-it",
        ),
        pytest.param(lambda project: {}, False, id="no-servers"),
    ],
)
def test_interposed_means_the_one_server_runs_this_projects_gateway(
    tmp_path, project, servers, interposed: bool
):
    home = tmp_path / "harness-home"
    home.mkdir()
    (home / ".claude.json").write_text(
        # In the harness's own style, which tegh proves before it reads a block.
        json.dumps(
            {"projects": {str(project): {"mcpServers": servers(project)}}},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert _is_interposed(project, Harness.CLAUDE_CODE, home) is interposed


@pytest.mark.parametrize(
    "phrase",
    [
        "does not point at it",  # a gateway exists; nothing is interposed
        "tegh can interpose",    # ...and this is what would interpose it
        "built-in",              # the harness's own tools are ungated
        "header_map",            # a header-authenticated remote server cannot be wrapped
        "ptc-gal-reference#104", # MCP stdio children spawn unconfined
    ],
)
def test_the_live_gaps_are_named_unconditionally(project, store, phrase):
    """Naming a gap you are not closing is the honest move; implying it is
    handled is the dishonest one. These four are live TODAY."""
    text = render(_report(project, store)).lower()

    assert phrase.lower() in text


def test_tegh_names_no_builtin_tool_or_hook_api():
    """tegh interposes on MCP servers only — it names no harness built-in tool
    and no pre-tool-call hook API. This is the evidence for the posture line
    saying built-ins are ungated.

    Identifiers only, via the AST. The first version of this evidence was a text
    grep quoted inside the posture line itself, which matched its own prose and
    so contradicted the claim it supported the moment it shipped. Prose about
    hooks — docstrings, comments, this one — is invisible here; only code that
    actually reaches for such an API can fail it.
    """
    import ast

    vocabulary = {
        "PreToolUse", "beforeMCPExecution", "pre_tool_call", "trustedToolPolicy",
        "register_hook", "before_tool_call", "pre_mcp_tool_use",
    }
    offenders: list[str] = []
    for py in sorted((Path(__file__).resolve().parents[1]).rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            name = (
                node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name)
                else node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                else None
            )
            if name in vocabulary:
                offenders.append(f"{py.name}:{node.lineno}: {name}")

    assert not offenders, (
        "tegh reached for a harness built-in/hook API — the posture line claiming "
        "built-ins are ungated is now wrong:\n  " + "\n  ".join(offenders)
    )


def test_no_gap_is_described_as_pending_or_planned(project, store):
    """A gap described in the future tense reads to a user as handled."""
    text = render(_report(project, store)).lower()

    for weasel in ("will be", "planned", "coming soon", "pending implementation"):
        assert weasel not in text, f"future tense in a posture report: {weasel!r}"


def test_same_user_exposure_is_stated_as_a_property_that_does_not_hold(project, store):
    """store.py:25 says `tegh posture` MUST say this. It is the single claim
    most likely to be quietly dropped, because it undercuts the feature.

    Asserted on the line's VERDICT, not on the words: a first cut of this test
    matched "same-user" anywhere in the rendered text, and passed when the claim
    was flipped to "tegh authority is protected" — because the phrase survived
    in the detail underneath. Keyword-matching a report cannot tell a warning
    from a reassurance; the verdict field can.
    """
    report = _report(project, store)

    (line,) = [x for x in report.holds if "same-user attacker" in x.claim]
    assert line.holds == "no"
    assert "not privilege separation" in line.detail


# ---------------------------------------------------------------------------
# Wrap durability — the harness's property, not tegh's
# ---------------------------------------------------------------------------


def test_claude_code_durability_is_unresolved_not_structural():
    """The pre-2026-07-26 summary called this 'structural, unbypassed except
    bypassPermissions'. The vendor docs do not license that: they never say
    which tools the protected-path gate covers, while documenting a shell path
    as ungated. `unknown` is the honest verdict until something is TESTED."""
    line = durability_line(Harness.CLAUDE_CODE)

    assert line.holds == "unknown"
    assert "documentation claim" in line.detail


def test_an_unresearched_harness_gets_no_durability_claim():
    """Inheriting another harness's durability line is precisely the overclaim
    the ladder exists to prevent."""
    line = durability_line("some-future-harness")

    assert line.holds == "unknown"
    assert "no durability claim is made" in line.detail


# ---------------------------------------------------------------------------
# The store audit — 'could not run' is never rounded to 'clean'
# ---------------------------------------------------------------------------


def _fake_audit(returncode: int, stdout: str = "", stderr: str = ""):
    def run(env, db_path):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


#: What the base reports when it audited the registry too: the sqlite arm at the
#: pinned commit. Leaving these out of a payload is the "registry not audited"
#: shape, which the base also spells as explicit nulls.
_REGISTRY_EXAMINED = {"mcp_rows": 2, "mcp_records": 2, "mcp_proposals": 2}


def _violation(rule: str) -> dict:
    return {"rule": rule, "coordinate": "somewhere", "detail": "something"}


def _payload(
    grants: int = 0, *, registry: dict | None = None, violations: tuple[str, ...] = ()
) -> str:
    return json.dumps(
        {
            "backend": "sqlite",
            "location": "tegh.db",
            "clean": not violations,
            "violations": [_violation(rule) for rule in violations],
            "acknowledged": [],
            "skipped_rules": ["GRANT_TAMPER"],
            "examined": {
                "grants": grants,
                "records": 0,
                "proposals": 0,
                "envelopes": 0,
                **(registry or {}),
            },
        }
    )


def _clean_payload(grants: int = 0) -> str:
    """A clean run in the shape the pinned base reports for a tegh home."""
    return _payload(grants, registry=_REGISTRY_EXAMINED)


def test_no_database_reports_unknown_not_clean(project, store):
    report = _report(project, store)

    (line,) = report.store
    assert line.holds == "unknown"
    assert "does not exist" in line.source


def test_audit_that_could_not_run_reports_unknown(project, store):
    """Exit 2 from the audit means nobody looked. Rounding that to clean is the
    failure the audit's own exit-code contract exists to prevent."""
    store.db_path.write_text("")
    report = _report(project, store, audit_runner=_fake_audit(2, stderr="no key"))

    assert report.store[0].holds == "unknown"
    assert "not 'clean'" in report.store[0].detail


def test_skipped_rules_are_reported_as_partial_not_passed(project, store):
    store.db_path.write_text("")
    report = _report(project, store, audit_runner=_fake_audit(0, _clean_payload()))

    skipped = [line for line in report.store if line.holds == "partial"]
    assert skipped, "a clean audit with skipped rules must still say what did not run"
    assert "not a passed rule" in skipped[0].detail


@pytest.mark.parametrize(
    "registry",
    [
        None,  # a base older than the registry auditor: the counts are absent
        {"mcp_rows": None, "mcp_records": None, "mcp_proposals": None},  # a DynamoDB table
    ],
)
def test_a_clean_grants_result_does_not_imply_the_registry_is_audited(
    project, store, registry
):
    """The trap this line exists for: tegh's admissions live in the MCP registry,
    so a clean grants result says nothing about the rows tegh's trust rests on
    unless the run covered them. Not audited is never zero rows and never green."""
    store.db_path.write_text("")
    payload = _payload(grants=0, registry=registry)
    report = _report(project, store, audit_runner=_fake_audit(0, payload))

    line = report.store[0]
    assert "NOT audited" in line.claim
    assert "admitted-tool row" not in line.claim
    assert line.holds == "partial"
    assert "ptc-gal-reference#96" in line.detail


@pytest.mark.parametrize(
    "violations, grant_count, registry_count",
    [
        ((), 0, 0),
        (("MCP_ROW_HMAC_INTACT",), 0, 1),
        (("GRANT_TAMPER",), 1, 0),
        (("GRANT_TAMPER", "MCP_ORPHAN_ROW", "MCP_ROW_HMAC_INTACT"), 1, 2),
    ],
)
def test_registry_findings_are_not_reported_as_grant_findings(
    project, store, violations, grant_count, registry_count
):
    """The base returns grant and registry findings in ONE list. Counting that
    list under a grants label reports a tampered registry row as a grant
    violation, which sends the reader to the wrong rows."""
    store.db_path.write_text("")
    payload = _payload(grants=2, registry=_REGISTRY_EXAMINED, violations=violations)
    report = _report(project, store, audit_runner=_fake_audit(1 if violations else 0, payload))

    line = report.store[0]
    assert f"{grant_count} grant violation(s) over 2 grant(s)" in line.claim
    assert f"{registry_count} registry violation(s) over 2 admitted-tool row(s)" in line.claim
    assert line.holds == ("no" if violations else "yes")
    for rule in violations:
        assert rule in line.detail


def test_the_signature_gap_is_named_while_tegh_names_no_verify_keys(
    project, store, monkeypatch
):
    """The gap line says the registry's admission signatures go unverified because
    tegh names no verify keys for the audit. Both halves are asserted together, so
    naming a verify-keys file in `ceremony_env` turns this red until the line is
    rewritten. A gap that outlives its cause is pessimism; one that is deleted
    while the cause stands is the flattering direction."""
    for name in ("ISSUER_VERIFY_KEYS_FILE", "ISSUER_VERIFY_KEYS_PARAM"):
        monkeypatch.delenv(name, raising=False)
    environment = store.ceremony_env(role="checker", project=project)
    assert "ISSUER_VERIFY_KEYS_FILE" not in environment
    assert "ISSUER_VERIFY_KEYS_PARAM" not in environment

    (line,) = [x for x in _report(project, store).gaps if "admission records" in x.claim]
    assert line.holds == "partial"
    assert "ISSUER_VERIFY_KEYS_FILE" in line.detail


@pytest.mark.parametrize("name", ["ISSUER_VERIFY_KEYS_FILE", "EVALUATOR_VERIFY_KEYS_FILE"])
def test_a_verify_keys_file_named_in_the_shell_reaches_the_audit_alone(
    project, store, monkeypatch, name
):
    """The skipped-rules line tells the reader a rule runs if the calling
    environment names a verify-keys file. `ceremony_env` no longer inherits the
    shell (#5), so the audit carries these two names itself; if it stopped, that
    line would be advice nobody could follow. No ceremony leg gets them."""
    monkeypatch.setenv(name, "/somewhere/verify-keys.json")
    store.db_path.write_text("")
    handed_over: list[dict] = []

    def run(env, db_path):
        handed_over.append(dict(env))
        return _fake_audit(0, _clean_payload())(env, db_path)

    _report(project, store, audit_runner=run)

    (env,) = handed_over
    assert env[name] == "/somewhere/verify-keys.json"
    assert name not in store.ceremony_env(role="checker", project=project)


# ---------------------------------------------------------------------------
# Lock state — three-state verification survives into the report
# ---------------------------------------------------------------------------


def test_unwrapped_project_is_reported_without_consulting_the_store(project, store):
    report = _report(project, store)

    (line,) = [x for x in report.project if "not wrapped" in x.claim]
    assert line.holds == "no"


def test_unknown_signer_is_not_reported_as_tampering(project, store):
    from tegh.signing import UnknownLockSigner

    def reader(project, store):
        raise UnknownLockSigner("key tegh-local-abc is unknown to this home")

    report = build_report(project, store, Harness.CLAUDE_CODE, lock_reader=reader)

    (line,) = [x for x in report.project if "signed by a key" in x.claim]
    assert line.holds == "unknown"
    assert "NOT reported as tampering" in line.detail


def test_failed_verification_is_reported_as_unknown_origin(project, store):
    from tegh.lockfile import LockSignatureInvalid

    def reader(project, store):
        raise LockSignatureInvalid("signature does not match these bytes")

    report = build_report(project, store, Harness.CLAUDE_CODE, lock_reader=reader)

    (line,) = [x for x in report.project if "does NOT verify" in x.claim]
    assert line.holds == "no"


def test_posture_never_claims_the_lock_is_enforced(project, store):
    """A pinned tool is an admitted tool, not a gated one — and the difference
    is the whole of Phase 4."""
    text = render(_report(project, store))

    assert "no call is currently routed through tegh" in text


# ---------------------------------------------------------------------------
# The declared polarity
# ---------------------------------------------------------------------------
#
# The base must never hold a safe-default polarity — it is re-derived per agent and
# baking one in is a latent safety bug. That exclusion leaves an obligation on the
# consumer, and it was found landing on nobody.
#
# The FIRST version of this section declared a constant ACT-SAFE, and it was wrong
# within a day: `tegh wrap --polarity` already existed and defaulted to `abstain`, so
# the report contradicted the manifest it described. These tests exist mostly to keep
# that from recurring — posture READS, and reports three distinct claims rather than
# collapsing them into one verdict.


def _wrap_manifest(store, project, polarity: str) -> None:
    """Write the one field posture reads out of a project's synthesized manifest."""
    path = store.manifest_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"envelope": {"polarity": polarity}}), encoding="utf-8"
    )


@pytest.mark.parametrize("declared", ["abstain", "act"])
def test_the_declared_polarity_is_read_not_asserted(project, store, declared):
    _wrap_manifest(store, project, declared)

    report = _report(project, store)

    assert declared.upper() in report.polarity[0].claim
    assert report.polarity[0].holds == "n/a"


def test_posture_never_contradicts_the_manifest(project, store):
    """The regression test for the bug this section shipped with.

    A constant claim is wrong the moment a user exercises the flag. Whatever the
    manifest declares is what the report must say — including the value the first
    version hardcoded the opposite of.
    """
    for declared, forbidden in (("abstain", "ACT-SAFE"), ("act", "ABSTAIN-SAFE")):
        _wrap_manifest(store, project, declared)
        text = render(_report(project, store))

        assert f"{declared.upper()}-SAFE" in text
        assert forbidden not in text, (
            f"manifest declares {declared!r} and the report says {forbidden} — "
            "a posture line that asserts instead of reading is the bug this test pins"
        )


def test_an_unreadable_manifest_reports_unknown_not_a_guess(project, store):
    """No manifest at all — an unwrapped project. Unknown is a legitimate answer;
    defaulting one here would be the base's own excluded move, one layer up."""
    report = _report(project, store)

    assert "UNKNOWN" in report.polarity[0].claim
    assert report.polarity[0].holds == "unknown"


def test_a_malformed_manifest_reports_unknown(project, store):
    path = store.manifest_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this: [is: not: valid", encoding="utf-8")

    assert "UNKNOWN" in _report(project, store).polarity[0].claim


def test_a_manifest_with_a_bogus_polarity_reports_unknown(project, store):
    """A value outside the closed set reads as unknown rather than being echoed —
    posture must not launder an unvalidated string into a safety claim."""
    _wrap_manifest(store, project, "whatever-i-typed")

    assert "UNKNOWN" in _report(project, store).polarity[0].claim


def test_the_report_says_the_polarity_is_not_enforced(project, store):
    """The load-bearing line. A knob that silently does nothing is a false
    affordance — worse than no knob, and misleading in the direction the ladder
    forbids. It is graded `no` because it is a genuine gap, not a declaration."""
    _wrap_manifest(store, project, "abstain")

    not_enforced = [x for x in _report(project, store).polarity if x.holds == "no"]

    assert len(not_enforced) == 1
    assert "NOT ENFORCED" in not_enforced[0].claim
    # It must cite the code that makes it true, not just assert it.
    assert "facts.py" in not_enforced[0].source
    assert "human_reachable" in not_enforced[0].source


def test_the_two_axes_are_reported_separately(project, store):
    """Holding rather than executing is abstain-safe at the DECISION point; a
    one-command release is act-safe FRICTION. Collapsing them is what produced the
    original error, so the report keeps them as different lines."""
    _wrap_manifest(store, project, "abstain")
    report = _report(project, store)

    friction = [x for x in report.polarity if "friction" in x.claim.lower()]
    assert len(friction) == 1
    assert "ONE command" in friction[0].claim
    assert "different axis" in friction[0].detail


def test_the_polarity_says_it_is_a_consumer_decision(project, store):
    """The line must not read as a property of the floor. An agent that can move
    money should re-derive it and land the other way — if this text ever implies
    the platform chose for everyone, the base exclusion has leaked."""
    _wrap_manifest(store, project, "abstain")
    detail = _report(project, store).polarity[0].detail.lower()

    assert "consumer decision" in detail
    assert "re-derive" in detail
    # And it names the granularity that was ruled, so a reader knows what they can change.
    assert "per-project" in detail


def test_the_polarity_section_is_rendered_unconditionally(project, store):
    """Not tucked into a section that only prints when non-empty, and not behind a
    flag: an unread polarity is a silently-shipped one."""
    text = render(_report(project, store))

    assert "SAFE-DEFAULT POLARITY" in text


def test_json_carries_every_polarity_line(project, store):
    _wrap_manifest(store, project, "act")
    payload = to_dict(_report(project, store))

    assert len(payload["polarity"]) == 3
    for line in payload["polarity"]:
        assert line["source"].strip()
    assert "ACT-SAFE" in payload["polarity"][0]["claim"]
