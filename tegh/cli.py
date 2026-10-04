"""The `tegh` command — pin the MCP tools a coding project already uses.

Ten subcommands, and a deliberate division of labour with the base:

  init     mint this machine's tegh home (HMAC key, local issuer signing key)
  wrap     discover -> snapshot -> review -> admit -> tegh.lock -> interpose
  gateway  run the broker's MCP mouth for one wrapped project (what the harness spawns)
  call     drive one tool call through the gateway as the wrapped principal (`call.py`)
  approve  release a call the broker is holding (the local arm of the platform's owner-approval seam)
  audit    show this project's tape and where it lives; --verify checks the chain
  unwrap   restore the harness config the wrap displaced, credentials included, after asking
  status   read the committed lock back and report what is pinned
  diff     the lock's pinned definitions vs. what the servers advertise NOW
  posture  which floor properties hold in THIS configuration, and which do not

**The ceremony is WRAPPED, never reimplemented.** `wrap` shells out to
`python -m safe_agents.broker.mcp.commands` for snapshot, admit-propose and
admit-ratify. That is not plumbing convenience — it is the import boundary
holding: tegh stands on `safe_agents.broker.schemas`, on the gateway client the
base publishes from `safe_agents.broker.api` (three names, used by `call.py`
alone) and on the base's public CLI, never on broker internals, which is what let tegh
move into its own repository as a packaging change rather than an
untangling. tegh still never imports `build_runtime` or anything else that
decides. It also means maker≠checker, the proposal HMAC, record-before-row, and
the DSSE ledger are the base's implementations doing their real jobs, not a
local re-derivation of them.

**Where authority lives.** Key #1 (the namespace + ToolOp declaration) is a
manifest under tegh's home, key #2 (the activation row) is a sqlite registry row
beside it — both OUTSIDE the project tree the wrapped agent can write. The
`tegh.lock` this writes into the project is a PROJECTION of that admission
(TL1), read by humans and by `status`/`diff`, and never by the per-call path.
Reversing that would let an injected agent admit its own tools with one edit.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import yaml

from safe_agents.broker.schemas import (
    McpServerSnapshot,
    McpToolDef,
    ToolOp,
    compute_tool_def_hash,
)
from tegh.classify import (
    CorrectionRefused,
    ToolOpProposal,
    apply_corrections,
    propose_tool_op,
)
from tegh import configvalues
from tegh.configvalues import LiteralField
from tegh.discovery import (
    DiscoveredServer,
    DiscoveryResult,
    UnrepresentableTransportError,
)
from tegh import interpose
from tegh import unwrap
from tegh.harnesses import claude_code
from tegh.launch import DEFAULT_CALL_TIMEOUT_SECONDS, tegh_launcher
from tegh.lock import (
    LOCK_FORMAT_VERSION,
    AttestationKind,
    Harness,
    LockAttestation,
    LockedServer,
    LockedTool,
    TeghLock,
)
from tegh.lockfile import (
    LoadedLock,
    LockSignatureInvalid,
    UnsignedLockRefused,
    lock_paths,
    read_lock,
    write_lock,
)
from tegh.posture import build_report, render, to_dict
from tegh.review import (
    compute_tool_delta,
    render_ack_requirements,
    render_first_admission,
    render_proposal,
    render_tool_delta,
)
from tegh.signing import (
    UnknownLockSigner,
    resolve_local_verifier,
    signer_from_pem,
)
from tegh.store import (
    TeghStore,
    TeghStoreError,
    project_slug,
    provision,
    tegh_home,
)

#: Adapters by harness name. A closed registry: `wrap <harness>` selects from
#: it and can never name a module to import (docs/config-provenance.md).
ADAPTERS = {Harness.CLAUDE_CODE: claude_code}

#: What a user may TYPE, mapped onto the format's closed catalog. The alias
#: exists because `tegh wrap claude` is what the command reads like out loud;
#: it is resolved to `Harness.CLAUDE_CODE` immediately and never reaches the
#: lock, so the frozen format's one closed catalog stays exactly as frozen
#: (TL9a) and the CLI's spelling can change without touching the artifact.
HARNESS_ALIASES = {
    "claude": Harness.CLAUDE_CODE,
    Harness.CLAUDE_CODE.value: Harness.CLAUDE_CODE,
}

_CEREMONY = [sys.executable, "-m", "safe_agents.broker.mcp.commands"]
_PROPOSAL_TTL_HOURS = "1"

#: Answer for one tool in the batched review.
Prompt = Callable[[str], str]


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _run_ceremony(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*_CEREMONY, *args], env=env, capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def init_command(args: argparse.Namespace) -> int:
    home = Path(args.home).expanduser() if args.home else tegh_home()
    try:
        store = provision(home)
    except TeghStoreError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"tegh home provisioned: {store.home}")
    print(f"  store database   {store.db_path} (created on first admission)")
    print(f"  HMAC key         {store.hmac_key_path} (0600)")
    print(f"  issuer key       {store.issuer_key_path} (0600)")
    print(f"  issuer key_id    {store.issuer_key_id()}")
    print(
        "\nThis directory is local authority: it holds the tool namespace and the\n"
        "activation rows, deliberately outside any project tree. A same-user\n"
        "attacker can still read it — that is posture 1, and `tegh posture` says so."
    )
    return 0


# ---------------------------------------------------------------------------
# wrap
# ---------------------------------------------------------------------------


def _render_discovery(result: DiscoveryResult) -> str:
    lines = [f"Discovered {len(result.effective)} MCP server(s) for {result.project_path}:"]
    for server in result.effective:
        detail = server.url or " ".join(filter(None, [server.command, *server.args]))
        lines.append(
            f"  {server.server_id:<24} {server.transport:<6} scope={server.scope.value:<8} "
            f"{server.activation.value:<16} {detail}"
        )
    if result.shadowed:
        lines.append("")
        lines.append("Shadowed by a higher-precedence scope (NOT wrapped):")
        for server in result.shadowed:
            lines.append(
                f"  {server.server_id:<24} scope={server.scope.value} "
                f"beaten by {server.shadowed_by.value if server.shadowed_by else '?'}"
            )
    if result.skipped:
        lines.append("")
        lines.append("Entries this harness would not load (recorded, not dropped):")
        for entry in result.skipped:
            lines.append(f"  {entry.server_id:<24} scope={entry.scope.value}  {entry.reason}")
    return "\n".join(lines)


def _render_findings(result: DiscoveryResult) -> str:
    if not result.findings:
        return ""
    order = {"blocking": 0, "warning": 1, "info": 2}
    lines = ["", "Findings:"]
    for finding in sorted(result.findings, key=lambda f: order[f.severity]):
        mark = {"blocking": "!!", "warning": " !", "info": "  "}[finding.severity]
        where = f" [{finding.server_id}]" if finding.server_id else ""
        lines.append(f"  {mark} {finding.kind.value}{where}: {finding.detail}")
    return "\n".join(lines)


def _project_principal(project: Path) -> dict:
    """The principal this project's grants are issued to.

    DERIVED from the project path, never asked for: a principal the user types
    is a principal the user can retype differently next wrap, and every grant is
    keyed on this tuple — a changed `agentId` silently orphans the whole grant
    set (they read as absent, and absent is denied, so the symptom is a gateway
    that serves nothing for no visible reason).

    `tier: "D"` is the floor of the coarse trust ladder, which is the honest
    tier for a laptop wrap: posture 1, same OS user, nothing confined
    (`docs/posture-ladder.md`).
    """
    return {
        "agentId": f"tegh-{project_slug(project)}",
        "skill": "wrapped-agent",
        "user": "local-developer",
        "tier": "D",
    }


def _synthesize_manifest(
    servers: Sequence[DiscoveredServer],
    *,
    polarity: str,
    project: Path,
    daily_cap: int,
    tools_by_server: Optional[dict[str, list[tuple[McpToolDef, ToolOp]]]] = None,
    env_by_server: Optional[dict[str, dict[str, str]]] = None,
    credentials_by_server: Optional[dict[str, dict[str, str]]] = None,
) -> dict:
    """Build key #1: the namespace + ToolOp declaration for this project.

    Written to tegh's home, NEVER into the project. Discovery reads config the
    wrapped agent can write, so what discovery proposes is untrusted input; what
    a human confirmed in the review is what lands here, and this file is what
    the ceremony reads back.

    `polarity` is REQUIRED and has no base default. The safe-default polarity is
    re-derived per agent by rule (a base default would be a latent safety bug),
    so it is a flag on `wrap` rather than a constant in this module.

    `env_by_server` carries the literal config values a human classified as
    configuration into `McpServerDecl.env`, the field the base documents
    as "the STATIC half of the child environment ... credential material never
    appears here". Before this the block was dropped entirely, so a wrapped
    `@modelcontextprotocol/server-memory` spawned WITHOUT its `MEMORY_FILE_PATH`
    and silently read a different memory file than the one it had been reading
    for months — a wrap that reports success while changing what the server
    does. Only classified values arrive here; a value classified as a CREDENTIAL
    goes to `connector_auth.env_map` instead, built from
    `credentials_by_server`. The base refuses an overlap between the two halves
    at manifest load, and the classification is what keeps them disjoint.
    """
    confirmed = tools_by_server or {}
    environments = env_by_server or {}
    credentials = credentials_by_server or {}
    mcp_servers: dict[str, dict] = {}
    tool_ops: list[dict] = []

    for server in servers:
        declaration: dict = {
            "transport": "stdio" if server.transport == "stdio" else "streamable-http",
        }
        if server.transport == "stdio":
            if server.command:
                declaration["command"] = server.command
                if server.args:
                    declaration["args"] = list(server.args)
                if server.cwd:
                    declaration["cwd"] = server.cwd
                if environment := environments.get(server.server_id):
                    declaration["env"] = dict(environment)
        else:
            declaration["url"] = server.url
        admitted = confirmed.get(server.server_id, [])
        if admitted:
            declaration["tools"] = [
                {"tool_name": definition.tool_name} for definition, _ in admitted
            ]
        mcp_servers[server.server_id] = declaration
        tool_ops.extend(op.model_dump(mode="json") for _, op in admitted)

    # The admitted coordinates, as the action classes the gateway will serve.
    # WITHOUT this the wrap produces a manifest that cannot build a runtime at
    # all: `served_registry()` is built from granted classes, so a gateway over a
    # grant-less manifest advertises nothing and denies everything (found
    # by running it). The classes are exactly what the human confirmed in the
    # review — never the discovered set, which is untrusted input.
    grant_classes = sorted(f"{op['tool']}.{op['op']}" for op in tool_ops)

    # Credential delivery. One LEAF per server, named for the server
    # itself — the base's default is leaf == connector name, and declaring it
    # explicitly keeps this manifest readable and independent of that default.
    # `env_map` is an identity mapping because the fields are STORED under the
    # names the server already expects; it is still an explicit allowlist, so an
    # extra field appearing in the leaf later would not reach the child.
    connector_auth = {
        server_id: {
            "strategy": "static_secret",
            "env_map": {name: name for name in sorted(values)},
        }
        for server_id, values in credentials.items()
        if server_id in mcp_servers and values
    }
    connector_secrets = {server_id: server_id for server_id in connector_auth}

    payload = {
        "principal": _project_principal(project),
        "grant_classes": grant_classes,
        "envelope": {
            "polarity": polarity,
            # NAMED, never defaulted. The base refuses to apply its implicit
            # platform default to granted WRITE classes, and a cap is
            # authority: whatever number lands here is the per-op daily budget
            # the wrapped agent gets. `wrap --daily-cap` is how the operator
            # says it out loud.
            "caps": {"actions_per_utc_day": daily_cap},
            "allowlists": {"tools": grant_classes},
        },
        # Every constructible server must also be wired as a connector — the
        # base refuses a construction block nothing wires, because a server the
        # manifest can spawn but no connector reaches is dead config that reads
        # like a live capability.
        "connectors": sorted(mcp_servers),
        "mcp_servers": mcp_servers,
        "tool_ops": tool_ops,
    }
    if connector_auth:
        payload["connector_auth"] = connector_auth
        payload["connector_secrets"] = connector_secrets
    return payload


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class ConfigValueDecisions:
    """What the operator settled about this wrap's literal config values."""

    #: Coordinates cleared as configuration OR relocated as credentials, mapped
    #: to the digest of the value that was decided about. The gate re-reads the
    #: config and re-compares, so a value that moves during the wrap is refused
    #: rather than approved by name. Credentials belong here too: their literal
    #: is displaced by the same interposition, so the same window applies.
    cleared: dict[str, str]
    #: `{server_id: {NAME: value}}` for the servers this wrap will construct.
    env_by_server: dict[str, dict[str, str]]
    #: `{server_id: {NAME: value}}` for values classified as CREDENTIALS and
    #: relocated into tegh's per-project secret store.
    credentials_by_server: dict[str, dict[str, str]] = dc_field(default_factory=dict)
    #: Relocated coordinate -> the bare LEAF it now lives under. Interposition
    #: replaces each of these in the backup with a reference, so the credential
    #: survives in exactly one place.
    relocated: dict[str, str] = dc_field(default_factory=dict)


