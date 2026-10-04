"""What a pod can observe about its own containment (Phase 6.1).

`posture.py` answers "what holds in THIS configuration" for a laptop. On a
cluster the vocabulary changes — ServiceAccount, SCC, RBAC, NetworkPolicy — and
so does the honest answer, because the posture-1 caveat that same-user process
separation is not privilege separation is exactly the thing a cluster lifts.

## The rule this module is built around

**Observe, do not read the manifest.** A `readOnly: true` field is a statement of
intent by whoever applied it; `os.getuid()` is a fact about this process. Every
line below comes from `/proc`, from the filesystem this pod actually has, or
from the token the kubelet actually projected. Nothing here parses a YAML file
to decide what is true, because a posture report generated from the manifests
would agree with the manifests by construction and would be worthless on a
cluster where they were applied wrong, partially, or not at all.

`60-networkpolicy.yaml` states the same rule from the other side: "a
NetworkPolicy in a YAML file is not evidence."

## What this module deliberately cannot say

Posture makes no network calls — `posture.py` is explicit that a posture command
which silently dialled out would be a surprising thing to run, and being inside a
cluster does not change that. But the two properties that make a pod's
containment *interesting* are refusals, and a refusal has to be ATTEMPTED:

* that egress is constrained to the gateway
* that RBAC refuses this pod the broker's Secret

So both are reported `unknown` here, pointing at `cluster-agent.sh`, which
attempts them for real from inside this same pod and names the mechanism that
refused each one. Reporting them as `yes` on the strength of the manifests is
the precise overclaim this arm's audience is best equipped to catch, and
reporting them as `no` would be the understatement that costs the same
credibility.

That produces an honest and slightly awkward result worth stating plainly: **this
module cannot certify posture 2 by itself.** It observes the local half — a
distinct workload identity, an SCC-constrained process, and the absence of every
credential mount — and it names the drill for the half that must be attempted.

## The posture, and why containment alone does not move it

`docs/posture-ladder.md` puts the agent inside a sandbox and the gateway outside
it at posture 2. A pod in this arm satisfies the topology. It does NOT follow that
any pod is at posture 2, because the ladder is about where the boundary sits for
*tegh's control*, and a pod running a synthetic HTTP client has no harness
config, no interposition, and therefore nothing gated. Containment upgrades a
configuration that is already gating; on its own it is a boundary around nothing.
`posture_note` says which half is present rather than rounding either way.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tegh.posture import PostureLine

#: Where the kubelet projects a pod's ServiceAccount credentials. Relative so the
#: whole surface can be exercised against a fixture tree — a module that could
#: only be tested on a live cluster would be tested when someone remembered.
SA_DIR = "var/run/secrets/kubernetes.io/serviceaccount"

#: The mounts that carry authority in this arm, and what each one would hand a
#: pod that had it. Verified against the manifests rather than assumed:
#: `50-deployment-broker.yaml:105-108` mounts the first four into the broker, and
#: `41-job-ratify.yaml:72` mounts the issuer key into the ratify leg.
#:
#: Their ABSENCE is the claim. `52-job-agent.yaml` declares no volume at all, so
#: these paths do not exist in an agent pod — a kernel-level fact, not a
#: permission the pod is politely declining to use.
SENSITIVE_MOUNTS = {
    "/run/connector-secrets": "connector credentials",
    "/run/issuer-projected": "the issuer signing key",
    "/var/lib/tegh": "the tegh store",
    "/var/lib/tegh-grants": "the grant key space",
    "/var/lib/tegh-audit": "the audit tape",
}

#: All-zero effective capabilities, as `restricted-v2` leaves them. Compared as a
#: string against `/proc/self/status` rather than parsed into an int, because the
#: field is a fixed-width hex mask and "is it exactly none" is the only question
#: being asked of it.
NO_CAPABILITIES = "0000000000000000"


@dataclass(frozen=True)
class PodFacts:
    """What this process can see about its own pod, with nothing inferred."""

    namespace: str
    #: From the projected token's payload, UNVERIFIED — see `_token_service_account`.
    service_account: str | None
    uid: int
    #: `CapEff` from /proc/self/status, or None where /proc is not readable.
    capabilities: str | None
    #: `NoNewPrivs` — 1 under an SCC that forbids privilege escalation.
    no_new_privs: bool | None
    present_mounts: tuple[str, ...]
    absent_mounts: tuple[str, ...]


def _token_service_account(token: str) -> str | None:
    """The SA name from a projected token's payload, WITHOUT verifying it.

    Deliberately unverified, and the line built from it says so. Verifying would
    mean asking the API server, which posture does not do; and the value is still
    worth reporting, because it is what the kubelet handed this pod. The drill's
    SelfSubjectReview is what turns it into the API server's own answer
    (`cluster-agent.sh` step 3), and that is what the line points at.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    k8s = claims.get("kubernetes.io", {})
    if isinstance(k8s, dict):
        account = k8s.get("serviceaccount", {})
        if isinstance(account, dict) and account.get("name"):
            return str(account["name"])
    # The older, flatter claim shape.
    subject = claims.get("sub", "")
    if isinstance(subject, str) and subject.startswith("system:serviceaccount:"):
        return subject.rsplit(":", 1)[-1]
    return None


