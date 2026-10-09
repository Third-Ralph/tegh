"""Cluster containment, as a pod can actually observe it (Phase 6.1).

Everything here runs against a FIXTURE TREE rather than a live cluster, and that
is the point of `observe(root=...)`: a module testable only on OpenShift would be
tested when someone remembered, which for this arm means never. The arms other
than EC2 have thin automated coverage, so the cluster legs have to be scripted
rather than manual.

What a fixture tree cannot prove is that the paths are the RIGHT ones. That is
settled against the manifests (`50-deployment-broker.yaml`,
`41-job-ratify.yaml`) and re-settled by the drill, which fails loudly in-pod
if any of them exists where it should not.
"""

from __future__ import annotations

import base64
import json

import pytest

from tegh.cluster import (
    NO_CAPABILITIES,
    SA_DIR,
    SENSITIVE_MOUNTS,
    cluster_lines,
    observe,
    posture_note,
)


def _token(service_account: str = "safe-agents-agent") -> str:
    payload = {
        "sub": f"system:serviceaccount:safe-agents:{service_account}",
        "kubernetes.io": {"serviceaccount": {"name": service_account}},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


@pytest.fixture
def pod(tmp_path):
    """A fixture tree shaped like the agent pod: identity, no authority mounts."""

    def build(
        *,
        namespace: str = "safe-agents",
        token: str | None = None,
        cap_eff: str = NO_CAPABILITIES,
        mounts: tuple[str, ...] = (),
    ):
        sa = tmp_path / SA_DIR
        sa.mkdir(parents=True, exist_ok=True)
        (sa / "namespace").write_text(namespace)
        (sa / "token").write_text(token if token is not None else _token())
        status = tmp_path / "proc/self"
        status.mkdir(parents=True, exist_ok=True)
        (status / "status").write_text(f"Name:\tpython\nCapEff:\t{cap_eff}\nNoNewPrivs:\t1\n")
        for mount in mounts:
            target = tmp_path / mount.lstrip("/")
            target.mkdir(parents=True, exist_ok=True)
        return tmp_path

    return build


# ---------------------------------------------------------------------------
# Observation, not assertion
# ---------------------------------------------------------------------------


def test_not_a_pod_observes_nothing(tmp_path):
    """A laptop must not grow cluster claims. The detector is the namespace file
    the kubelet projects — never an env var, because a posture surface that could
    be TOLD it was in a cluster would report a claim rather than an observation."""
    assert observe(root=tmp_path) is None


def test_an_empty_namespace_file_is_not_a_cluster(pod):
    root = pod(namespace="")

    assert observe(root=root) is None


def test_the_agent_pod_is_seen_as_a_distinct_principal_holding_nothing(pod):
    facts = observe(root=pod())

    assert facts is not None
    assert facts.namespace == "safe-agents"
    assert facts.service_account == "safe-agents-agent"
    assert facts.capabilities == NO_CAPABILITIES
    assert facts.no_new_privs is True
    assert facts.present_mounts == ()
    assert set(facts.absent_mounts) == set(SENSITIVE_MOUNTS)


def test_a_broker_shaped_pod_is_seen_to_carry_authority(pod):
    """The same code, run where the credentials ARE, must say so — a module that
    only ever reported 'nothing is mounted' would be a constant."""
    facts = observe(root=pod(mounts=("/run/connector-secrets", "/var/lib/broker")))

    assert set(facts.present_mounts) == {"/run/connector-secrets", "/var/lib/broker"}
    (line,) = [x for x in cluster_lines(facts) if "CARRIES authority" in x.claim]
    assert line.holds == "no"
    assert "does nothing for a compromised broker" in line.detail


@pytest.mark.parametrize(
    "mounts",
    [
        ("/var/lib/broker", "/var/lib/broker-audit"),
        ("/var/lib/broker",),
        ("/var/lib/broker-grants",),
        ("/var/lib/broker-audit",),
        ("/run/issuer-projected",),
        ("/run/issuer-private",),
        ("/run/connector-secrets",),
    ],
    ids=lambda mounts: "+".join(mounts),
)
def test_a_pod_holding_any_authority_mount_is_never_reported_as_holding_none(pod, mounts):
    lines = cluster_lines(observe(root=pod(mounts=mounts)))

    (line,) = [x for x in lines if "mounted into this pod" in x.claim or "CARRIES" in x.claim]
    assert line.holds == "no", f"{mounts} mounted, yet the report says: {line.claim}"
    for mount in mounts:
        assert f"{mount} (" in line.claim, f"{mount} is mounted and the report does not name it"


def test_an_unreadable_token_leaves_the_identity_unknown_not_assumed(pod):
    facts = observe(root=pod(token="not-a-jwt"))

    assert facts.service_account is None
    (line,) = [x for x in cluster_lines(facts) if "ServiceAccount" in x.claim]
    assert line.holds == "unknown"


# ---------------------------------------------------------------------------
# The SCC line reports effects, and never names a policy it cannot read
# ---------------------------------------------------------------------------


def test_zero_capabilities_and_a_non_root_uid_hold(pod):
    (line,) = [x for x in cluster_lines(observe(root=pod())) if "SCC-constrained" in x.claim]

    assert line.holds == "yes"


def test_running_as_root_does_not_hold(pod, monkeypatch):
    monkeypatch.setattr("os.getuid", lambda: 0)
    (line,) = [x for x in cluster_lines(observe(root=pod())) if "SCC-constrained" in x.claim]

    assert line.holds == "no"


def test_capabilities_held_does_not_hold(pod):
    facts = observe(root=pod(cap_eff="00000000a80425fb"))
    (line,) = [x for x in cluster_lines(facts) if "SCC-constrained" in x.claim]

    assert line.holds == "no"


def test_the_scc_line_does_not_name_the_policy_as_observed(pod):
    """`restricted-v2` is a field on the Pod object and is unreadable from inside
    without API access. Naming it as if observed is the overclaim this audience
    is best equipped to catch, so the line reports effects and says so."""
    (line,) = [x for x in cluster_lines(observe(root=pod())) if "SCC-constrained" in x.claim]

    assert "not readable from inside" in line.detail
    assert "privileged" in line.detail, "the builder-SA exception must be named"


# ---------------------------------------------------------------------------
# The refusals posture is structurally unable to settle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subject", ["egress", "RBAC"])
def test_a_refusal_is_never_reported_as_holding(pod, subject):
    """The load-bearing honesty property of this module.

    Egress denial and the Secret refusal are the two claims a manifest would
    happily assert and posture cannot: both are refusals, a refusal has to be
    ATTEMPTED, and posture makes no network calls. Reporting either as `yes` on
    the strength of the YAML is precisely the overclaim, and it is the one a
    future edit is most likely to introduce, because both are TRUE — they are
    just not true *here*.
    """
    (line,) = [
        x for x in cluster_lines(observe(root=pod())) if subject in x.claim
    ]

    assert line.holds == "unknown"
    assert "UNKNOWN here" in line.claim
    assert "cluster-agent.sh" in line.source, "must name what does attempt it"


# ---------------------------------------------------------------------------
# The posture — containment alone is a boundary around nothing
# ---------------------------------------------------------------------------


def test_containment_without_gating_is_not_rung_2(pod):
    line = posture_note(observe(root=pod()), interposed=False)

    assert line.holds == "no"
    assert "CONTAINMENT half" in line.claim and "GATING half is" in line.claim


def test_both_halves_is_partial_not_yes(pod):
    """Even wrapped and contained, the two refusals are unattempted here.

    And the line must OWN the fact that it disagrees with the POSTURE printed above
    it: the ladder's topology for posture 2 is satisfied, the posture still reads 1,
    and a reader who spots that deserves the reason in the same place rather
    than a report that quietly reads two ways.
    """
    line = posture_note(observe(root=pod()), interposed=True)

    assert line.holds == "partial"
    assert "while the POSTURE above still reads 1" in line.claim
    assert "did not ATTEMPT" in line.detail


def test_off_cluster_the_posture_note_claims_nothing_about_a_cluster(tmp_path):
    line = posture_note(None, interposed=True)

    assert line.holds == "unknown"
    assert "not running in a pod" in line.claim


def test_off_cluster_a_report_grows_no_cluster_section(tmp_path, monkeypatch):
    """The Mac product must be unchanged by this module existing."""
    from pathlib import Path

    from tegh.lock import Harness
    from tegh.posture import build_report, render
    from tegh.store import provision

    monkeypatch.setattr("tegh.cluster.observe", lambda *a, **k: None)
    project = tmp_path / "proj"
    project.mkdir()
    report = build_report(
        Path(project),
        provision(tmp_path / "home"),
        Harness.CLAUDE_CODE,
        lock_reader=lambda p, s: None,
    )

    assert report.cluster == ()
    assert "CLUSTER CONTAINMENT" not in render(report)