def _classify_config_values(
    store: TeghStore,
    project: Path,
    sites: Sequence[interpose.ConfigSite],
    servers: Sequence[DiscoveredServer],
    *,
    prompt: Prompt,
    identity: str,
) -> Optional[ConfigValueDecisions]:
    """Ask which literal config values are credentials. `None` refuses the wrap.

    Runs BEFORE the manifest is written and before a single ceremony
    subprocess: a wrap that is going to refuse should refuse before it burns an
    admission, which is the posture `bulk-ratify` takes for the same reason.
    This question used to be asked implicitly, at the very last step, and
    answered "everything is a credential" — so every real project refused after
    paying for a full review and admission pass.

    `--admit-all` deliberately does NOT reach this prompt. It is a statement
    about tools, and a credential decision is precisely the kind a bulk flag
    must not supply. What makes a second wrap non-interactive is the persisted
    record, which is bound to the values it was made about.
    """
    record = configvalues.load_record(store.config_decisions_path(project))
    # Keyed by SCOPE, not by path: Claude Code's local and user scopes are two
    # different blocks in the same `~/.claude.json`, so a path key would collapse
    # them and hand one scope's values to the other.
    blocks: dict[str, dict] = {}
    fields: list[LiteralField] = []
    for site in sites:
        block = interpose.read_block(site)
        blocks[site.scope.value] = block
        fields.extend(configvalues.inventory(block, scope=site.scope.value))

    if not fields:
        return ConfigValueDecisions(cleared={}, env_by_server={})

    pending = configvalues.uncleared(fields, record)
    credentials: list[LiteralField] = []
    if pending:
        print()
        print("=" * 78)
        print(
            f"CONFIG VALUES — {len(pending)} literal value(s) this wrap would displace"
        )
        print("=" * 78)
        print(
            "tegh cannot tell a credential from a path, and will not print these\n"
            "values to help you decide — they are the thing being protected. What\n"
            "follows is each field's NAME and what tegh could check about it.\n\n"
            "  CREDENTIAL   is MOVED out of your harness config into tegh's\n"
            "               per-project secret store, and delivered to the server\n"
            "               at spawn. It is gone from the config afterwards.\n"
            "  CONFIGURATION is carried through to the server tegh spawns\n\n"
            "Answering nothing means CREDENTIAL: the safe answer is the one that\n"
            "refuses to copy."
        )
        for field in pending:
            print()
            print(
                configvalues.describe(
                    field, changed=record.was_decided_about_another_value(field)
                )
            )
            try:
                answer = prompt(
                    f"     is {field.name} a CREDENTIAL? [Y] yes, relocate it  "
                    "[n] no, it is configuration: "
                ).strip().lower()
            except EOFError:
                # No one is there to answer. Resolving that to the permissive
                # side would let a wrap in a script classify a credential as
                # config with nobody having decided anything.
                print("     (no input available)")
                answer = ""
            if answer in ("n", "no"):
                record.record(field, decided_at=_now(), decided_by=identity)
                print(f"     -> configuration  {field.coordinate}")
            else:
                # Never recorded, and relocation did not change that: a relocated
                # credential is GONE from the harness config, so the next wrap
                # finds no literal at this coordinate and has nothing to re-ask.
                record.forget(field.coordinate)
                credentials.append(field)
                verb = "relocate" if field.is_deliverable else "REFUSE"
                print(f"     -> CREDENTIAL     {field.coordinate}  ({verb})")

    configvalues.save_record(store.config_decisions_path(project), record)

    # A credential in a block tegh cannot deliver (`headers`) still refuses —
    # that is `header_map`, the remote leg, deliberately a follow-on. Splitting
    # here rather than at the prompt keeps the question the operator answers the
    # same one regardless of which transport happens to carry it.
    unrelocatable = [field for field in credentials if not field.is_deliverable]
    if unrelocatable:
        sys.stdout.flush()  # keep the refusal after the answers when redirected
        print(f"\nREFUSED: {configvalues.render_refusal(unrelocatable)}", file=sys.stderr)
        return None

    cleared = configvalues.cleared_digests(fields, record)
    if not pending:
        print(
            f"\n{len(cleared)} literal config value(s) were already classified as "
            f"configuration\n({store.config_decisions_path(project)}); each is bound "
            "to the value it was decided about."
        )

    # Deliver only to the servers this wrap actually constructs, matched on the
    # SCOPE each was resolved at — a shadowed duplicate at a losing scope must
    # not hand its value to the winner.
    by_scope: dict[tuple[str, str], dict[str, str]] = {}
    for scope, block in blocks.items():
        carried = configvalues.read_cleared_env(block, cleared, scope=scope)
        for server_id, values in carried.items():
            by_scope[(scope, server_id)] = values

    env_by_server: dict[str, dict[str, str]] = {}
    delivered: set[str] = set()
    for server in servers:
        if values := by_scope.get((server.scope.value, server.server_id)):
            env_by_server[server.server_id] = values
            delivered.update(
                f"{server.scope.value}:{server.server_id}.env.{name}" for name in values
            )

    _report_undelivered_values(fields, record, delivered, servers)

    # -- relocation ---------------------------------------------------------
    # The leaf is the bare server_id: by ruling, a manifest names a LEAF
    # and the topology supplies the scope, and here the per-project directory
    # IS the topology (docs/config-provenance.md, "Secret naming").
    relocated: dict[str, str] = {}
    credentials_by_server: dict[str, dict[str, str]] = {}
    if credentials:
        constructed = {
            (server.scope.value, server.server_id) for server in servers
        }
        by_coordinate = {field.coordinate: field for field in credentials}
        for scope, block in blocks.items():
            wanted = {
                coordinate: field.value_sha256
                for coordinate, field in by_coordinate.items()
                if field.scope == scope
            }
            if not wanted:
                continue
            try:
                found = configvalues.read_cleared_credentials(
                    block, wanted, scope=scope
                )
            except configvalues.CredentialMoved as exc:
                print(f"\nREFUSED: {exc}", file=sys.stderr)
                return None
            for server_id, values in found.items():
                if (scope, server_id) not in constructed:
                    # A server this wrap does not construct (shadowed, or an
                    # unrepresentable transport) has nowhere to deliver to.
                    # Its literal is still displaced, so say so rather than
                    # relocating a credential no gateway will ever use.
                    print(
                        f"\n!! {scope}:{server_id} holds a credential but is not a "
                        "server this wrap constructs;\n   it is NOT relocated and "
                        "its definition still moves to tegh's backup."
                    )
                    return None
                credentials_by_server[server_id] = values
                store.write_secret_leaf(project, server_id, values)
                for name in values:
                    relocated[f"{scope}:{server_id}.env.{name}"] = server_id

        moved = sum(len(values) for values in credentials_by_server.values())
        print(
            f"\n{moved} credential value(s) moved into "
            f"{store.secrets_path(project)} (0600).\n"
            "They will be delivered to the server at spawn via "
            "connector_auth.env_map,\nand are removed from your harness config "
            "by the interposition below."
        )

    # The gate re-reads the config at the END of the wrap; a relocated
    # credential's literal is still sitting there until interposition runs, so
    # it must be approved by DIGEST like every other displaced value.
    gate_cleared = dict(cleared)
    gate_cleared.update(
        {field.coordinate: field.value_sha256 for field in credentials}
    )

    return ConfigValueDecisions(
        cleared=gate_cleared,
        env_by_server=env_by_server,
        credentials_by_server=credentials_by_server,
        relocated=relocated,
    )