def _proc_status_field(root: Path, field: str) -> str | None:
    try:
        text = (root / "proc/self/status").read_text()
    except OSError:
        return None
    match = re.search(rf"^{field}:\s*(\S+)$", text, re.MULTILINE)
    return match.group(1) if match else None


def observe(root: Path | None = None) -> PodFacts | None:
    """Read this pod's own facts, or None when this is not a pod.

    The namespace file is the detector rather than any environment variable: an
    env var is something a caller can set, and a posture surface that could be
    told it was in a cluster would be reporting a claim rather than an
    observation.
    """
    root = root or Path("/")
    namespace_file = root / SA_DIR / "namespace"
    try:
        namespace = namespace_file.read_text().strip()
    except OSError:
        return None
    if not namespace:
        return None

    try:
        token = (root / SA_DIR / "token").read_text().strip()
    except OSError:
        token = ""

    present, absent = [], []
    for path in SENSITIVE_MOUNTS:
        target = root / path.lstrip("/")
        (present if target.exists() else absent).append(path)

    no_new_privs = _proc_status_field(root, "NoNewPrivs")
    return PodFacts(
        namespace=namespace,
        service_account=_token_service_account(token) if token else None,
        uid=os.getuid(),
        capabilities=_proc_status_field(root, "CapEff"),
        no_new_privs=None if no_new_privs is None else no_new_privs == "1",
        present_mounts=tuple(present),
        absent_mounts=tuple(absent),
    )


def _identity_line(facts: PodFacts) -> PostureLine:
    who = facts.service_account or "<not readable from the projected token>"
    return PostureLine(
        claim=f"This workload runs as ServiceAccount {who!r} in namespace "
        f"{facts.namespace!r} — a distinct cluster principal, not a second "
        "process under one user",
        holds="yes" if facts.service_account else "unknown",
        # Absolute, because this is a RUNTIME citation: a reader should be able
        # to cat it in the pod, and a leading-slash-less path renders like a
        # repo-relative one, which points at a file that does not exist.
        source=f"/{SA_DIR}/token (payload, UNVERIFIED); "
        "safe_agents/arms/openshift/10-serviceaccounts.yaml",
        detail="this is the posture-1 caveat lifting: on a laptop tegh and the agent "
        "are the same OS user, and here they are different principals the "
        "platform distinguishes. NOT verified against the API server — posture "
        "makes no network calls, so this is the identity the kubelet handed this "
        "pod rather than the identity the API server would attest. "
        "`safe_agents/arms/openshift/cluster-agent.sh` obtains that attested "
        "answer with a SelfSubjectReview, and is what confirms this line",
    )


def _scc_line(facts: PodFacts) -> PostureLine:
    dropped = facts.capabilities == NO_CAPABILITIES
    holds = "yes" if dropped and facts.uid != 0 else "no"
    if facts.capabilities is None:
        holds = "unknown"
    return PostureLine(
        claim=f"This process is SCC-constrained: uid {facts.uid}, effective "
        f"capabilities {facts.capabilities or '<unreadable>'}",
        holds=holds,
        source="/proc/self/status (CapEff, NoNewPrivs); os.getuid()",
        detail="an arbitrary non-root uid and an all-zero capability mask are what "
        "OpenShift's default `restricted-v2` produces, and both are read from this "
        "process rather than from a manifest. NB the SCC's NAME is a field on the "
        "Pod object and is not readable from inside without API access, so this "
        "line reports the EFFECTS and does not name the policy that caused them. "
        "It also says nothing about other workloads in the namespace — the image "
        "BuildConfigs run under OpenShift's `builder` account on the `privileged` "
        "SCC, which is a real exception to any namespace-wide reading of this",
    )


def _mount_line(facts: PodFacts) -> PostureLine:
    if facts.present_mounts:
        carried = ", ".join(
            f"{path} ({SENSITIVE_MOUNTS[path]})" for path in facts.present_mounts
        )
        return PostureLine(
            claim=f"This pod CARRIES authority: {carried}",
            holds="no",
            source="the pod's own filesystem; "
            "safe_agents/arms/openshift/50-deployment-broker.yaml",
            detail="reported as a property that does not hold because it is stated "
            "from the agent's side of the boundary: a pod holding these is the "
            "broker, and compromising it reaches everything they carry. The "
            "boundary protects the credentials FROM the agent, and does nothing "
            "for a compromised broker",
        )
    return PostureLine(
        claim="No credential, key, store or audit tape is mounted into this pod",
        holds="yes",
        source="the pod's own filesystem; "
        "safe_agents/arms/openshift/52-job-agent.yaml",
        detail="absent rather than permission-denied: "
        + ", ".join(facts.absent_mounts)
        + " do not exist here, because no volume declares them. That is posture 2's "
        "second property — credentials are across a real boundary, so a "
        "compromised agent cannot read them because it is not in the same place "
        "as them, not because it was asked not to. What it does NOT cover is a "
        "pod this one could AUTHOR with those mounts; RBAC is what refuses that, "
        "and it is the next line",
    )


