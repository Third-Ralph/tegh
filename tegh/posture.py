"""`tegh posture` — what actually holds in THIS configuration, and what does not.

Three modules already deferred to this one by name before it existed
(`store.py:38-44`, `signing.py:47-49`, `cli.py:231`), each having correctly declined
to make a security claim on the grounds that posture would make it. That debt is
what this module pays.

## The one rule

**A claim without a citation is an overclaim.** Not a review guideline — a
constructor argument: `PostureLine` cannot be built without a `source`, and a
conformance test asserts every line in a rendered report carries one. The
temptation this defends against is specific and strong: the posture vocabulary was
ruled in advance, so it is easy to write a confident ladder from the vocabulary
rather than from the code, and produce a document that reads like an audit and is
actually a summary of intentions.

`docs/posture-ladder.md` is the referent for the vocabulary. Sources are code
paths (`file:line`) for what tegh does, and `docs/references/harnesses/` for
vendor behaviour.

## The second rule

**Report the present tense.** Posture describes what the code does today, not
what the roadmap says it will do. A gap named plainly is the honest move and the
cheapest possible argument for funding its fix; a gap described as "pending"
reads to a user as "handled".

`_invariant_gaps` returns five lines. The first is interposition, and it is the
one line there that is read per project: `_routing` reads the three `mcpServers`
blocks tegh can write for the project and the line reports one of four states
(the gateway is the only server in them, the gateway sits beside other servers,
no entry runs the gateway, or tegh could not establish which). The second is the
harness's built-in tools, which are ungated in every state; whether tegh's
`PostToolUse` hook observes them after they run is also read per project. The
other three are static: a REMOTE server authenticating by HTTP header cannot be
wrapped (`header_map` exists in the base and tegh does not emit it, while the
stdio half relocates); MCP-stdio children spawn unconfined
(wjatx/ptc-gal-reference#104); and the registry audit verifies admission-record
signatures only when the calling environment names a verify-keys file.

The interposition line is the one to watch. When the gateway was first built and
no command could yet point a harness at it, that line was rewritten and its
verdict stayed `no`. That was the correct outcome and an easy one to get wrong in
the flattering direction: building the component that would enforce was not the
same as enforcing, and a report that celebrated the build would have told a user
they were protected by something no call was routed through. The verdict moved
only once `tegh wrap` could interpose the gateway, and it moves per project.

## What posture deliberately does not do

It does not grade, score, or rate. There is no "posture: 7/10" — a number
invites improving the number. It reports properties and names a posture.

It also does not run the drift check: `tegh diff` does that, it needs to reach
the servers, and a posture command that silently made network calls would be a
surprising thing to run.

## In a pod

`cluster.py` adds a CLUSTER CONTAINMENT section when this process is running
inside one, in that platform's own vocabulary — ServiceAccount, SCC, RBAC,
NetworkPolicy. It observes rather than reading the manifests, and the no-network
rule above bites there hardest: egress and RBAC are REFUSALS, a refusal has to be
attempted, so both are reported `unknown` and point at the drill that attempts
them. Containment does not raise the posture on its own; a contained pod that gates
nothing is a boundary around nothing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

import yaml

from tegh.launch import inherited_env, python_module_argv
from tegh.lock import Harness
from tegh.lockfile import LoadedLock, LockSignatureInvalid, lock_paths
from tegh.signing import UnknownLockSigner
from tegh.store import TeghStore, TeghStoreError

#: How posture gets at a project's lock: the caller's single verified read path,
#: injected. See `_project_lines` for why this is not implemented here.
LockReader = Callable[[Path, TeghStore], Optional[LoadedLock]]

#: How much of a claim holds. `partial` and `unknown` exist so a line never has
#: to be rounded to yes or no — rounding is where overclaims come from, and
#: "unknown" is a legitimate and useful thing for a posture report to say.
Holds = Literal["yes", "no", "partial", "unknown", "n/a"]

#: The posture this configuration sits on (docs/posture-ladder.md). `pre-1` is not
#: a fourth posture: it is posture 1 with its enforcement half absent, which is where
#: tegh sits until something actually routes a call through it, and calling it
#: "posture 1" would be the first overclaim in the file. NB the gateway existing
#: is not that thing — the interposition is what `tegh wrap` adds.
Posture = Literal["pre-1", "1", "2", "3"]


@dataclass(frozen=True)
class PostureLine:
    """One claim, one verdict, one citation.

    `source` has no default on purpose. A line whose citation is optional is a
    line that will eventually ship without one.
    """

    claim: str
    holds: Holds
    source: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError(
                f"posture line {self.claim!r} has no source — a claim without a "
                "citation is an overclaim; weaken the claim or cite it"
            )


#: Where a project's declared polarity lives, and what sets it.
_POLARITY_SOURCE = (
    "envelope.polarity in this project's synthesized manifest, set by "
    "`tegh wrap --polarity`"
)


def _declared_polarity(project: Path, store: TeghStore) -> str | None:
    """This project's declared polarity, READ from its manifest. None if unreadable.

    Read rather than asserted, for the same reason `_interposition_line` reads the
    harness config: a posture report must not state what it can check. The first
    version of this section declared a constant, and it was wrong within a day —
    `tegh wrap --polarity` already existed and defaulted to the OTHER value, so the
    report contradicted the manifest it was describing.

    Every failure returns None rather than a guess. An unwrapped project, a manifest
    this version cannot parse, and a manifest with no polarity are all "unknown", and
    the ladder is explicit that unknown is a legitimate and useful thing for a posture
    report to say.
    """
    path = store.manifest_path(project)
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(loaded, dict):
        return None
    envelope = loaded.get("envelope")
    if not isinstance(envelope, dict):
        return None
    polarity = envelope.get("polarity")
    return polarity if polarity in ("abstain", "act") else None


def _polarity_lines(project: Path, store: TeghStore) -> list[PostureLine]:
    """tegh's safe-default polarity — what is declared, and what that is worth.

    The base correctly refuses to hold a polarity: abstain-is-safe vs
    positive-safe-action is re-derived per agent, and baking one into the floor is a
    latent safety bug. That exclusion creates an obligation downstream, and for a
    laptop coding agent it lands here — the user will not read the friction doctrine
    and will not know they were supposed to derive one.

    THREE lines, because collapsing them is what produced the first version's error:

    1. **What is declared** — read from the manifest, per project.
    2. **That it is NOT ENFORCED.** `Facts` carries no polarity field, so the PDP
       cannot see one; `_approval_or_deny` dispatches on `human_reachable`, which the
       PIP hardcodes to True. Setting the flag changes no decision today. Saying this
       out loud is the whole point — a knob that silently does nothing is a false
       affordance, and it misleads in exactly the direction the ladder forbids.
    3. **The friction that IS observable** — a held call is released by one command.
       That is act-safe *friction*, it is real, and it is NOT what the flag controls.

    Lines 1 and 3 are different axes: holding rather than executing is abstain-safe at
    the decision point, while a one-command release is act-safe at the friction level.
    Both are true of tegh at once. The first version of this section reported the
    second as though it were the first.

    Its own section, printed unconditionally: a polarity listed among the "what holds"
    rows is one a reader skims past, which is the silent shipping this prevents.
    """
    declared = _declared_polarity(project, store)
    if declared is None:
        first = PostureLine(
            claim="Safe-default polarity: UNKNOWN — no declared polarity could be "
            "read for this project",
            holds="unknown",
            source=_POLARITY_SOURCE,
            detail="the project is not wrapped, or its manifest cannot be read. "
            "Unknown is reported rather than guessed: a polarity is a safety "
            "decision, and defaulting one here would be the base's own excluded move "
            "committed one layer up",
        )
    else:
        first = PostureLine(
            claim=f"Safe-default polarity DECLARED: {declared.upper()}"
            + (
                "-SAFE — refusing is treated as the safe failure"
                if declared == "abstain"
                else "-SAFE — a blocked call is treated as the costlier outcome"
            ),
            holds="n/a",
            source=_POLARITY_SOURCE,
            detail="THIS IS A CONSUMER DECISION, not a property of the floor: the "
            "base never holds a polarity, and an agent that can move money or send "
            "mail should re-derive it. Change it by re-running `tegh wrap "
            "--polarity`; it is per-project, which is the granularity tegh offers "
            "[ruling: maintainer, 2026-07-29]",
        )
    return [
        first,
        PostureLine(
            claim="...and it is NOT ENFORCED at the decision point — setting it "
            "changes no decision today",
            holds="no",
            source="safe_agents/broker/pdp/facts.py (no polarity field); "
            "safe_agents/broker/pdp/engine.py::_approval_or_deny; "
            "safe_agents/broker/prototype/broker_server.py::_make_pip "
            "(human_reachable hardcoded True)",
            detail="the declared value is validated and stored and nothing reads it: "
            "the PDP's Facts carry no polarity, and the seam that would dispatch on "
            "it branches on whether a human is reachable — which this PIP hardcodes "
            "to True, so the branch that polarity would govern is never taken. "
            "Reported as a gap rather than omitted, because a knob that silently "
            "does nothing is worse than no knob. Tracked as wjatx/ptc-gal-reference#124",
        ),
        PostureLine(
            claim="The friction that IS observable: a held call is released by ONE "
            "command, not by a ceremony",
            holds="yes",
            source="safe_agents/broker/approval/release_cli.py; "
            "tegh/cli.py (`tegh approve`)",
            detail="act-safe FRICTION, and a different axis from the declared "
            "polarity above — holding rather than executing is abstain-safe at the "
            "decision point, while a one-command release is act-safe at the friction "
            "level. Both are true at once; `--polarity` controls neither",
        ),
    ]


@dataclass(frozen=True)
class PostureReport:
    posture: Posture
    rung_reason: PostureLine
    #: NEVER empty in a real report — `render` prints this section unconditionally,
    #: and a polarity a reader does not see is one that shipped unstated. It is a
    #: tuple rather than one line because what is declared, whether it is enforced,
    #: and what friction is observable are three different claims (see
    #: `_polarity_lines`), and the first version of this collapsed them and was wrong.
    polarity: tuple[PostureLine, ...] = ()
    holds: tuple[PostureLine, ...] = ()
    gaps: tuple[PostureLine, ...] = ()
    project: tuple[PostureLine, ...] = ()
    store: tuple[PostureLine, ...] = ()
    #: Empty off-cluster. See `cluster.py` — these are observations of the pod
    #: this process is in, never a reading of the manifests that made it.
    cluster: tuple[PostureLine, ...] = ()

    @property
    def all_lines(self) -> tuple[PostureLine, ...]:
        return (
            self.rung_reason,
            *self.polarity,
            *self.holds,
            *self.gaps,
            *self.project,
            *self.store,
            *self.cluster,
        )


# ---------------------------------------------------------------------------
# The invariant claims — true of any tegh configuration at this version
# ---------------------------------------------------------------------------


def _invariant_holds() -> list[PostureLine]:
    """What a plain tegh install genuinely gets, with nothing rounded up."""
    return [
        PostureLine(
            claim="Admission is two-key: a tool is callable only if a namespace "
            "declaration and a separately-written activation row agree",
            holds="yes",
            source="broker/MCP-HOST.md (M1-M13); tegh/store.py:3-11",
            detail="tegh holds key #1 as a synthesized manifest and key #2 as a "
            "sqlite registry row, both under the tegh home",
        ),
        PostureLine(
            claim="Neither admission key lives in the project tree the agent can write",
            holds="yes",
            source="tegh/store.py:13-18",
            detail="the harness's protected-path list is hard-coded vendor-side and "
            "tegh cannot extend it, so authority is kept out of reach instead",
        ),
        PostureLine(
            claim="A same-user attacker can read and write tegh's authority anyway",
            holds="no",
            source="tegh/store.py:38-44",
            detail="same-user process separation is not privilege separation; this "
            "defeats an ordinary injected-agent edit, not a determined adversary "
            "already running as this user. Posture 2 is what makes it a boundary",
        ),
        PostureLine(
            claim="The admission ceremony enforces maker != checker",
            holds="partial",
            source="docs/GAL.md section 8; safe_agents/broker/ceremony_identity.py",
            detail="two identities from a closed catalog, both derived from this one "
            "OS user — the ceremony's structure is real, the separation of the two "
            "humans it models is not present on a single-player laptop",
        ),
        PostureLine(
            claim="Admission decisions are recorded in a signed, append-only ledger",
            holds="yes",
            source="broker/MCP-HOST.md; tegh/store.py:46-54",
            detail="ledger records are DSSE-signed by this tegh home's local issuer key",
        ),
        PostureLine(
            claim="A wrapped stdio server's credential is MOVED out of the harness "
            "config, not copied",
            holds="partial",
            source="tegh/store.py; tegh/interpose.py",
            detail="relocation: the value goes to this project's 0600 secrets.json and is "
            "delivered at spawn via connector_auth.env_map; the wrap backup keeps a "
            "REFERENCE, not the value, so one copy survives rather than two. PARTIAL "
            "because that copy sits in your tegh home under the same OS user as the "
            "wrapped agent — a boundary against an injected agent reading your config, "
            "not against a same-user adversary, which is posture 2",
        ),
    ]


#: What `_routing` found in the `mcpServers` blocks tegh can write for a project.
#: Three of the four are "not interposed", kept apart because they are different
#: facts with different remedies, and reporting one as another would assert a
#: cause the read did not establish.
Interposition = Literal["interposed", "beside-others", "no-gateway", "unestablished"]


@dataclass(frozen=True)
class Routing:
    """One reading of a project's writable `mcpServers` blocks.

    `beside` names every entry that is not this project's gateway, as
    `name (scope)`, and is filled only for `beside-others`.
    """

    state: Interposition
    beside: tuple[str, ...] = ()

    @property
    def interposed(self) -> bool:
        return self.state == "interposed"


#: The sources of MCP servers a wrap does not reach, said the same way wherever
#: a line claims the gateway is alone.
_UNWRITABLE_SOURCES = (
    "Those three blocks are the ones tegh can write. Plugin-provided servers and "
    "claude.ai connectors are loaded from elsewhere and tegh cannot rewrite "
    "them, so a call to one of those goes around the broker (the next line says "
    "whether it is recorded); a managed-mcp.json, where one exists, takes exclusive "
    "control and the gateway is then not loaded at all"
)

_CONFIG_SOURCE = (
    "the harness config's own mcpServers blocks (local, project and user scope), "
    "read at report time"
)


def _interposition_line(routing: "Routing | bool") -> PostureLine:
    """Whether calls are actually routed through tegh, for THIS project.

    Read from the harness config rather than asserted, because this is the line
    the whole report hangs on: while it is false every other line describes
    evidence quality rather than enforcement. It is the one line here whose
    answer changes per project, so a wrapped project and an unwrapped one on
    the same machine sit on different postures.

    A bare bool is accepted for the two states a caller can name without a
    read: True is `interposed`, False is `no-gateway`.
    """
    if isinstance(routing, bool):
        routing = Routing("interposed" if routing else "no-gateway")

    if routing.state == "interposed":
        return PostureLine(
            claim="Calls to the gateway's tools are routed through tegh: the gateway "
            "is the only MCP server in the three config blocks tegh can write for "
            "this project (local, project, user)",
            holds="yes",
            source="tegh/interpose.py; "
            f"tegh/harnesses/claude_code.py::unwritable_sources; {_CONFIG_SOURCE}",
            detail="the broker's MCP mouth is interposed, so an unadmitted tool is "
            f"refused by the broker and recorded. {_UNWRITABLE_SOURCES}. This gates "
            "the gateway's MCP tools only: the agent's built-in shell, file and "
            "web tools do not go through the gateway and nothing decides them; at "
            "most they are observed after they ran, which is the next line",
        )
    if routing.state == "beside-others":
        count = len(routing.beside)
        return PostureLine(
            claim="MCP calls can go around tegh: this project's harness config runs "
            f"the gateway, and also holds {count} other MCP server "
            f"{'entry' if count == 1 else 'entries'} that the harness loads beside it",
            holds="no",
            source=f"tegh/harnesses/claude_code.py::config_sites; {_CONFIG_SOURCE}",
            detail=f"beside the gateway: {', '.join(routing.beside)}. The harness "
            "loads servers from all three scopes side by side, so a call to a tool "
            "the gateway serves is decided by the broker and a call to one of these "
            "is decided by nothing. A wrap leaves the gateway as the only entry in "
            "the three blocks; the read does not say how these came to be there. "
            "Unwrap and wrap again to bring them behind the gateway, or remove them",
        )
    if routing.state == "unestablished":
        return PostureLine(
            claim="Whether calls are routed through tegh could not be established "
            "from this project's harness config, so the report treats them as not "
            "routed",
            holds="no",
            source=f"tegh/posture.py::_routing; {_CONFIG_SOURCE}",
            detail="one of these holds and this report does not say which: a config "
            "file could not be read or parsed, tegh does not know where this harness "
            "keeps its MCP servers, or an entry runs this project's gateway but not "
            "as the single local-scope entry a wrap writes. An unreadable config is "
            "never taken as evidence that enforcement is in place",
        )
    return PostureLine(
        claim="Nothing is enforced at runtime: a gateway exists, but this "
        "project's harness config does not point at it, so no call is "
        "currently routed through tegh",
        holds="no",
        source=f"safe_agents/broker/gateway/surface.py; {_CONFIG_SOURCE}",
        detail="the broker presents as an MCP server and tegh can interpose "
        "it, so both halves exist. All three blocks were read and no entry in "
        "them runs this project's gateway; the read does not say whether the "
        "project was never wrapped, was unwrapped, or had the entry removed "
        "some other way. Run `tegh wrap` to route calls through "
        "it. Until then the lock records what was admitted and nothing gates a "
        "call, which makes every other line below a statement about evidence "
        "quality rather than about enforcement",
    )


def _routing(project: Path, harness: Harness, home: Path | None = None) -> Routing:
    """Read the `mcpServers` blocks the harness loads for this project.

    Claude Code loads the local, project and user blocks side by side, so the
    gateway being alone in the local block says nothing while either of the
    other two holds a server. `interposed` therefore needs all three: the local
    block is exactly one entry that runs this project's gateway, and the
    project and user blocks hold no entry at all.

    The gateway is the entry that RUNS this project's gateway, under any name:
    the reading `tegh wrap` and `tegh unwrap` use. A lone server of the user's
    own that happens to be called `tegh` gates nothing and is not it.

    Deliberately reads the CONFIG, not tegh's own backup file: the question is
    what the harness would load, and a stale backup would answer a different
    question. Any failure to read any block answers `unestablished`, never
    `interposed`: an unreadable config cannot be evidence that enforcement is in
    place, and this is the direction that must fail safe.

    The three sites come from the Claude Code adapter directly, imported here
    the way `tegh.harnesses` dispatches on harness, because that package offers
    an accessor for the gateway's site alone.
    """
    try:
        from tegh.interpose import _load_site, _runs_gateway  # noqa: PLC0415

        if harness != Harness.CLAUDE_CODE:
            return Routing("unestablished")
        from tegh.harnesses.claude_code import config_sites  # noqa: PLC0415

        sites, gateway_site = config_sites(project, home=home)
        blocks = [(site, _load_site(site)[1]) for site in sites]
    except Exception:  # noqa: BLE001 — any failure means "not proven interposed"
        return Routing("unestablished")

    local_gateways = 0
    gateways_elsewhere = 0
    others: list[str] = []
    for site, block in blocks:
        for name, entry in block.items():
            if not _runs_gateway(entry, str(project)):
                others.append(f"{name} ({site.scope.value} scope)")
            elif site is gateway_site:
                local_gateways += 1
            else:
                gateways_elsewhere += 1

    if others:
        if local_gateways or gateways_elsewhere:
            return Routing("beside-others", tuple(others))
        return Routing("no-gateway")
    if local_gateways == 1 and not gateways_elsewhere:
        return Routing("interposed")
    if local_gateways or gateways_elsewhere:
        return Routing("unestablished")
    return Routing("no-gateway")


def _is_interposed(project: Path, harness: Harness, home: Path | None = None) -> bool:
    """True when the gateway is the only server in the blocks tegh can write.

    See `_routing`, which this is one reading of.
    """
    return _routing(project, harness, home).interposed


#: The tests the built-ins line rests on: tegh reaches for no pre-call hook
#: API anywhere, the one hook it installs registers for `PostToolUse` alone,
#: and what that hook does with an MCP call the gateway did not serve and with
#: a harness-internal tool is read off a real tape.
_BUILT_INS_SOURCE = (
    "tegh/tests/test_posture.py::test_tegh_names_no_builtin_tool_or_hook_api; "
    "tegh/tests/test_hooksite.py::test_the_entry_is_post_tool_use_only_and_observes_every_tool; "
    "tegh/tests/test_hook_e2e.py::"
    "test_mcp_calls_on_the_tape_one_record_each_and_the_unbrokered_one_named"
)

#: What holds for built-ins whether or not they are observed.
_NOT_GATED = (
    "Nothing tegh installs runs before a built-in call or can stop, undo or "
    "contain one, so a built-in write, shell command or fetch happens whatever "
    "the broker would have said. Built-ins are contained at posture 2, never "
    "gated here"
)


def _built_ins_line(observed: Optional[bool]) -> PostureLine:
    """The agent's built-in tools: ungated always, and observed or not per project.

    `observed` is read from the harness's settings at report time
    (`_hook_observes`), for the reason `_interposition_line` reads the config:
    the hook is a per-project fact a report must not assume. The verdict is
    `no` in all three cases, because what the hook adds is evidence and a
    taint, never a gate.
    """
    if observed:
        detail = (
            "OBSERVED, not gated: this project's harness settings carry tegh's "
            "PostToolUse hook, so after each built-in call the harness runs "
            "`tegh hook`, which reports it to the gateway's audit tape. A read "
            "outside the project, a web fetch or a web search taints the turn, so "
            "the agent's next external write through the gateway is held; a read "
            "inside the project is recorded and trusted. A read made through the "
            "shell is recorded as `shell` and taints nothing. A call to an MCP "
            "tool the gateway does not serve (a plugin's server, a claude.ai "
            "connector) is recorded as `claude-code-mcp other`, which names it as "
            "a call that went around the broker, and taints nothing: tegh cannot "
            "tell whether it read, and the platform has no class for it yet "
            "(wjatx/ptc-gal-reference#187). Claude Code's own deferred-tool "
            "lookup (`ToolSearch`) is not recorded. A hook that fails or is "
            "removed lets calls through unrecorded (the harness fails open on "
            "every hook failure). " + _NOT_GATED
        )
    elif observed is None:
        detail = (
            "whether they are even observed is UNKNOWN: the harness settings that "
            "would carry tegh's PostToolUse hook could not be read. " + _NOT_GATED
        )
    else:
        detail = (
            "NOT observed either: this project's harness settings carry no tegh "
            "hook (it was wrapped with --no-hooks, wrapped before tegh installed "
            "one, or is not wrapped), so built-in calls reach neither the gateway "
            "nor the tape. Unwrap and wrap again to add it. " + _NOT_GATED
        )
    return PostureLine(
        claim="The agent's built-in tools (shell, file write, network) are ungated",
        holds="no",
        # Cites TESTS rather than a grep, and deliberately. The first cut of
        # this line cited `grep -rniE 'bash|builtin|PreToolUse|hook'`, which
        # matched its own text the moment it was written, so re-running it
        # verbatim contradicted the claim it was evidence for. A citation that
        # a reader cannot re-run is worse than none: it looks checkable. The
        # first test parses each module's AST and looks only at IDENTIFIERS, so
        # prose about hooks (this comment included) is invisible to it; the
        # second reads the one entry a wrap installs.
        source=_BUILT_INS_SOURCE,
        detail=detail,
    )


def _hook_observes(project: Path, harness: Harness) -> Optional[bool]:
    """True when this project's harness settings run tegh's hook for it.

    False for none, and None when the settings cannot be read: unlike
    `_is_interposed`, either answer is safe to give, because the line's verdict
    does not move with it, so "unknown" is said rather than rounded down.
    """
    try:
        from tegh.harnesses import hook_config_site  # noqa: PLC0415
        from tegh.hooksite import commands_at  # noqa: PLC0415
        from tegh.launch import hook_home_in  # noqa: PLC0415

        site = hook_config_site(harness, project)
        if site is None:
            return False
        commands = commands_at(site)
    except Exception:  # noqa: BLE001 — any failure means "not known"
        return None
    return any(hook_home_in(command, project=project) is not None for command in commands)


def _invariant_gaps(
    routing: "Routing | bool" = False, observed: Optional[bool] = False
) -> list[PostureLine]:
    """The gaps that are live today, stated in the present tense."""
    return [
        _interposition_line(routing),
        _built_ins_line(observed),
        PostureLine(
            claim="A REMOTE server that authenticates by HTTP header cannot be wrapped",
            holds="no",
            source="tegh/configvalues.py",
            detail="a stdio server's literal env credential is MOVED out of "
            "your harness config into tegh's per-project 0600 store and delivered at "
            "spawn through connector_auth.env_map, so a server needing an API key wraps. "
            "The remote dual (connector_auth.header_map) exists in the base and "
            "tegh does not emit it, so a header-authenticated server still refuses",
        ),
        PostureLine(
            claim="MCP stdio servers spawn unconfined, inside the broker's trust zone",
            holds="no",
            source="wjatx/ptc-gal-reference#104; safe_agents/broker/mcp/client.py",
            detail="CONNECTOR-AUTH.md Doctrine 2 requires IAM-scoping AND an OS sandbox "
            "for a local-exec connector; the IAM half has shipped and the sandbox "
            "half has no implementation. Third-party server code runs with the host "
            "reach of whatever spawned it",
        ),
        # Until the pin moved past the base's registry auditor this line said the
        # registry was unaudited outright. It is audited now, on the local store,
        # by the same command the STORE INTEGRITY section runs. What is still
        # missing is on tegh's side: `ceremony_env` names no verify keys, so the
        # signature rules run only when the calling shell names a file, which
        # `_audit_lines` forwards. `test_posture.py` pins that pairing, so
        # this line goes red the day tegh starts naming a verify-keys file.
        PostureLine(
            claim="The registry rows tegh's trust rests on are audited, but the "
            "signatures on their admission records are not verified",
            holds="partial",
            source="safe_agents/broker/mcp/audit.py; tegh/store.py",
            detail="the store audit reported below covers the MCP registry "
            "(TOOLDEF#/TOOLREC#/TOOLPROP#) in this tegh home: row HMACs, row-to-record "
            "consistency, orphans and proposals. PARTIAL because tegh names no issuer "
            "verify keys for that audit. The rules that verify an admission "
            "record's signature run only if the environment `tegh posture` is "
            f"called from names a verify-keys file ({_ISSUER_VERIFY_KEYS_FILE_ENV}), "
            "and then they rest on a file that environment chose. Otherwise they "
            "are skipped, and the record bytes the other rules compare are not "
            "authenticated. STORE INTEGRITY below lists the rules that did not "
            "run in this report. tegh does not write or name a verify-keys file",
        ),
    ]


# ---------------------------------------------------------------------------
# This project's wrap
# ---------------------------------------------------------------------------


def _project_lines(
    project: Path, store: TeghStore, lock_reader: LockReader
) -> list[PostureLine]:
    """Lock state for one project, read through the caller's ONE lock reader.

    `lock_reader` is injected rather than implemented here on purpose. `cli.py`
    already documents its `_read_verified_lock` as "the ONE read path for every
    command that consumes a lock, so `status` and `diff` cannot drift into
    different trust postures — a reader that verifies and a reader that doesn't,
    over the same artifact, is the second-arm gap in its most literal
    form." A posture command that grew its own reader would be that gap's third
    arm, and in the one place whose whole job is not to overclaim.
    """
    lock_path, signature_path = lock_paths(project)
    try:
        loaded = lock_reader(project, store)
    except LockSignatureInvalid as exc:
        return [
            PostureLine(
                claim="This project's lock does NOT verify — it is bytes of unknown origin",
                holds="no",
                source=str(signature_path),
                detail=str(exc),
            )
        ]
    except UnknownLockSigner as exc:
        return [
            PostureLine(
                claim="This project's lock was signed by a key this tegh home does not know",
                holds="unknown",
                source=str(signature_path),
                detail=f"{exc} — this is the expected state for a teammate's lock and is "
                "deliberately NOT reported as tampering",
            )
        ]

    if loaded is None:
        return [
            PostureLine(
                claim="This project is not wrapped",
                holds="no",
                source=f"{lock_path} does not exist",
                detail="run `tegh wrap claude` to pin its MCP tools",
            )
        ]

    # `verified` is three-state and stays that way here: True, False and
    # "nothing to verify with" are three different security claims, and
    # collapsing the last into either of the others is how an unchecked
    # artifact starts reading as a checked one (lockfile.LoadedLock).
    if loaded.verified:
        signature = PostureLine(
            claim="This project's lock carries a VERIFIED signature",
            holds="yes",
            source=str(signature_path),
            detail="the signature binds these bytes to this tegh home's issuer key. It "
            "says nothing about whether the admissions behind the entries were sound, "
            "and nothing against an adversary already running as this user "
            "[tegh/signing.py:44-49]",
        )
    elif loaded.is_signed:
        signature = PostureLine(
            claim="This project's lock is signed but this machine cannot verify it",
            holds="unknown",
            source=f"{signature_path} (no public key in {store.home})",
            detail="not verified is not verified-by-default",
        )
    else:
        signature = PostureLine(
            claim="This project's lock is UNSIGNED",
            holds="no",
            source=f"{signature_path} does not exist; docs/tegh-lock.md",
            detail="the signature file is absent, so this lock carries no evidence "
            "of origin. The absence is all this report read. A lock written with "
            "--allow-unsigned, one written before this tegh home had keys, and one "
            "whose signature file was deleted all look like this (TL3)",
        )

    pinned = sum(len(server.admitted) for server in loaded.lock.servers)
    return [
        signature,
        PostureLine(
            claim=f"{pinned} tool(s) admitted across {len(loaded.lock.servers)} server(s)",
            holds="yes" if pinned else "no",
            source=str(lock_path),
            detail="each entry pins the full ratified definition rather than a hash, so "
            "`tegh diff` can render WHAT changed rather than only that something did "
            "(TL4). Admitted is not gated: whether a call to any of them goes "
            "through the broker is the first line under WHAT DOES NOT",
        ),
        PostureLine(
            claim="Whether those pins still match what the servers advertise is UNKNOWN here",
            holds="unknown",
            source="tegh/posture.py — posture makes no network calls",
            detail="run `tegh diff`; it must reach every server, which is not something a "
            "posture report should do silently",
        ),
    ]


# ---------------------------------------------------------------------------
# Store integrity: the base's store audit, shelled out to (never imported)
# ---------------------------------------------------------------------------

_AUDIT_COMMAND = python_module_argv("safe_agents.broker.grants.audit_command")

#: Every MCP-registry rule the base's audit reports is named with this prefix,
#: and its findings share the grants' `violations` list (the `report_to_dict`
#: contract in `safe_agents/broker/grants/audit_command.py`). The prefix is the
#: only way a caller on this side of the process boundary can tell a registry
#: finding from a grant finding.
_REGISTRY_RULE_PREFIX = "MCP_"

#: Where the base reads each signing role's verify keys on a machine with no
#: parameter store (`safe_agents/broker/grants/issuer_keys.py`). tegh names
#: neither today, which is why the signature rules are skipped.
_ISSUER_VERIFY_KEYS_FILE_ENV = "ISSUER_VERIFY_KEYS_FILE"
_EVALUATOR_VERIFY_KEYS_FILE_ENV = "EVALUATOR_VERIFY_KEYS_FILE"

#: The two names the audit, and only the audit, takes from the calling
#: environment. `ceremony_env` inherits a short list of machine settings and no
#: signing or verification name (#5), so without this the skipped-rules line
#: below would point the reader at a variable that could never arrive. They are
#: carried for this one read-only command and for no ceremony leg. They are not
#: inert there: the file decides which signatures the audit counts as valid, so
#: a signature rule that runs because a shell named one rests on a key file the
#: shell chose, and not on one tegh named.
_AUDIT_VERIFY_KEYS_ENV = (_ISSUER_VERIFY_KEYS_FILE_ENV, _EVALUATOR_VERIFY_KEYS_FILE_ENV)


def _audit_lines(store: TeghStore, project: Path, *, runner=None) -> list[PostureLine]:
    """Run the base's store audit against this project's database.

    One command, one read of the file, two things audited: the grants and the
    MCP admitted-tool registry. They are reported apart here because they are
    different facts to a tegh user, whose admissions live in the registry.

    The database is the project's own (`TeghStore.db_path`), so the counts are
    this project's and say nothing about another one under the same home. A
    project wrapped under the earlier layout, whose admissions are in a
    database tegh does not read, is reported as that and nothing is audited.

    Shelled out rather than imported, for the same reason the ceremony is
    (`cli.py`): tegh may import `safe_agents.broker.schemas` and the gateway
    client from `safe_agents.broker.api`, and neither of those audits a store.
    The `--json` contract is what makes that a contract rather than a scrape.
    """
    earlier_layout = store.layout_refusal(project)
    if earlier_layout is not None:
        return [
            PostureLine(
                claim="Nothing is served for this project: its wrap is not one this "
                "tegh reads admissions for",
                holds="no",
                source=f"{store.manifest_path(project)}; {store.layout_path(project)}",
                detail=earlier_layout,
            )
        ]

    database = store.db_path(project)
    if not database.exists():
        return [
            PostureLine(
                claim="No local store database exists yet",
                holds="unknown",
                source=f"{database} does not exist",
                detail="it is created by the project's first wrap; nothing has been admitted",
            )
        ]

    # `ceremony_env` is reused for its ENV, not because this is a ceremony leg:
    # it is the one place that names BROKER_HMAC_KEY and BROKER_SQLITE_PATH
    # together, which is exactly what the audit needs, and re-deriving them here
    # would be a second arm of the same resolution. The identity it also sets is
    # inert for a read-only command; the audit writes nothing.
    try:
        env = store.ceremony_env(role="checker", project=project)
        env.update(inherited_env(_AUDIT_VERIFY_KEYS_ENV))
    except TeghStoreError as exc:
        return [
            PostureLine(
                claim="The store could not be audited",
                holds="unknown",
                source=f"{store.home}",
                detail=str(exc),
            )
        ]

    run = runner or _run_audit
    try:
        completed = run(env, str(database))
    except OSError as exc:  # pragma: no cover - environment-dependent
        return [
            PostureLine(
                claim="The store audit could not be run",
                holds="unknown",
                source=" ".join(_AUDIT_COMMAND),
                detail=str(exc),
            )
        ]

    if completed.returncode == 2 or not completed.stdout.strip():
        return [
            PostureLine(
                claim="The store audit could not run against this store",
                holds="unknown",
                source=" ".join(_AUDIT_COMMAND),
                detail=(completed.stderr or "no output").strip()
                + " — 'could not run' is not 'clean'",
            )
        ]

    try:
        payload = json.loads(completed.stdout)
    except ValueError:  # pragma: no cover - defensive
        return [
            PostureLine(
                claim="The store audit produced output this version cannot parse",
                holds="unknown",
                source=" ".join(_AUDIT_COMMAND),
                detail="reporting unknown rather than assuming clean",
            )
        ]

    lines = [_integrity_line(payload, database)]
    if payload["skipped_rules"]:
        lines.append(
            PostureLine(
                claim=f"{len(payload['skipped_rules'])} audit rule(s) did not run",
                holds="partial",
                source=" ".join(_AUDIT_COMMAND) + " --json (skipped_rules)",
                detail=", ".join(payload["skipped_rules"])
                + ". A skipped rule is not a passed rule. tegh gives this audit the "
                "store's HMAC key and names no verify keys, so a rule that needs a "
                "verify key runs only if the calling environment supplies one "
                f"({_ISSUER_VERIFY_KEYS_FILE_ENV}, {_EVALUATOR_VERIFY_KEYS_FILE_ENV})",
            )
        )
    return lines


def _integrity_line(payload: dict, database: Path) -> PostureLine:
    """The audit's verdict, with grant findings and registry findings kept apart.

    The base returns both in one `violations` list. Counting that list under a
    grants label would report a tampered registry row as a grant violation, and
    a clean grants result as a statement about the registry.

    `mcp_rows` is three-state on purpose. A number means the registry was
    audited. `None`, or the key missing, means it was not, which the base
    reports for a DynamoDB table (wjatx/ptc-gal-reference#96) and which a base
    older than the registry auditor reports by omission. Not audited is never
    rendered as zero rows, and never as a clean result.
    """
    examined = payload["examined"]
    violations = payload["violations"]
    registry_findings = [
        found for found in violations if found["rule"].startswith(_REGISTRY_RULE_PREFIX)
    ]
    grant_findings = len(violations) - len(registry_findings)
    registry_rows = examined.get("mcp_rows")
    grants_clause = f"{grant_findings} grant violation(s) over {examined['grants']} grant(s)"
    fired = ", ".join(sorted({found["rule"] for found in violations}))
    source = f"python -m safe_agents.broker.grants.audit_command --sqlite {database}"

    if registry_rows is None:
        return PostureLine(
            claim=f"Store-integrity audit: {grants_clause}; the MCP registry was NOT audited",
            holds="no" if violations else "partial",
            source=source,
            detail="tegh's admissions live in the MCP registry (TOOLDEF#/TOOLREC#), "
            "and this run did not cover it, so the result says nothing about them. "
            "The base audits the registry on a local sqlite store only; no audit "
            "identity can read a DynamoDB registry (wjatx/ptc-gal-reference#96)"
            + (f". Rules that fired: {fired}" if fired else ""),
        )

    return PostureLine(
        claim=f"Store-integrity audit: {grants_clause}, {len(registry_findings)} "
        f"registry violation(s) over {registry_rows} admitted-tool row(s)",
        holds="yes" if payload["clean"] else "no",
        source=source,
        detail=f"also examined: {examined['records']} grant record(s), "
        f"{examined.get('mcp_records')} admission record(s) and "
        f"{examined.get('mcp_proposals')} admission proposal(s), from one read of "
        "the store. The counts are over the rules that ran"
        + (f". Rules that fired: {fired}" if fired else ""),
    )


def _run_audit(env: dict, db_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*_AUDIT_COMMAND, "--sqlite", db_path, "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


#: What posture 1 means, said once for every branch of `_rung_reason`.
_POSTURE_1_MEANS = (
    "posture 1 means deterministic gating plus taint plus audit honesty on one machine"
)
_RUNG_SOURCE = "docs/posture-ladder.md; the harness config's mcpServers blocks"


def _rung_reason(routing: Routing) -> PostureLine:
    """Why this configuration sits on the posture it does, per `Routing` state.

    Four texts, because the three states below posture 1 are different facts:
    with no gateway entry no call is gated, with the gateway beside other
    servers some calls are, and with an unreadable config neither is known.
    """
    if routing.state == "interposed":
        return PostureLine(
            claim="This configuration is at posture 1 — calls to the tools the "
            "gateway serves are gated on this one machine, by tegh, as the same OS "
            "user as the agent",
            holds="partial",
            source=_RUNG_SOURCE,
            detail=f"{_POSTURE_1_MEANS}, and that is what this is for calls to the "
            "tools the gateway serves, and for no other call. An MCP server the "
            "harness loads from a source tegh cannot write (a plugin, a claude.ai "
            "connector) is not behind the gateway. It is not "
            "posture 2: tegh runs as the same OS user as the agent it serves, so a "
            "determined same-user adversary reads and writes tegh's home too. "
            "The agent's built-in shell, file and web tools are gated by nothing: "
            "with tegh's hook installed they are observed after they run, and a "
            "read outside the project taints the turn",
        )
    if routing.state == "beside-others":
        return PostureLine(
            claim="This configuration is BELOW posture 1 — the gateway is one MCP "
            "server among several, so only some MCP calls are gated",
            holds="partial",
            source=_RUNG_SOURCE,
            detail=f"{_POSTURE_1_MEANS}. Admission, the signed ledger, the signed "
            "lock and a gateway are all in place, and this project's config runs "
            "the gateway; it also holds other MCP servers the harness loads beside "
            "it, and a call to one of those is gated by nothing",
        )
    if routing.state == "unestablished":
        return PostureLine(
            claim="This configuration is reported BELOW posture 1 — tegh could not "
            "establish that calls are routed through the gateway",
            holds="partial",
            source=_RUNG_SOURCE,
            detail=f"{_POSTURE_1_MEANS}. Admission, the signed ledger, the signed "
            "lock and a gateway are all in place; whether this project's config "
            "points at the gateway and at nothing else could not be read, so no "
            "gating is claimed",
        )
    return PostureLine(
        claim="This configuration is BELOW posture 1 — the evidence machinery is "
        "real, the enforcement half is not wired",
        holds="partial",
        source=_RUNG_SOURCE,
        detail=f"{_POSTURE_1_MEANS}. Admission, the signed ledger, the signed lock "
        "and a gateway are all in place; this project's config does not point at "
        "the gateway, so no call is gated",
    )


def build_report(
    project: Path,
    store: TeghStore,
    harness: Harness,
    *,
    lock_reader: LockReader,
    audit_runner=None,
    harness_home: Path | None = None,
) -> PostureReport:
    """Assemble the report for one project under one tegh home.

    The posture is DERIVED, never configured, and it is derived from ONE fact: does
    this project's harness config actually route calls through tegh. A posture
    describes where the boundary IS, not what has been built — which is why the
    posture did not move when the gateway landed and does move, per project,
    once the interposition does.

    So two projects on one machine can sit on different postures, and that is
    correct rather than an inconsistency: the wrapped one has a boundary and the
    unwrapped one does not.
    """
    from tegh.cluster import cluster_lines, observe, posture_note
    from tegh.harnesses import durability_line

    routing = _routing(project, harness, harness_home)
    wrapped = routing.interposed
    gaps = _invariant_gaps(routing, _hook_observes(project, harness))

    # Off-cluster this is empty, and deliberately: a laptop report should not
    # grow a row of "no cluster here" lines. The posture itself is NOT raised by
    # containment — see `cluster.posture_note` for why a contained pod that gates
    # nothing is a boundary around nothing.
    pod = observe()
    cluster = (
        () if pod is None else (posture_note(pod, interposed=wrapped), *cluster_lines(pod))
    )

    return PostureReport(
        posture="1" if wrapped else "pre-1",
        rung_reason=_rung_reason(routing),
        polarity=tuple(_polarity_lines(project, store)),
        holds=tuple(_invariant_holds()),
        gaps=tuple(gaps),
        project=tuple(
            [durability_line(harness), *_project_lines(project, store, lock_reader)]
        ),
        store=tuple(_audit_lines(store, project, runner=audit_runner)),
        cluster=cluster,
    )


#: "n/a" is for a line that is a DECLARATION rather than a claim about whether
#: something holds — the polarity is a decision this consumer made, and grading it
#: yes/no would invite reading it as a control that is switched on.
_MARK = {"yes": "+", "no": "-", "partial": "~", "unknown": "?", "n/a": "="}


def render(report: PostureReport) -> str:
    """Human rendering. Every line prints its source — the citation is part of
    the output, not metadata behind a flag, because a reader who cannot see the
    basis cannot weigh the claim."""
    out = [
        "tegh posture",
        "",
        f"  POSTURE {report.posture}  (docs/posture-ladder.md)",
        f"    {report.rung_reason.claim}",
        f"      {report.rung_reason.detail}",
        f"      source: {report.rung_reason.source}",
    ]
    # Unconditional, and BEFORE the graded sections: the polarity is the frame a
    # reader needs to weigh everything below it, not a row among the findings.
    out += ["", "  SAFE-DEFAULT POLARITY"]
    for line in report.polarity:
        out.append(f"    [{_MARK[line.holds]}] {line.claim}")
        if line.detail:
            out.append(f"        {line.detail}")
        out.append(f"        source: {line.source}")
    for title, lines in (
        ("WHAT HOLDS", report.holds),
        ("WHAT DOES NOT", report.gaps),
        ("THIS PROJECT", report.project),
        ("STORE INTEGRITY", report.store),
        ("CLUSTER CONTAINMENT", report.cluster),
    ):
        if not lines:
            continue
        out += ["", f"  {title}"]
        for line in lines:
            out.append(f"    [{_MARK[line.holds]}] {line.claim}")
            if line.detail:
                out.append(f"        {line.detail}")
            out.append(f"        source: {line.source}")
    return "\n".join(out)


def to_dict(report: PostureReport) -> dict:
    def one(line: PostureLine) -> dict:
        return {
            "claim": line.claim,
            "holds": line.holds,
            "source": line.source,
            "detail": line.detail,
        }

    return {
        "posture": report.posture,
        "rung_reason": one(report.rung_reason),
        "polarity": [one(x) for x in report.polarity],
        "holds": [one(x) for x in report.holds],
        "gaps": [one(x) for x in report.gaps],
        "project": [one(x) for x in report.project],
        "store": [one(x) for x in report.store],
        "cluster": [one(x) for x in report.cluster],
    }