def _report_undelivered_values(
    fields: Sequence[LiteralField],
    record: configvalues.DecisionRecord,
    delivered: set[str],
    servers: Sequence[DiscoveredServer],
) -> None:
    """Name every value that cleared the gate and still does not reach a server.

    Three ways that happens: a `headers` value (the broker's server declaration
    has no static header field), a server this wrap does not construct (shadowed
    at a losing scope, or on an unrepresentable transport), and a `${VAR}`
    reference (which tegh will not expand — that would pull a live secret into
    its memory, which discovery refuses to do).

    All three are reported rather than dropped. A wrap that quietly changes what
    a server receives is the same silent-incompleteness failure the discovery
    half already refuses to commit.
    """
    stranded = [
        field.coordinate
        for field in fields
        if record.clears(field) and field.coordinate not in delivered
    ]
    if stranded:
        print(
            "\n  !! classified as configuration, but NOT delivered to the wrapped\n"
            "     server — check whether it still behaves as you expect:"
        )
        for coordinate in sorted(stranded):
            print(f"       {coordinate}")

    unexpanded = {
        f"{server.scope.value}:{server.server_id}": [
            path
            for path in server.unexpanded_fields
            if path.split(".", 1)[0] in configvalues.SECRET_BEARING_BLOCKS
        ]
        for server in servers
    }
    report = configvalues.render_undelivered_references(
        {name: paths for name, paths in unexpanded.items() if paths}
    )
    if report:
        print()
        print(report)