def _attempted_elsewhere_lines() -> list[PostureLine]:
    """The two properties posture is structurally unable to settle.

    Both are refusals, and a refusal must be attempted. `unknown` here is not a
    hedge — it is the difference between a report and a manifest summary.
    """
    return [
        PostureLine(
            claim="Whether egress is constrained to the gateway is UNKNOWN here",
            holds="unknown",
            source="safe_agents/arms/openshift/60-networkpolicy.yaml; "
            "safe_agents/arms/openshift/cluster-agent.sh",
            detail="posture makes no network calls, so it cannot attempt the "
            "connection whose failure is the evidence. The drill does: it resolves "
            "an external name (DNS is allowed on purpose, so a failure to connect "
            "cannot be confused with a failure to resolve) and shows the "
            "connection dropped. NetworkPolicy is a platform boundary we "
            "ORCHESTRATE — OVN and the kernel enforce it, no safe_agents code is "
            "in the path — and it constrains where this pod can reach, never what "
            "it does inside itself",
        ),
        PostureLine(
            claim="Whether RBAC refuses this pod the broker's Secret is UNKNOWN here",
            holds="unknown",
            source="safe_agents/arms/openshift/10-serviceaccounts.yaml; "
            "safe_agents/arms/openshift/cluster-agent.sh",
            detail="same reason: the 403 has to be asked for. The drill asks twice, "
            "because RBAC governs API ACCESS and not kubelet volume mounts — it "
            "requests the Secret directly, and separately tries to create a pod "
            "that would MOUNT it, since refusing only the first would leave "
            "'the agent cannot read the Secret' true and useless",
        ),
    ]


def posture_note(facts: PodFacts | None, *, interposed: bool) -> PostureLine:
    """Which half of posture 2 is present, without rounding either way.

    Containment does not by itself move a configuration up the ladder: the postures
    describe where the boundary sits for TEGH'S CONTROL, and a pod with no
    harness config gates nothing however well contained it is. Nor does
    interposition alone reach posture 2. Saying which half is missing is more use to
    a reader than a number that averages them.
    """
    if facts is None:
        return PostureLine(
            claim="No cluster containment is observed — this is not running in a pod",
            holds="unknown",
            source=f"/{SA_DIR}/namespace does not exist",
            detail="the posture-1 same-user caveat therefore stands in full. Nothing "
            "is claimed about a cluster this process cannot see",
        )
    if interposed:
        return PostureLine(
            claim="This configuration is gating AND contained — posture 2 by the "
            "ladder's topology, while the POSTURE above still reads 1",
            holds="partial",
            source="docs/posture-ladder.md; "
            "safe_agents/arms/openshift/cluster-agent.sh",
            detail="the disagreement is deliberate, and is the honest way round. "
            "Posture 2 is 'the agent inside a sandbox, the gateway outside it, egress "
            "to the gateway only', and this topology is that. But posture makes no "
            "network calls, so it did not ATTEMPT either of the refusals that make "
            "the containment real — and a posture is a claim about where a boundary "
            "is, which is not something to assert off a manifest. The drill "
            "attempts both; a reader who wants posture 2 asserted rather than "
            "half-observed should run it and read that instead of this",
        )
    return PostureLine(
        claim="The CONTAINMENT half of posture 2 is present and the GATING half is "
        "not: this workload is a contained principal, and no call is routed "
        "through tegh",
        holds="no",
        source="docs/posture-ladder.md; the harness config's mcpServers block",
        detail="a boundary around something that gates nothing. This is the "
        "expected state for a synthetic client driving the broker's HTTP surface "
        "directly, which is what this arm's drill runs — it exercises the "
        "topology without a wrapped harness. Posture 2 needs both halves, and "
        "reporting this pod as posture 2 on the strength of its containment would be "
        "the flattering half of the same error as calling a wrapped laptop posture 2",
    )


def cluster_lines(facts: PodFacts | None) -> list[PostureLine]:
    """The CLUSTER section, empty off-cluster.

    Empty rather than a row of "not applicable" lines: a laptop report should not
    grow six cluster claims that all say nothing, and `posture_note` already carries
    the one statement an off-cluster reader needs.
    """
    if facts is None:
        return []
    return [
        _identity_line(facts),
        _scc_line(facts),
        _mount_line(facts),
        *_attempted_elsewhere_lines(),
    ]