def _snapshot_server(
    store: TeghStore, project: Path, server_id: str, env: dict[str, str]
) -> tuple[Optional[McpServerSnapshot], str]:
    """Capture one server's live advertised set via the base's `snapshot`."""
    out = store.snapshot_path(project, server_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _run_ceremony(
        [
            "snapshot",
            "--server-id",
            server_id,
            "--manifest",
            str(store.manifest_path(project)),
            "--out",
            str(out),
        ],
        env,
    )
    if result.returncode != 0 or not out.exists():
        return None, (result.stdout + result.stderr).strip()
    return McpServerSnapshot.model_validate_json(out.read_bytes()), ""


#: The MCP SDK's wrapper for any child that dies during connect. It names no
#: cause; the child's own stderr, printed beside it, always does.
_SDK_TASKGROUP_NOISE = "unhandled errors in a TaskGroup"


def _render_snapshot_failure(
    server: DiscoveredServer, error: str, *, delivered: Sequence[str]
) -> str:
    """Say why a server could not be reached, in the server's own words.

    The raw failure for a credential-needing server is `ERROR: could not
    discover tools from 'x': unhandled errors in a TaskGroup (1 sub-exception)`,
    which tells an operator nothing — this was called out specifically after a
    real wrap attempt against a vendor MCP produced exactly that and no hint
    that the server had wanted credentials.

    The child's stderr is already in this text; it was simply printed after the
    SDK's line and given no billing. So the fix is mostly ordering and framing,
    plus one inference stated AS an inference: a server that references
    environment variables and received none is very likely failing for that
    reason, and saying so costs nothing when the guess is wrong.
    """
    lines = [f"\n!! {server.server_id}: could not be reached"]
    said = [
        line.strip()
        for line in error.splitlines()
        if line.strip()
        and _SDK_TASKGROUP_NOISE not in line
        and not line.strip().startswith("ERROR: could not discover tools")
    ]
    if said:
        lines.append("   the server said:")
        lines.extend(f"     {line}" for line in said)
    else:
        # Nothing but the wrapper. Say that rather than reprinting it as though
        # it were an explanation.
        lines.append(
            "   it exited during startup without saying why (the MCP SDK reports "
            "this as\n   an unhandled TaskGroup error, which names no cause)."
        )

    wanted = [name for name in server.env_names if name not in delivered]
    if wanted:
        lines.append(
            f"   NB this server's config names {', '.join(sorted(wanted))} and this "
            "wrap delivered\n      no value for it. That is the most common reason a "
            "server fails to start here."
        )
        if server.unexpanded_vars:
            lines.append(
                "      Its config uses a ${...} reference, which tegh does not expand "
                "— replace it\n      with the literal value and re-run `tegh wrap`, "
                "which will offer to relocate it."
            )
    lines.append("   Not wrapped. Fix the server, then re-run `tegh wrap`.")
    return "\n".join(lines)


def _confirm_tool(
    tool_def: McpToolDef,
    proposal: ToolOpProposal,
    *,
    prompt: Prompt,
) -> tuple[ToolOpProposal, bool]:
    """Confirm, CORRECT, or skip one tool. Returns (final proposal, admitted).

    The correction half was missing for long enough to make the
    product unusable: a server that advertises no annotations gets the
    restrictive defaults (`effect: write`, `reversible: false`), the grant is
    issued at on-loop, and the PDP holds every irreversible external write — so
    every call to a correctly-wrapped server was held for approval, forever,
    with no way for a reviewer who could plainly see `get_entry` was a read to
    say so. Confirming a proposal you believe is wrong is not ratification, it
    is rubber-stamping, and skipping was the only alternative.

    Nothing is admitted by default and `[N]` remains the empty-input answer: an
    edit affordance must not become a nudge toward admitting. Editing then
    re-renders and asks again, so the human always confirms the values that
    actually bind rather than the ones they started from.
    """
    while True:
        answer = prompt(
            f"  {tool_def.tool_name}: [y] admit  [e] edit classification  [N] skip "
        ).strip().lower()
        if answer in ("y", "yes"):
            return proposal, True
        if answer in ("e", "edit"):
            proposal = _edit_classification(proposal, prompt=prompt)
            print()
            print(render_proposal(proposal))
            continue
        return proposal, False


def _edit_classification(
    proposal: ToolOpProposal, *, prompt: Prompt
) -> ToolOpProposal:
    """Collect one round of corrections. Empty input KEEPS the current value.

    Empty-means-keep rather than empty-means-clear: a reviewer pressing enter
    through fields they have no opinion about must not thereby assert anything,
    and the restrictive default is the safer thing to leave in place.
    """
    effect = prompt(
        f"    effect [{proposal.effect.value}] (read/write, enter to keep): "
    ).strip().lower()
    updates: dict = {}
    if effect:
        updates["effect"] = effect

    # Ask about reversibility only where the base treats it as applicable.
    effective_effect = updates.get("effect", proposal.effect.value)
    if effective_effect == "write":
        reversible = prompt(
            f"    reversible [{proposal.reversible.value}] (y/n, enter to keep): "
        ).strip().lower()
        if reversible in ("y", "yes", "true"):
            updates["reversible"] = True
        elif reversible in ("n", "no", "false"):
            updates["reversible"] = False

    egress = prompt(
        f"    egress_arg [{proposal.egress_arg.value}] (argument name, '-' for "
        "none, enter to keep): "
    ).strip()
    if egress == "-":
        updates["clear_egress_arg"] = True
    elif egress:
        updates["egress_arg"] = egress

    try:
        return apply_corrections(proposal, **updates)
    except CorrectionRefused as exc:
        print(f"    REFUSED: {exc}")
        return proposal


def _review_server(
    server: DiscoveredServer,
    snapshot: McpServerSnapshot,
    *,
    prompt: Prompt,
    admit_all: bool,
) -> list[tuple[McpToolDef, ToolOp]]:
    """The batched per-server review — the one place human judgment enters.

    Lazy admission [ruling: maintainer, 2026-07-25]: every advertised tool is offered,
    a human admits what they choose, and admitting NONE is a first-class
    outcome (TL8). Nothing is admitted by default, because a default here is
    the whole control silently answering its own question.
    """
    print()
    print("=" * 78)
    print(
        f"REVIEW  server {snapshot.server_id!r} ({snapshot.transport}, "
        f"scope={server.scope.value}) — {len(snapshot.entries)} tool(s) advertised"
    )
    print(f"        source: {snapshot.source}")
    print("=" * 78)

    confirmed: list[tuple[McpToolDef, ToolOp]] = []
    for entry in snapshot.entries:
        proposal = propose_tool_op(entry.tool_def, transport=snapshot.transport)
        print()
        print(render_first_admission(entry.tool_def, proposal, url=server.url))
        if admit_all:
            confirmed.append((entry.tool_def, proposal.tool_op))
            print(f"  -> ADMIT {entry.tool_def.tool_name}")
            continue
        proposal, admitted = _confirm_tool(entry.tool_def, proposal, prompt=prompt)
        if admitted:
            confirmed.append((entry.tool_def, proposal.tool_op))
            print(f"  -> ADMIT {entry.tool_def.tool_name}")
        else:
            print(f"  -> skip  {entry.tool_def.tool_name}")

    print(
        f"\n  {snapshot.server_id}: {len(confirmed)} of {len(snapshot.entries)} "
        "tool(s) admitted"
    )
    _warn_about_held_tools(confirmed)
    return confirmed


def _warn_about_held_tools(confirmed: list[tuple[McpToolDef, ToolOp]]) -> None:
    """Say plainly which admitted tools are held on every call, and why.

    An irreversible external write is held for approval by the broker. Before `tegh approve`
    that was terminal on a laptop — there was no channel to release it, so these
    tools were admitted, advertised and permanently stuck — and this warning said
    they would never execute. `tegh approve` makes that false: they execute after
    an explicit per-call release, which is the friction working rather than a
    wall.

    It is still worth saying at wrap time. One release per call is real friction,
    and the wrap is where the human is present and the classification is still
    cheap to change. This is stated from tegh's OWN manifest data — it does not
    re-derive the PDP's rule table, which lives behind the base's boundary and is
    not tegh's to model.
    """
    held = sorted(
        op.op
        for _, op in confirmed
        if op.effect == "write" and op.reversible is False
    )
    if not held:
        return
    print(
        f"\n  !! {len(held)} admitted tool(s) are classified as IRREVERSIBLE WRITES: "
        f"{', '.join(held)}\n"
        "     The broker HOLDS every call to these; none executes until you "
        "release it with\n"
        "     `tegh approve <intent-id>` — once per call, not once for the tool. "
        "If any is\n"
        "     really a read, re-run `tegh wrap` and correct it with [e] — a "
        "missing\n"
        "     readOnlyHint is what proposed the write."
    )


def _admit(
    store: TeghStore,
    project: Path,
    server_id: str,
    tool_name: str,
    snapshot_path: Path,
) -> tuple[bool, str]:
    """Run propose (maker) then ratify (checker) for ONE tool.

    Two subprocesses under two DIFFERENT local identities, because
    maker≠checker is re-derived per coordinate and the base compares the
    identity strings. A single combined call does not exist here for the same
    reason `bulk-admit` does not exist in the base.
    """
    propose = _run_ceremony(
        [
            "admit-propose",
            "--server-id",
            server_id,
            "--tool-name",
            tool_name,
            "--from-snapshot",
            str(snapshot_path),
            "--ttl-hours",
            _PROPOSAL_TTL_HOURS,
        ],
        store.ceremony_env(role="maker", project=project),
    )
    if propose.returncode != 0:
        return False, (propose.stdout + propose.stderr).strip()

    proposal_id = ""
    for line in propose.stdout.splitlines():
        if "proposal_id=" in line:
            proposal_id = line.split("proposal_id=", 1)[1].split()[0].strip()
    if not proposal_id:
        return False, f"could not read a proposal_id from: {propose.stdout.strip()}"

    ratify = _run_ceremony(
        [
            "admit-ratify",
            "--server-id",
            server_id,
            "--tool-name",
            tool_name,
            "--proposal-id",
            proposal_id,
        ],
        store.ceremony_env(role="checker", project=project),
    )
    if ratify.returncode != 0:
        return False, (ratify.stdout + ratify.stderr).strip()
    return True, ""


def wrap_command(args: argparse.Namespace, *, prompt: Prompt = input) -> int:
    harness = HARNESS_ALIASES[args.harness]
    adapter = ADAPTERS[harness]
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())

    if not store.is_provisioned:
        print(
            f"REFUSED: no tegh home at {store.home}. Run `tegh init` first — a wrap "
            "writes admission rows and signed ledger records, which need this "
            "machine's HMAC and issuer keys.",
            file=sys.stderr,
        )
        return 2

    harness_home = Path(args.harness_home).expanduser() if args.harness_home else None
    result = adapter.discover(project, home=harness_home)
    print(_render_discovery(result))
    findings_text = _render_findings(result)
    if findings_text:
        print(findings_text)

    blocking = result.blocking_findings
    if blocking and not args.accept_gaps:
        print(
            "\nREFUSED: this wrap would be INCOMPLETE. The blocking findings above "
            "name\nsources tegh cannot enumerate or pin, so servers can stay "
            "reachable behind\nyour back. Close them (for claude.ai connectors: set "
            "`disableClaudeAiConnectors`\nin your Claude Code settings), or pass "
            "--accept-gaps to wrap anyway with the\ngaps on record.",
            file=sys.stderr,
        )
        return 2

    lockable = [server for server in result.effective if server.is_lockable]
    unlockable = [server for server in result.effective if not server.is_lockable]
    for server in unlockable:
        print(
            f"\n!! {server.server_id}: transport {server.transport!r} cannot be "
            "pinned by the lock format. It loads in the harness and is NOT wrapped."
        )
    if not lockable:
        print("\nNothing to wrap: no server this harness loads can be pinned.")
        return 1

    identity = f"local-solo:{store.issuer_key_id()}"

    # Which literal config values are credentials is a human's call, and
    # it is asked HERE — before the manifest, before the first ceremony
    # subprocess — because a wrap that will refuse must refuse before it burns
    # an admission. Reading the sites now also moves the format round-trip
    # refusal to the same early point.
    sites, gateway_site = adapter.config_sites(project, home=harness_home)
    try:
        decisions = _classify_config_values(
            store, project, sites, lockable, prompt=prompt, identity=identity
        )
    except interpose.InterposeError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 2
    if decisions is None:
        return 2

    # Key #1, first pass: connection config only. The tool namespace is empty
    # until a human confirms one — the ceremony must never be able to read a
    # namespace that discovery alone produced.
    manifest_path = store.manifest_path(project)
    _write_manifest(
        manifest_path,
        _synthesize_manifest(
            lockable,
            polarity=args.polarity,
            project=project,
            daily_cap=args.daily_cap,
            env_by_server=decisions.env_by_server,
            credentials_by_server=decisions.credentials_by_server,
        ),
    )

    snapshot_env = store.ceremony_env(role="maker", project=project)
    confirmed: dict[str, list[tuple[McpToolDef, ToolOp]]] = {}
    reachable: list[DiscoveredServer] = []
    for server in lockable:
        snapshot, error = _snapshot_server(store, project, server.server_id, snapshot_env)
        if snapshot is None:
            print(
                _render_snapshot_failure(
                    server,
                    error,
                    delivered=sorted(
                        {
                            *decisions.env_by_server.get(server.server_id, {}),
                            *decisions.credentials_by_server.get(server.server_id, {}),
                        }
                    ),
                )
            )
            continue
        reachable.append(server)
        confirmed[server.server_id] = _review_server(
            server, snapshot, prompt=prompt, admit_all=args.admit_all
        )

    if not reachable:
        print("\nNo server could be reached; nothing was admitted and no lock written.")
        return 1

    # Key #1, second pass: the CONFIRMED namespace and classifications.
    _write_manifest(
        manifest_path,
        _synthesize_manifest(
            reachable,
            polarity=args.polarity,
            project=project,
            daily_cap=args.daily_cap,
            tools_by_server=confirmed,
            env_by_server=decisions.env_by_server,
            credentials_by_server=decisions.credentials_by_server,
        ),
    )

    print("\n" + "=" * 78)
    print("ADMITTING (maker -> checker, one ceremony per tool)")
    print("=" * 78)
    admitted: dict[str, list[McpToolDef]] = {}
    for server in reachable:
        for tool_def, _op in confirmed[server.server_id]:
            ok, error = _admit(
                store,
                project,
                server.server_id,
                tool_def.tool_name,
                store.snapshot_path(project, server.server_id),
            )
            coordinate = f"{server.server_id}/{tool_def.tool_name}"
            if ok:
                print(f"  admitted  {coordinate}")
                admitted.setdefault(server.server_id, []).append(tool_def)
            else:
                print(f"  FAILED    {coordinate}: {error}")

    stamped = _now()
    servers: list[LockedServer] = []
    for server in reachable:
        try:
            locked = server.to_locked_server(harness)
        except UnrepresentableTransportError as exc:  # pragma: no cover — filtered above
            print(f"  !! {exc}")
            continue
        locked.admitted = [
            LockedTool(
                tool_def=tool_def,
                def_hash=compute_tool_def_hash(tool_def),
                tool_op=next(
                    op for definition, op in confirmed[server.server_id]
                    if definition.tool_name == tool_def.tool_name
                ),
                attestation=LockAttestation(
                    kind=AttestationKind.SOLO_ATTESTED,
                    admitted_by=identity,
                    admitted_at=stamped,
                ),
            )
            for tool_def in admitted.get(server.server_id, [])
        ]
        servers.append(locked)

    lock = TeghLock(
        format_version=LOCK_FORMAT_VERSION, generated_at=stamped, servers=servers
    )
    # TL2/TL3: sign with the tegh home's issuer key unless the operator has
    # deliberately asked for an unsigned lock. `--allow-unsigned` SUPPRESSES the
    # signer rather than merely tolerating a missing one, so the flag means the
    # same thing on a provisioned home as on one without a key — a flag whose
    # effect depends on unrelated local state is a flag nobody can reason about.
    signer = None if args.allow_unsigned else signer_from_pem(
        store.issuer_key_id(), store.issuer_signing_pem()
    )
    try:
        lock_path, signature_path = write_lock(
            lock, project=project, signer=signer, allow_unsigned=args.allow_unsigned
        )
    except UnsignedLockRefused as exc:  # pragma: no cover — unreachable above
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 2

    total = sum(len(server.admitted) for server in servers)
    print(f"\nwrote {lock_path} — {total} tool(s) pinned across {len(servers)} server(s)")
    if signature_path is None:
        print("  UNSIGNED (--allow-unsigned): this lock carries no evidence of origin.")
    else:
        print(f"  signed by {store.issuer_key_id()} -> {signature_path.name}")

    if total == 0:
        print(
            "\nNothing was admitted, so the config is left alone: interposing a "
            "gateway that serves no tools would remove the user's servers and "
            "give nothing back."
        )
        return 0

    if not _seed_grants(store, project):
        return 2
    return _interpose(
        store,
        project,
        harness,
        adapter,
        result,
        sites=sites,
        gateway_site=gateway_site,
        cleared_config=decisions.cleared,
        relocated=decisions.relocated,
        skip=args.no_rewrite,
    )


def _seed_grants(store: TeghStore, project: Path) -> bool:
    """Issue the floor grants for this project's admitted coordinates.

    The gateway advertises `served_registry()`, which is built from GRANTS —
    admission alone makes a tool callable, it does not make it served. Before
    this step a wrapped project got a gateway that advertised nothing and denied
    everything, which is how interposing the gateway turned out to be more than a config rewrite.

    The base's ceremony does the writing, as ever: `seed` creates each grant with
    a bootstrap-typed, issuer-signed ledger record beside it, and never
    overwrites an existing one (so re-wrapping is idempotent rather than a
    silent re-mint).
    """
    # `ceremony_env` is the ADMISSION ceremony's environment and deliberately
    # names no manifest — MCP admission does not read one. The grant ceremony
    # does: it derives the principal, the granted classes and the envelope hash
    # from the manifest the operator NAMED, and refuses to default it.
    env = store.ceremony_env(role="maker", project=project)
    env["BROKER_MANIFEST"] = str(store.manifest_path(project))
    seed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "safe_agents.broker.grants.commands", "seed"],
        env=env,
        capture_output=True,
        text=True,
    )
    if seed.returncode != 0:
        print(
            f"\nREFUSED: could not issue grants for this project's admitted tools:\n"
            f"{(seed.stdout + seed.stderr).strip()}",
            file=sys.stderr,
        )
        return False
    print(f"\ngrants issued for the admitted coordinates (store: {store.db_path})")
    return True


def _interpose(
    store: TeghStore,
    project: Path,
    harness: Harness,
    adapter,
    result: DiscoveryResult,
    *,
    sites: Sequence[interpose.ConfigSite],
    gateway_site: interpose.ConfigSite,
    cleared_config: Mapping[str, str],
    relocated: Mapping[str, str],
    skip: bool,
) -> int:
    """Point the harness at the gateway, and record what was displaced.

    The sites are passed in rather than re-resolved: they were already read to
    classify this wrap's literal config values, and resolving them twice
    would let the classification and the gate disagree about which files are in
    play.
    """
    if skip:
        print(
            "\n--no-rewrite: the harness config was NOT touched, so the wrapped "
            "agent still reaches its original servers directly. The lock records "
            "what WOULD be pinned; nothing is interposed."
        )
        return 0

    try:
        plan = interpose.plan_interposition(
            sites=sites,
            gateway_site=gateway_site,
            gateway_entry=adapter.gateway_entry(
                project, launcher=tegh_launcher(), home=store.home
            ),
            unwritable=adapter.unwritable_sources(result),
            cleared_config=cleared_config,
        )
    except interpose.InterposeError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 2

    backup = interpose.apply_interposition(
        plan,
        wrapped_at=_now(),
        project=project,
        harness=harness.value,
        relocated=relocated,
    )
    backup_path = store.backup_path(project)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(backup.to_json(), encoding="utf-8")

    print("\n" + "=" * 78)
    print("INTERPOSED")
    print("=" * 78)
    for label, names in sorted(plan.displaced.items()):
        print(f"  displaced {label}: {', '.join(names)}")
    if not plan.displaced:
        print("  (no servers were configured; the gateway is simply added)")
    print(f"  gateway   {plan.gateway_site.label} as {plan.gateway_name!r}")
    print(f"  backup    {backup_path}")
    # Named here because this is the moment it starts being written, and because
    # a payoff nothing points at is a payoff nobody finds: every brokered
    # call from now on lands on this tape.
    print(f"  audit     {store.audit_path(project)}")
    for source in plan.unwritable:
        print(
            f"\n  !! {source.scope.value}: {source.detail}\n"
            "     The gateway is NOT this harness's only MCP server while that holds."
        )
    print("\nSee what the broker records: tegh audit --verify --project " + str(project))
    print("Restore with: tegh unwrap --project " + str(project))
    return 0


# ---------------------------------------------------------------------------
# gateway / unwrap
# ---------------------------------------------------------------------------


def gateway_command(args: argparse.Namespace) -> int:
    """Run the broker's MCP mouth for one wrapped project.

    This is what the harness spawns. It is deliberately a THIN launcher: it
    resolves the tegh home, builds the environment, and execs the base's
    gateway. It does not import the broker's runtime, so the import boundary
    holds here exactly as it does in `wrap`, and the mouth stays the base's
    implementation rather than tegh's.

    **stdout belongs to the MCP protocol** (GATEWAY.md G8), so this function
    prints nothing on success; diagnostics go to stderr.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    if not store.is_provisioned:
        print(f"REFUSED: no tegh home at {store.home}. Run `tegh init`.", file=sys.stderr)
        return 2
    manifest = store.manifest_path(project)
    if not manifest.exists():
        print(
            f"REFUSED: {project} has not been wrapped — no manifest at {manifest}. "
            "Run `tegh wrap claude --project <path>` first.",
            file=sys.stderr,
        )
        return 2
    try:
        env = store.gateway_env(project=project)
    except TeghStoreError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    import os  # noqa: PLC0415 — only the exec path needs it

    os.execve(  # noqa: S606 — fixed argv, no shell
        sys.executable,
        [sys.executable, "-m", "safe_agents.broker.gateway"],
        env,
    )


def approve_command(args: argparse.Namespace) -> int:
    """Release one held call for a wrapped project.

    A THIN launcher, exactly like `gateway` above and for the same reason: the
    release is a broker operation on broker state, so the base owns it
    (`safe_agents.broker.approval.release_cli`) and tegh contributes the product
    surface. Reading, rendering, confirming and executing all happen in that one
    subprocess, because WYSIWYE is a claim about the bytes the human saw and
    splitting the render from the execution would put a store round-trip between
    them.

    It runs under `gateway_env` — deliberately the SAME environment the gateway
    serves under, not a similar one. A release that resolved a different store,
    tape or manifest would be a second writer of the thing it is releasing from;
    any divergence between these two environments is a bug, so there is one.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    if not store.is_provisioned:
        print(f"REFUSED: no tegh home at {store.home}. Run `tegh init`.", file=sys.stderr)
        return 2
    if not store.manifest_path(project).exists():
        print(
            f"REFUSED: {project} has not been wrapped — nothing has been held for "
            "it. Run `tegh wrap claude --project <path>` first.",
            file=sys.stderr,
        )
        return 2

    argv = ["-m", "safe_agents.broker.approval.release_cli", args.intent_id]
    if args.yes:
        argv.append("--yes")
    if args.json:
        argv.append("--json")
    # Not captured: the confirmation prompt needs this terminal's stdin, and the
    # render needs its stdout.
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, *argv],
        env=store.gateway_env(project=project),
        check=False,
    ).returncode


def audit_command(args: argparse.Namespace) -> int:
    """Show this project's audit tape, and optionally verify its chain.

    The tape is the product's payoff — "a fully compromised agent can still only
    ask", demonstrated rather than asserted — and until this existed `tegh`
    contained no occurrence of the word "audit" at all. `wrap` printed where the
    lock, the backup and the store went, and said nothing about the tape it had
    configured moments earlier, so a user had no way to find it.

    Prints the PATH even when it cannot read the file. That is the more common
    need: the question a user actually has is "where is it", and answering that
    does not depend on anything having been recorded yet.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    tape = store.audit_path(project)

    argv = ["-m", "safe_agents.broker.auditor.tape_cli", "--path", str(tape)]
    if args.verify:
        argv.append("--verify")
    if args.json:
        argv.append("--json")
    # No broker environment: the tape reader opens no store, manifest or
    # connector. Looking at what was recorded must not be able to change it.
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, *argv], check=False
    ).returncode


def unwrap_command(
    args: argparse.Namespace, *, prompt: Optional[Prompt] = None
) -> int:
    """Restore the harness config this project's wrap displaced.

    Says what it will change and asks first; `--yes` answers in advance. The
    plan, the order of the writes and the report are `tegh.unwrap`'s.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    try:
        plan = unwrap.build_plan(store, project)
    except interpose.InterposeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return unwrap.EXIT_REFUSED
    print(unwrap.render_plan(plan))
    if not args.yes and not unwrap.confirm(unwrap.QUESTION, prompt=prompt):
        return unwrap.EXIT_NOT_UNWRAPPED
    return unwrap.apply_plan(store, plan)


# ---------------------------------------------------------------------------
# status / diff
# ---------------------------------------------------------------------------


def _read_verified_lock(project: Path, store: TeghStore) -> Optional[LoadedLock]:
    """Read the lock, verifying its sidecar against this home's issuer key.

    The ONE read path for every command that consumes a lock, so `status` and
    `diff` cannot drift into different trust postures — a reader that verifies
    and a reader that doesn't, over the same artifact, is the second-arm gap
    in its most literal form.

    Raises on a signature that does not verify (`LockSignatureInvalid`) or one
    from an unknown signer (`UnknownLockSigner`); both are handled in `main`,
    where the exit codes live. Returns None when the project has no lock, which
    is a reportable state rather than an error.

    The absent-lock check comes FIRST, before the store is touched: whether a
    project is wrapped is a fact about the project, and answering it must not
    depend on what this machine's tegh home happens to hold. Resolving the
    verifier eagerly would make "not wrapped" fail differently on a home with no
    keys — a report that varies with unrelated local state.
    """
    lock_path, _ = lock_paths(project)
    if not lock_path.exists():
        return None
    return read_lock(
        project,
        verifier=resolve_local_verifier(store.issuer_key_id(), store.issuer_public_pem()),
    )


def _signature_line(loaded: LoadedLock) -> str:
    """One honest sentence about the lock's provenance.

    Three states, never two (see `LoadedLock.verified`): verified, present but
    unchecked, and absent. Collapsing the middle into either neighbour is how an
    unverified artifact starts reading as a verified one.
    """
    if not loaded.is_signed:
        return "ABSENT — this lock carries no evidence of origin"
    assert loaded.signature is not None
    signer = loaded.signature.key_id
    if loaded.verified:
        return f"present, VERIFIED against {signer}"
    return (
        f"present ({signer}), NOT VERIFIED — this tegh home holds no public key, "
        "so the signature has not been checked"
    )


def status_command(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    loaded = _read_verified_lock(project, store)
    if loaded is None:
        print(f"No tegh.lock in {project} — this project is not wrapped.")
        return 1

    lock = loaded.lock
    print(f"{loaded.path}  (format v{lock.format_version}, generated {lock.generated_at})")
    print(f"  signature: {_signature_line(loaded)}")
    for server in lock.servers:
        target = server.url or " ".join(filter(None, [server.command, *server.args]))
        print(
            f"\n  {server.server_id}  [{server.harness.value} / {server.scope} / "
            f"{server.transport}]  {target}"
        )
        if not server.admitted:
            # TL8: this is a deliberate refusal, not a gap. Saying so is the
            # whole reason the format distinguishes it from an absent server.
            print("    (discovered, NOTHING admitted — no tool from this server is callable)")
        for tool in server.admitted:
            op = tool.tool_op
            print(
                f"    {tool.tool_def.tool_name:<28} {op.effect:<5} "
                f"external={str(op.external).lower():<5} "
                f"reversible={str(op.reversible).lower():<5} "
                f"[{tool.attestation.kind.value} by {tool.attestation.admitted_by}]"
            )
    return 0


def posture_command(args: argparse.Namespace) -> int:
    """Report which floor properties hold in THIS configuration, with citations.

    Always exits 0 on a report it could produce: posture is a description, not a
    check, and today every configuration has open gaps by construction. An exit
    code that went red on "gaps exist" would be red always and would therefore
    mean nothing. Store refusals still surface as 2 through `main`.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    report = build_report(
        project,
        store,
        Harness.CLAUDE_CODE,
        lock_reader=_read_verified_lock,
        harness_home=Path(args.harness_home).expanduser() if args.harness_home else None,
    )
    print(json.dumps(to_dict(report), indent=2) if args.json else render(report))
    return 0


def diff_command(args: argparse.Namespace) -> int:
    """The lock's pinned definitions vs. what the servers advertise NOW.

    Read-only, and deliberately so: drift is COMPUTED at read time and never
    written back. This is where a poisoned description surfaces — rendered
    verbatim and in full, per TL11.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    loaded = _read_verified_lock(project, store)
    if loaded is None:
        print(f"No tegh.lock in {project} — this project is not wrapped.")
        return 1

    # Drift is judged against the PINNED definitions, so whether those bytes are
    # the ones a human signed is a precondition for reading the diff at all —
    # printed here rather than left to `status`, which a `diff` user has no
    # reason to have run.
    print(f"lock signature: {_signature_line(loaded)}\n")
    env = store.ceremony_env(role="maker", project=project)
    drifted = 0
    withdrawn = 0
    unchanged = 0
    new_tools = 0

    for server in loaded.lock.servers:
        snapshot, error = _snapshot_server(store, project, server.server_id, env)
        if snapshot is None:
            print(f"!! {server.server_id}: could not be reached — {error}")
            continue

        live = {entry.tool_def.tool_name: entry.tool_def for entry in snapshot.entries}
        pinned = {tool.tool_def.tool_name: tool for tool in server.admitted}
        deltas = []

        for name, tool in pinned.items():
            if name not in live:
                withdrawn += 1
                print(
                    f"\n  --- {server.server_id}/{name} — WITHDRAWN ---\n"
                    "  the server no longer advertises this admitted tool"
                )
                continue
            delta = compute_tool_delta(tool.tool_def, live[name], transport=server.transport)
            if delta.has_drift:
                drifted += 1
                deltas.append(delta)
                print()
                print(render_tool_delta(delta, url=server.url))
            else:
                unchanged += 1

        unadmitted = sorted(set(live) - set(pinned))
        new_tools += len(unadmitted)
        if unadmitted:
            print(
                f"\n  {server.server_id}: {len(unadmitted)} advertised tool(s) not "
                f"admitted (not callable): {', '.join(unadmitted)}"
            )

        acknowledgements = render_ack_requirements(deltas)
        if acknowledgements:
            print()
            print(acknowledgements)

    print(
        f"\nDRIFT={drifted} WITHDRAWN={withdrawn} UNCHANGED={unchanged} "
        f"UNADMITTED={new_tools}"
    )
    return 1 if (drifted or withdrawn) else 0


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tegh", description="Pin the MCP tools a coding project already uses."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_home(target: argparse.ArgumentParser) -> None:
        target.add_argument("--home", help="tegh home directory (default: $TEGH_HOME or ~/.tegh)")

    def with_project(target: argparse.ArgumentParser) -> None:
        target.add_argument("--project", default=".", help="project root (default: .)")

    init = sub.add_parser("init", help="mint this machine's tegh home")
    with_home(init)

    wrap = sub.add_parser("wrap", help="discover, review, admit, and write tegh.lock")
    wrap.add_argument(
        "harness", choices=sorted(HARNESS_ALIASES), help="which coding harness to wrap"
    )
    with_project(wrap)
    with_home(wrap)
    wrap.add_argument(
        "--polarity",
        choices=("abstain", "act"),
        default="abstain",
        help=(
            "the safe-default polarity for this wrap, recorded per project. "
            "'abstain' (refusing is the safe failure) fits an interactive coding "
            "agent and is the default HERE, in a consumer; it is deliberately never "
            "a base default. NOT YET ENFORCED: the value is validated and stored, "
            "and no decision reads it (wjatx/ptc-gal-reference#124) — `tegh posture` says so rather than "
            "letting the flag imply otherwise"
        ),
    )
    wrap.add_argument(
        "--accept-gaps",
        action="store_true",
        help="wrap even though blocking findings mean the wrap is incomplete",
    )
    wrap.add_argument(
        "--admit-all",
        action="store_true",
        help="admit every advertised tool without prompting (review still prints)",
    )
    wrap.add_argument(
        "--allow-unsigned",
        action="store_true",
        help=(
            "write tegh.lock with NO signature (TL3). The lock is signed by "
            "this tegh home's issuer key by default; use this only to produce a "
            "lock deliberately carrying no evidence of origin"
        ),
    )
    wrap.add_argument(
        "--daily-cap",
        type=int,
        default=200,
        help=(
            "per-op daily budget for the wrapped agent (default: 200). This is "
            "authority: it is the envelope cap every admitted coordinate is "
            "granted under, and the base refuses to default it for writes"
        ),
    )
    wrap.add_argument(
        "--harness-home",
        help=(
            "where this harness keeps its USER-level config (default: your home "
            "directory). Set this if you have relocated the harness's config "
            "tree; tegh does not infer the location from the harness's own "
            "environment variables, because their exact semantics are not "
            "documented and guessing them would rewrite the wrong file"
        ),
    )
    wrap.add_argument(
        "--no-rewrite",
        action="store_true",
        help=(
            "admit and write the lock, but do NOT point the harness at the "
            "gateway. Nothing is interposed and the agent keeps reaching its "
            "servers directly — useful to review a wrap before committing to it"
        ),
    )

    gateway = sub.add_parser(
        "gateway", help="run the broker's MCP gateway for a wrapped project"
    )
    with_project(gateway)
    with_home(gateway)

    call = sub.add_parser(
        "call",
        help="drive one tool call through the gateway and print the broker's answer",
        description=(
            "Drive ONE tools/call through this project's gateway as the wrapped "
            "principal, and print what the broker answered. The gateway is spawned "
            "with the command `tegh wrap` wrote into the harness config and the "
            "minimal environment a harness gives it. The call is recorded on the "
            "audit tape like any other."
        ),
        epilog=(
            "exit status: 0 the call executed; 1 the broker answered and the call "
            "did NOT execute (held for approval, denied, or the connector failed); "
            "2 tegh could not ask (no tegh home, project not wrapped, --args is not "
            "a JSON object, the gateway did not start or answer, or the timeout "
            "passed)."
        ),
    )
    call.add_argument(
        "tool",
        help=(
            "the tool name as the gateway advertises it: <server>__<tool>, for "
            "example memory__read_graph. A name the gateway never offered is "
            "still asked, which is how a deny is produced"
        ),
    )
    call.add_argument(
        "--args",
        default="{}",
        metavar="JSON",
        help="the call's arguments as one JSON object (default: {})",
    )
    call.add_argument(
        "--json",
        action="store_true",
        help="emit the answer as JSON: tool, executed, text",
    )
    call.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CALL_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "how long to wait for each reply from the gateway (default: "
            f"{DEFAULT_CALL_TIMEOUT_SECONDS:g}; a cold `npx` connector start is slow)"
        ),
    )
    with_project(call)
    with_home(call)

    approve = sub.add_parser(
        "approve", help="release a call the broker is holding for approval"
    )
    approve.add_argument("intent_id", help="the intent id the agent reported")
    approve.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    approve.add_argument(
        "--json", action="store_true", help="emit the outcome as JSON"
    )
    with_project(approve)
    with_home(approve)

    audit = sub.add_parser(
        "audit", help="show this project's audit tape, and where it lives"
    )
    audit.add_argument(
        "--verify", action="store_true", help="check the tape's hash chain"
    )
    audit.add_argument("--json", action="store_true", help="emit the tape as JSON")
    with_project(audit)
    with_home(audit)

    unwrap_parser = sub.add_parser(
        "unwrap",
        help=(
            "restore the harness config a wrap displaced, and move any relocated "
            "credential back into it"
        ),
    )
    unwrap_parser.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    with_project(unwrap_parser)
    with_home(unwrap_parser)

    status = sub.add_parser("status", help="report what this project's lock pins")
    with_project(status)
    with_home(status)

    diff = sub.add_parser("diff", help="the lock vs. what the servers advertise now")
    with_project(diff)
    with_home(diff)

    posture = sub.add_parser(
        "posture", help="what actually holds in this configuration, and what does not"
    )
    with_project(posture)
    with_home(posture)
    posture.add_argument("--json", action="store_true", help="emit the report as JSON")
    posture.add_argument(
        "--harness-home",
        help=(
            "where this harness keeps its USER-level config (default: your home "
            "directory). Must match what `wrap` was given, or posture reads a "
            "different config than the one that was wrapped and reports the "
            "wrong posture"
        ),
    )

    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "init":
            return init_command(args)
        if args.command == "wrap":
            return wrap_command(args)
        if args.command == "gateway":
            return gateway_command(args)
        if args.command == "call":
            # Imported here, not at module top: `tegh.call` imports the base's
            # gateway client from `safe_agents.broker.api`, which loads the whole
            # broker runtime. Every other subcommand, `gateway` above all, should
            # not pay for that or carry it.
            from tegh.call import call_command  # noqa: PLC0415

            return call_command(args)
        if args.command == "approve":
            return approve_command(args)
        if args.command == "audit":
            return audit_command(args)
        if args.command == "unwrap":
            return unwrap_command(args)
        if args.command == "status":
            return status_command(args)
        if args.command == "posture":
            return posture_command(args)
        return diff_command(args)
    except TeghStoreError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except LockSignatureInvalid as exc:
        # Exit 3, distinct from a store refusal (2) and from drift (1): a lock
        # whose signature fails is not a lock with a finding in it, it is bytes
        # of unknown origin, and a script must be able to tell those apart.
        print(f"TAMPER: {exc}", file=sys.stderr)
        return 3
    except UnknownLockSigner as exc:
        print(f"UNVERIFIABLE: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
