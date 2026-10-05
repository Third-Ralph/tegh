"""The `tegh` command — pin the MCP tools a coding project already uses.

Ten subcommands, and a deliberate division of labour with the base:

  init     mint this machine's tegh home (HMAC key, local issuer signing key)
  wrap     discover -> snapshot -> review -> propose -> tegh.lock -> interpose -> admit
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
import re
import subprocess
import shlex
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Callable, Collection, Mapping, Optional, Sequence

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
)
from tegh import interpose
from tegh import unwrap
from tegh.harnesses import claude_code
from tegh.launch import (
    BROKER_INHERITED_ENV_VARS,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    gateway_home_in,
    inherited_env,
    python_module_argv,
    tegh_launcher,
)
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
from tegh.transaction import Stopped, WrapTransaction, signals_held, stops_like_ctrl_c

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

_CEREMONY = python_module_argv("safe_agents.broker.mcp.commands")
_PROPOSAL_TTL_HOURS = "1"

#: Answer for one tool in the batched review.
Prompt = Callable[[str], str]


class _InputEnded(Exception):
    """Input ended at one of a wrap's questions. Carries the question asked."""


def _ask(question: str) -> str:
    """`input`, with a CLOSED descriptor read as the end of input that it is.

    `tegh wrap <&-` starts Python with `sys.stdin` as None, and `input` then
    raises a RuntimeError where an empty pipe raises EOFError. Both are nobody
    answering.
    """
    if sys.stdin is None:
        raise EOFError
    return input(question)


def _answered_or_refused(prompt: Prompt) -> Prompt:
    """Make the end of input a refusal of the wrap, at every question it asks.

    No question a wrap asks has an answer that silence may stand for: each one
    either moves a credential or admits a tool. A prompt that resolved the end
    of input to its default would let a wrap whose answers ran out finish on
    decisions nobody made, and one that let the EOFError through is a
    traceback with a credential already moved. `wrap_command` turns this into
    the refusal, in one place, with what the wrap had written put back.
    """

    def ask(question: str) -> str:
        try:
            return prompt(question)
        except EOFError:
            raise _InputEnded(question.strip()) from None

    return ask


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


#: How a ceremony subprocess is started: an argv and an environment in, the
#: finished process out. A wrap passes its transaction's `run`, which keeps the
#: handle so that a stopped wrap stops the ceremony, and the server the
#: ceremony spawned, before it reports. `diff` writes nothing and passes `_run`.
Runner = Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess]


def _run(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        list(argv), env=dict(env), capture_output=True, text=True
    )


def _run_ceremony(
    run: Runner, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess:
    return run([*_CEREMONY, *args], env)


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
    named: Optional[Collection[tuple[str, str]]] = None,
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

    `named` limits the tool NAMESPACE to those `(server_id, tool_name)` pairs
    and leaves everything else (the classifications, the grant classes, the
    envelope they hash to) as confirmed. A tool the namespace does not name is
    uncallable whatever the store holds for it, which is what lets a wrap name
    each tool only once its admission is ratified (`_admit_committed`).
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
        callable_ = [
            {"tool_name": definition.tool_name}
            for definition, _ in admitted
            if named is None or (server.server_id, definition.tool_name) in named
        ]
        if callable_:
            declaration["tools"] = callable_
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
    """Replace the manifest whole, or not at all.

    This file decides which tools a call can reach and how each is classified,
    and a wrap rewrites it after its commit point, where a stop is not rolled
    back. Half of one could be a manifest in its own right, with a tool's
    classification cut short; so it is written beside the real one and renamed
    over it, keeping the permission bits the real one had.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tegh-tmp")
    try:
        tmp.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        if path.exists():
            tmp.chmod(path.stat().st_mode & 0o7777)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    rewrite: bool = True,
) -> ConfigValueDecisions:
    """Ask which literal config values are credentials. Raises to refuse the wrap.

    A refusal here is a `_WrapStopped` like any other, including one that comes
    after a credential has been written to the map: `wrap_command` puts the map
    back. `rewrite` is False under `--no-rewrite`, and only changes what is
    said about where a relocated credential ends up.

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
        try:
            block = interpose.read_block(site)
        except OSError as exc:
            raise interpose.InterposeError(
                unwrap.cannot_read(exc, needed_by="wrap")
            ) from exc
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
            # The end of input is not an answer here, in either direction: the
            # permissive side would classify a credential as config, and the
            # safe-looking side MOVES one, with nobody having decided anything.
            # `_answered_or_refused` refuses the wrap instead.
            answer = prompt(
                f"     is {field.name} a CREDENTIAL? [Y] yes, relocate it  "
                "[n] no, it is configuration: "
            ).strip().lower()
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
        raise _WrapStopped(configvalues.render_refusal(unrelocatable), then="")

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
                raise _WrapStopped(str(exc), then="") from exc
            for server_id, values in found.items():
                if (scope, server_id) not in constructed:
                    # A server this wrap does not construct (shadowed, or an
                    # unrepresentable transport) has nowhere to deliver to, and
                    # displacing it would put its literal in tegh's backup. So
                    # the wrap refuses, rather than relocating a credential no
                    # gateway will ever use.
                    raise _WrapStopped(
                        f"{scope}:{server_id} holds a value you classified as a "
                        "credential, but it is not a server this wrap constructs "
                        "(it is shadowed by another scope, or its transport "
                        "cannot be pinned), so tegh has nowhere to deliver the "
                        "credential",
                        then=(
                            "Remove that server entry, or replace the literal with "
                            "a ${VAR} reference, then run `tegh wrap` again."
                        ),
                    )
                credentials_by_server[server_id] = values
                store.write_secret_leaf(project, server_id, values)
                for name in values:
                    relocated[f"{scope}:{server_id}.env.{name}"] = server_id

        moved = sum(len(values) for values in credentials_by_server.values())
        print(
            f"\n{moved} credential value(s) moved into "
            f"{store.secrets_path(project)} (0600).\n"
            "They will be delivered to the server at spawn via "
            "connector_auth.env_map,\n"
            + (
                "and are removed from your harness config by the interposition "
                "below."
                if rewrite
                else "and STAY in your harness config as well, because "
                "--no-rewrite leaves it untouched."
            )
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
    run: Runner, store: TeghStore, project: Path, server_id: str, env: dict[str, str]
) -> tuple[Optional[McpServerSnapshot], str]:
    """Capture one server's live advertised set via the base's `snapshot`."""
    out = store.snapshot_path(project, server_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _run_ceremony(
        run,
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
                "— replace it\n      with the literal value, and the next `tegh wrap` "
                "will offer to relocate it."
            )
    # What to do next depends on how this wrap ends, which is not known yet: a
    # wrap that reaches no server is rolled back, and one that reaches another
    # commits and leaves a wrapped project. `_report_rolled_back` and
    # `_report_wrapped` each give the advice that works from where they end.
    lines.append("   Not wrapped.")
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
        "     really a read, run `tegh unwrap`, then `tegh wrap` again, and "
        "correct it with\n"
        "     [e] — a missing readOnlyHint is what proposed the write."
    )


@dataclass(frozen=True)
class _Proposal:
    """One tool's admission, proposed by the maker and not yet ratified."""

    server_id: str
    tool_name: str
    proposal_id: str

    @property
    def coordinate(self) -> str:
        return f"{self.server_id}/{self.tool_name}"

    @property
    def argv(self) -> list[str]:
        return [
            "--server-id", self.server_id,
            "--tool-name", self.tool_name,
            "--proposal-id", self.proposal_id,
        ]


def _propose(
    run: Runner, store: TeghStore, project: Path, server_id: str, tool_name: str
) -> tuple[Optional[_Proposal], str]:
    """The maker's half of one tool's admission: a PENDING proposal.

    It binds the definition in the snapshot the review was shown. It makes
    nothing callable: only the checker's ratification writes the row a call is
    checked against (`_ratify`). One left behind by a wrap that was killed
    authorises nothing and expires after `_PROPOSAL_TTL_HOURS`; a later wrap
    proposes again and ratifies its own proposal by id, so a stale one beside
    it changes nothing.
    """
    propose = _run_ceremony(
        run,
        [
            "admit-propose",
            "--server-id",
            server_id,
            "--tool-name",
            tool_name,
            "--from-snapshot",
            str(store.snapshot_path(project, server_id)),
            "--ttl-hours",
            _PROPOSAL_TTL_HOURS,
        ],
        store.ceremony_env(role="maker", project=project),
    )
    if propose.returncode != 0:
        return None, (propose.stdout + propose.stderr).strip()

    proposal_id = ""
    for line in propose.stdout.splitlines():
        if "proposal_id=" in line:
            proposal_id = line.split("proposal_id=", 1)[1].split()[0].strip()
    if not proposal_id:
        return None, f"could not read a proposal_id from: {propose.stdout.strip()}"
    return _Proposal(server_id, tool_name, proposal_id), ""


def _ratify(run: Runner, store: TeghStore, project: Path, proposal: _Proposal) -> str:
    """The checker's half: the admitted row. Returns what went wrong, or "".

    A second subprocess under a DIFFERENT local identity from `_propose`,
    because maker≠checker is re-derived per coordinate and the base compares
    the identity strings. A single combined call does not exist here for the
    same reason `bulk-admit` does not exist in the base.
    """
    ratify = _run_ceremony(
        run,
        ["admit-ratify", *proposal.argv],
        store.ceremony_env(role="checker", project=project),
    )
    return "" if ratify.returncode == 0 else (ratify.stdout + ratify.stderr).strip()


def _withdraw(
    run: Runner, store: TeghStore, project: Path, proposals: Sequence[_Proposal]
) -> int:
    """Burn proposals a stopped wrap will never ratify. Returns how many went.

    The base's `admit-reject`, which can only narrow: a rejected proposal can
    never be ratified. One that could not be burned is left to expire
    (`_PROPOSAL_TTL_HOURS`), and authorises nothing in the meantime.
    """
    withdrawn = 0
    for proposal in proposals:
        rejected = _run_ceremony(
            run,
            ["admit-reject", *proposal.argv],
            store.ceremony_env(role="checker", project=project),
        )
        withdrawn += rejected.returncode == 0
    return withdrawn



def _said_last(output: str) -> str:
    """The line of a ceremony's output that says what went wrong.

    A ceremony that refuses says so in one line that starts `REFUSED:` or
    `ERROR:`. One that crashes prints a Python traceback, whose last line is
    the error and whose other lines are the base's source. Either way it is
    one line, and it is the one a person can act on.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    named = [line for line in lines if line.startswith(("REFUSED:", "ERROR:"))]
    return (named or lines or ["it exited without saying why"])[-1]


def _ceremony_failed(what: str, output: str) -> _WrapStopped:
    """A ceremony that did not finish, as the one line a stopped wrap prints.

    The whole of what the child said goes to stdout first, with the rest of
    what this wrap printed, and the line names where to look: a traceback
    belongs in front of whoever will report it, not inside a sentence.
    """
    said = output.strip()
    if len(said.splitlines()) > 1:
        print(f"\n!! {what} failed. It said:")
        print("\n".join(f"     {line}" for line in said.splitlines()))
        seen = " (all it said is printed above)"
    else:
        seen = ""
    return _WrapStopped(
        f"{what} failed: {_said_last(said)}{seen}",
        then="Run `tegh wrap` again once what it reported is put right.",
        label="FAILED",
        status=1,
    )


def _already_wrapped(result: DiscoveryResult, project: Path) -> Optional[str]:
    """The refusal for a project whose config already runs tegh's gateway.

    None when it does not. A wrapped project's only MCP server IS the gateway,
    so a second wrap would snapshot it, be shown no tools, and replace the
    manifest and the signed lock of the first wrap with ones that admit none.

    Recognised by what the entry RUNS (`launch.gateway_home_in`, the reading of
    the command `gateway_entry` writes) and at any scope, shadowed or not. The
    name is not the test: `tegh` is only what a wrap happens to call it.

    The way out it names is `tegh unwrap`, with the tegh home the entry itself
    names, when that home holds the backup an unwrap restores from. When it
    does not, an unwrap would refuse for want of one, so the entry is named as
    something to remove by hand, and the servers as something to put back by
    hand: nothing else knows what they were.
    """
    for server in (*result.effective, *result.shadowed):
        if server.command is None:
            continue
        home = gateway_home_in([server.command, *server.args], project=project)
        if home is None:
            continue
        if TeghStore(home=Path(home)).backup_path(project).exists():
            way_out = (
                "restore the original servers with `tegh unwrap --project "
                f"{project} --home {home}`"
            )
        else:
            # Taking the entry out is not enough to wrap again: the servers
            # the first wrap displaced are in no file tegh can find, and a
            # wrap of a project with no servers has nothing to pin.
            way_out = (
                "remove that entry from the config by hand and put your own "
                f"servers back in its place (the tegh home it names, {home}, "
                "holds no wrap backup for this project, so `tegh unwrap` has "
                "nothing to restore them from)"
            )
        # `source_path` is a pointer into the file; a person wants the file.
        config_file = server.source_path.partition("#/")[0]
        return (
            f"{project} is already wrapped: its harness config "
            f"({server.scope.value} scope, {config_file}) runs tegh's gateway "
            f"for this project as the server {server.server_id!r}. Wrapping it again "
            "would review the gateway itself as one of your servers and replace "
            "tegh.lock, dropping the tools the first wrap pinned. Nothing was "
            f"changed. To change what is admitted, {way_out}, then run `tegh "
            "wrap` again."
        )
    return None


def _displaced_by_an_earlier_wrap(store: TeghStore, project: Path) -> Optional[str]:
    """The refusal for a project that has a wrap backup and no gateway entry.

    None when there is no backup. A backup with the gateway entry still in the
    config is `_already_wrapped`, checked first; this is what is left when the
    entry was taken out by hand, or when a wrap was cut off between writing
    its backup and rewriting the config. Either way the backup is the only
    record of the servers that wrap displaced, and the only thing that says
    which credentials in tegh's store are theirs. A wrap never writes over it:
    `tegh unwrap` restores from it and removes it, and then a wrap can run.
    """
    backup_path = store.backup_path(project)
    if not backup_path.exists():
        return None
    return (
        f"an earlier wrap of {project} left a backup at {backup_path}, and the "
        "harness config no longer runs tegh's gateway for this project (the "
        "entry was removed by hand, or that wrap was cut off before it "
        "finished). That backup is the only record of the servers the earlier "
        "wrap displaced, and wrapping now would replace it. Nothing was "
        "changed. Restore them first with `tegh unwrap --project "
        f"{_path(project)} --home {_path(store.home)}`, then run `tegh wrap` again."
    )


class _WrapStopped(Exception):
    """A wrap that cannot go on, in the words the person will read.

    Raised from anywhere between a wrap's first write and its last, and never
    reported where it is raised. Before the commit point `wrap_command` rolls
    the wrap back and says so, in one place, for every one of them; after it,
    `_admit_committed` reports how far the wrap got. `why` is what stopped it
    and `then` is what to do next.
    """

    def __init__(self, why: str, *, then: str, label: str = "REFUSED", status: int = 2):
        super().__init__(why)
        self.why = why
        self.then = then
        self.label = label
        self.status = status


@dataclass
class _WrapProgress:
    """How far a wrap got, for the report of whichever way it ended."""

    #: Servers that could not be asked for their tools, and so are not wrapped.
    unreachable: list[DiscoveredServer] = dc_field(default_factory=list)
    #: Every admission this wrap proposed, in the order it will ratify them.
    proposed: list[_Proposal] = dc_field(default_factory=list)
    #: Those ratified AND named in the manifest: what a call can reach.
    served: list[_Proposal] = dc_field(default_factory=list)
    #: How many unratified proposals a stopped wrap burned; None before it tried.
    withdrawn: Optional[int] = None
    #: The exit status of a wrap that was rolled back, set by its report.
    status: int = 1

    @property
    def waiting(self) -> list[_Proposal]:
        """Proposed and not served: not callable, whether or not ratified."""
        return [proposal for proposal in self.proposed if proposal not in self.served]


@dataclass(frozen=True)
class _PreparedWrap:
    """Everything a wrap decided, with nothing of it committed yet."""

    lock: TeghLock
    decisions: ConfigValueDecisions
    #: None under `--no-rewrite`: the harness config is deliberately left alone.
    plan: Optional[interpose.InterposePlan]
    #: The confirmed manifest, naming only the tools it is given.
    manifest_naming: Callable[[Collection[tuple[str, str]]], dict]


def _map_leaves(payload: object) -> object:
    """What a credential map holds, so two of them compare by content.

    A wrap creates an empty map for the snapshot whether or not it relocates
    anything, and taking that away again undoes no credential: a map that is
    absent and a map that is empty hold the same thing. One that does not parse
    compares by its bytes.
    """
    if not isinstance(payload, bytes):
        return {}
    try:
        return json.loads(payload)
    except ValueError:
        return payload


def _as_stopped(stopped: Optional[BaseException]) -> _WrapStopped:
    """Whatever ended a wrap, as the refusal or failure it is reported as."""
    if isinstance(stopped, _WrapStopped):
        return stopped
    if isinstance(stopped, _InputEnded):
        return _WrapStopped(
            "input ended before this wrap's questions were answered; the one "
            f"left open was `{stopped}`, and silence is not an answer to any of them",
            then=(
                "Run `tegh wrap` again from a terminal and answer each question. "
                "(--admit-all answers the per-tool questions in advance; only a "
                "person answers whether a value is a credential.)"
            ),
        )
    if isinstance(stopped, KeyboardInterrupt):
        # The status a shell gives a process a signal ended: 128 and its number.
        return _WrapStopped(
            "tegh wrap was stopped before it finished",
            then="Run `tegh wrap` again when you are ready.",
            label="INTERRUPTED",
            status=128 + stopped.number
            if isinstance(stopped, Stopped)
            else unwrap.EXIT_INTERRUPTED,
        )
    if isinstance(stopped, (interpose.InterposeError, TeghStoreError)):
        # Refusals written to be acted on; each already says what to do.
        return _WrapStopped(str(stopped), then="")
    # Not a refusal anybody wrote. The error's own text goes through, because
    # it is the only account of what happened that exists.
    described = f"{type(stopped).__name__}: {stopped}" if stopped else "no error"
    return _WrapStopped(
        f"tegh wrap stopped on an error it did not expect ({described})",
        then=(
            "Run `tegh wrap` again; if it stops the same way, that error is the "
            "thing to report."
        ),
        label="FAILED",
        status=1,
    )


def _path(path: Path | str) -> str:
    """A path as a shell takes it: quoted when it holds a space or the like."""
    return shlex.quote(str(path))


def _with_home(args: argparse.Namespace, store: TeghStore) -> str:
    """` --home <path>` for a printed command, when this one was given `--home`.

    A command tegh prints is followed word for word. One that left the flag
    off would look in `$TEGH_HOME` or `~/.tegh`, find no wrap there, and
    refuse.
    """
    return f" --home {_path(store.home)}" if args.home else ""


def _wrap_again(args: argparse.Namespace, store: TeghStore, project: Path) -> str:
    """This wrap as a command to run again, with every flag that says WHERE."""
    said = f"tegh wrap {args.harness} --project {_path(project)}"
    if args.harness_home:
        said += f" --harness-home {_path(args.harness_home)}"
    if args.no_rewrite:
        said += " --no-rewrite"
    return said + _with_home(args, store)


def _left_running(transaction: WrapTransaction) -> str:
    """What a stopped wrap could not stop, as a sentence; empty when it stopped all."""
    said = ""
    if transaction.not_stopped:
        pids = ", ".join(str(pid) for pid in transaction.not_stopped)
        said += (
            " tegh could not stop every process this wrap started, and these may "
            f"still be running: pid {pids}."
        )
    for failed in transaction.steps_failed:
        said += (
            " A step of putting things back raised an error of its own "
            f"({type(failed).__name__}: {failed})."
        )
    return said


def _say(line: str) -> None:
    """The one line a stopped wrap ends on. A terminal that has gone is not an error."""
    try:
        sys.stdout.flush()  # what the wrap printed stays ahead of why it stopped
        print(line, file=sys.stderr)
        sys.stderr.flush()
    except OSError:
        pass  # SIGHUP: there is nobody left to tell


def _withdrawn(progress: _WrapProgress) -> str:
    """What became of the proposals a stopped wrap never ratified, as a sentence."""
    waiting = len(progress.waiting)
    if not waiting:
        return ""
    left = waiting - (progress.withdrawn or 0)
    became = (
        "were withdrawn"
        if not left
        else f"expire within {_PROPOSAL_TTL_HOURS} hour(s) ({left} could not be withdrawn)"
    )
    return (
        f" The {waiting} admission proposal(s) it had made and not ratified "
        f"{became}; a proposal makes no tool callable."
    )


def _report_rolled_back(
    transaction: WrapTransaction,
    store: TeghStore,
    project: Path,
    progress: _WrapProgress,
    args: argparse.Namespace,
) -> None:
    """Say that a wrap stopped, why, and that what it wrote is put back.

    One line on stderr, whatever stopped it, printed as the last step of the
    rollback and inside its hold (`transaction.py`, "Signals"), so no signal
    can put another line in its place. A stopped wrap has usually moved a
    credential into tegh's store (the snapshot spawns the real server, which
    will not start without it) and rewritten the project's manifest with an
    empty tool namespace for the review, and may have got as far as the lock
    and the harness config. All of that was put back to the byte before this
    is printed, and the message names it.

    Nothing else needs putting back. A wrap stopped before its commit point
    has ratified no admission and issued no grant: what it wrote to tegh's
    store is proposals, which make nothing callable.

    A rollback that could not put every file back is FAILED whatever stopped
    the wrap, and says what is left and the command that deals with it.
    """
    stopped = _as_stopped(transaction.stopped_by)
    # One line: a refusal written as a paragraph is joined, and nothing else
    # about its spacing is touched (it may quote a question as it was asked).
    why = re.sub(r"\s*\n\s*", " ", stopped.why.strip()).rstrip(".")
    then = f" {stopped.then}" if stopped.then else ""
    secrets_path = store.secrets_path(project)
    running = _left_running(transaction)
    proposals = _withdrawn(progress)

    if transaction.not_restored:
        could_not = "; ".join(
            f"{path} ({reason})" for path, reason in transaction.not_restored
        )
        failed = {path for path, _ in transaction.not_restored}
        if secrets_path in failed:
            kept = (
                f" {secrets_path} may still hold a copy of the credential value(s) "
                "this wrap moved into it; remove them from that file by hand, or "
                "finish a wrap."
            )
        elif store.backup_path(project).exists():
            # A harness config would not go back, so the rest was left alone.
            kept = (
                " The wrap backup and the credential map were left as this wrap "
                "wrote them, because they are what restores that config: run "
                f"`tegh unwrap --project {_path(project)} --home {_path(store.home)}`."
            )
        else:
            kept = ""
        progress.status = 1
        _say(
            f"\nFAILED: {why}, and tegh could not put back every file this wrap "
            f"wrote: {could_not}.{kept}{proposals}{running}{then}"
        )
        return

    undone = ""
    if secrets_path in transaction.found_changed and _map_leaves(
        transaction.found_changed[secrets_path]
    ) != _map_leaves(transaction.before(secrets_path)):
        undone = (
            f" The credential value(s) this wrap had moved into {secrets_path} are "
            "undone: that file "
            + (
                "did not exist before this wrap and was removed again."
                if transaction.before(secrets_path) is None
                else "was put back, byte for byte, to what it held before this wrap."
            )
        )
    progress.status = stopped.status
    _say(
        f"\n{stopped.label}: {why}. Nothing was wrapped: your harness config, "
        "tegh.lock and its signature, and tegh's wrap backup, credential map, "
        "manifest, server snapshots and config-value decisions for this project "
        "are as they were before this command, and no tool was admitted."
        f"{undone}{proposals}{running}{then}"
    )


def _report_part_admitted(
    transaction: WrapTransaction,
    stopped_by: BaseException,
    prepared: _PreparedWrap,
    progress: _WrapProgress,
    args: argparse.Namespace,
    *,
    store: TeghStore,
    project: Path,
) -> int:
    """Say how far a wrap got that stopped AFTER its commit point.

    Nothing is put back from here: the admissions already ratified cannot be,
    and the files beside them are what makes them safe. So the line says what
    is true now. The project is wrapped. Each tool is either admitted and
    served or it is not callable, by name. And there is one way on, which
    works from every such state: unwrap, then wrap again. Under `--no-rewrite`
    there is nothing to unwrap, and it is the wrap alone.
    """
    stopped = _as_stopped(stopped_by)
    why = re.sub(r"\s*\n\s*", " ", stopped.why.strip()).rstrip(".")
    served = ", ".join(proposal.coordinate for proposal in progress.served) or "none"
    waiting = ", ".join(proposal.coordinate for proposal in progress.waiting) or "none"
    again = _wrap_again(args, store, project)
    if prepared.plan is None:
        state = (
            f"tegh.lock and tegh's own files for {project} are written and the "
            "harness config was left alone (--no-rewrite)"
        )
        finish = f"To finish, run `{again}`."
    else:
        state = f"{project} IS wrapped, and its harness config runs tegh's gateway"
        finish = (
            f"To undo it, run `tegh unwrap --project {_path(project)}"
            f"{_with_home(args, store)}`, which puts your servers back; to "
            f"finish, run that and then `{again}`."
        )
    _say(
        f"\n{stopped.label}: {why}, after the point where a wrap can still be put "
        f"back. {state}, but only {len(progress.served)} of {len(progress.proposed)} "
        f"tool(s) were admitted. Admitted and served: {served}. NOT admitted, and "
        f"refused if called: {waiting}. This wrap serves no tool you did not "
        "review, and the tegh.lock it wrote pins the definition of each one it serves."
        f"{_withdrawn(progress)}{_left_running(transaction)} {finish}"
    )
    return stopped.status


def wrap_command(args: argparse.Namespace, *, prompt: Prompt = _ask) -> int:
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

    # Before the findings are weighed and long before the review: no flag makes
    # a second wrap of a wrapped project mean anything but pinning the gateway,
    # and none makes replacing an earlier wrap's backup safe.
    refusal = _already_wrapped(result, project) or _displaced_by_an_earlier_wrap(
        store, project
    )
    if refusal is not None:
        print(f"\nREFUSED: {refusal}", file=sys.stderr)
        return 2

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

    # Everything above only read. From here a wrap writes, in two parts with a
    # COMMIT POINT between them.
    #
    # Before it, the wrap writes files and proposals. The `with` puts every
    # file back for whatever leaves it early (a refusal, the end of input, a
    # signal, a failed ceremony, an error nobody expected) and reports once;
    # no exit inside has a rollback or a report of its own. A proposal makes
    # nothing callable, so files are all there is to put back.
    #
    # After it (`_admit_committed`), the wrap writes what cannot be taken
    # back: the grants and the ratified admissions. Nothing is rolled back
    # from there. A wrap stopped there reports what is admitted and what is
    # not, and the files written before the commit point are what keep every
    # such state inside what was reviewed.
    lock_path, signature_path = lock_paths(project)
    transaction = WrapTransaction()
    # Each file a wrap writes, recorded as it is now. The harness configs are
    # the exception: `_write_wrap` records them at the moment it writes them.
    transaction.record(
        store.config_decisions_path(project),
        store.secrets_path(project),
        store.manifest_path(project),
        *(store.snapshot_path(project, server.server_id) for server in lockable),
        lock_path,
        signature_path,
        store.backup_path(project),
    )
    progress = _WrapProgress()

    def _withdraw_proposals() -> None:
        progress.withdrawn = _withdraw(transaction.run, store, project, progress.waiting)

    # Before the files go back, because a ceremony's environment creates the
    # credential map when there is none, and the rollback then removes it.
    transaction.on_rollback(_withdraw_proposals)
    transaction.after_rollback(
        lambda: _report_rolled_back(transaction, store, project, progress, args)
    )
    status = 0
    with stops_like_ctrl_c(), transaction:
        prepared = _prepare_wrap(
            transaction,
            progress,
            args,
            store=store,
            project=project,
            harness=harness,
            adapter=adapter,
            result=result,
            lockable=lockable,
            harness_home=harness_home,
            prompt=_answered_or_refused(prompt),
        )
        _write_wrap(transaction, prepared, args, store=store, project=project, harness=harness)
        status = _admit_committed(
            transaction, prepared, progress, args, store=store, project=project
        )
    return status if transaction.committed else progress.status


def _prepare_wrap(
    transaction: WrapTransaction,
    progress: _WrapProgress,
    args: argparse.Namespace,
    *,
    store: TeghStore,
    project: Path,
    harness: Harness,
    adapter,
    result: DiscoveryResult,
    lockable: Sequence[DiscoveredServer],
    harness_home: Optional[Path],
    prompt: Prompt,
) -> _PreparedWrap:
    """Classify, snapshot, review, propose each admission, and plan the rewrite.

    Returns only when every decision is made and every check has passed, with
    the lock and the harness config still unwritten and nothing admitted.
    Every other way out is a raise, which `wrap_command` turns into a rollback.
    """
    identity = f"local-solo:{store.issuer_key_id()}"

    # Which literal config values are credentials is a human's call, and
    # it is asked HERE — before the manifest, before the first ceremony
    # subprocess — because a wrap that will refuse must refuse before it makes
    # a proposal. Reading the sites now also moves the format round-trip
    # refusal to the same early point.
    sites, gateway_site = adapter.config_sites(project, home=harness_home)
    decisions = _classify_config_values(
        store,
        project,
        sites,
        lockable,
        prompt=prompt,
        identity=identity,
        rewrite=not args.no_rewrite,
    )

    # Key #1, first pass: connection config only. The tool namespace is
    # empty until a human confirms one — the ceremony must never be able to
    # read a namespace that discovery alone produced.
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

    confirmed: dict[str, list[tuple[McpToolDef, ToolOp]]] = {}
    reachable: list[DiscoveredServer] = []
    snapshot_env = store.ceremony_env(role="maker", project=project)
    for server in lockable:
        snapshot, error = _snapshot_server(
            transaction.run, store, project, server.server_id, snapshot_env
        )
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
            progress.unreachable.append(server)
            continue
        reachable.append(server)
        confirmed[server.server_id] = _review_server(
            server, snapshot, prompt=prompt, admit_all=args.admit_all
        )

    if not reachable:
        raise _WrapStopped(
            "no server could be reached, so there was nothing to review or admit",
            then="Fix what each server said above, then run `tegh wrap` again.",
            label="NOT WRAPPED",
            status=1,
        )
    if not any(confirmed.values()):
        # Asked before the first ceremony, so a wrap that admits nothing leaves
        # no proposal behind it either.
        raise _WrapStopped(
            "no tool was admitted, and a gateway that serves no tools would take "
            "your servers out of the harness config and give nothing back",
            then=(
                "Run `tegh wrap` again and answer [y] for each tool you want "
                "(--admit-all answers for all of them)."
            ),
            label="NOT WRAPPED",
            status=1,
        )

    # Key #1, second pass: the CONFIRMED namespace and classifications. Built
    # here and not written yet: `_write_wrap` and `_admit_committed` write it,
    # and say which tools it names at each write.
    def manifest_naming(named: Collection[tuple[str, str]]) -> dict:
        return _synthesize_manifest(
            reachable,
            polarity=args.polarity,
            project=project,
            daily_cap=args.daily_cap,
            tools_by_server=confirmed,
            env_by_server=decisions.env_by_server,
            credentials_by_server=decisions.credentials_by_server,
            named=named,
        )

    # The maker's half of every admission, all of them before any file of the
    # commit is written: a proposal the ceremony refuses is a wrap that would
    # pin less than was agreed to, and here it is still a clean rollback.
    print("\n" + "=" * 78)
    print("PROPOSING (maker; nothing is admitted until the checker ratifies)")
    print("=" * 78)
    for server in reachable:
        for tool_def, _op in confirmed[server.server_id]:
            coordinate = f"{server.server_id}/{tool_def.tool_name}"
            proposal, error = _propose(
                transaction.run, store, project, server.server_id, tool_def.tool_name
            )
            if proposal is None:
                raise _ceremony_failed(f"the admission proposal for {coordinate}", error)
            progress.proposed.append(proposal)
            print(f"  proposed  {coordinate}")

    stamped = _now()
    servers: list[LockedServer] = []
    for server in reachable:
        locked = server.to_locked_server(harness)
        locked.admitted = [
            LockedTool(
                tool_def=tool_def,
                def_hash=compute_tool_def_hash(tool_def),
                tool_op=tool_op,
                attestation=LockAttestation(
                    kind=AttestationKind.SOLO_ATTESTED,
                    admitted_by=identity,
                    admitted_at=stamped,
                ),
            )
            for tool_def, tool_op in confirmed[server.server_id]
        ]
        servers.append(locked)

    # The last thing that can refuse, and it only reads: the gate re-reads every
    # site and compares each literal with what was classified.
    plan = None
    if not args.no_rewrite:
        plan = interpose.plan_interposition(
            sites=sites,
            gateway_site=gateway_site,
            gateway_entry=adapter.gateway_entry(
                project, launcher=tegh_launcher(), home=store.home
            ),
            unwritable=adapter.unwritable_sources(result),
            cleared_config=decisions.cleared,
        )
    return _PreparedWrap(
        lock=TeghLock(
            format_version=LOCK_FORMAT_VERSION, generated_at=stamped, servers=servers
        ),
        decisions=decisions,
        plan=plan,
        manifest_naming=manifest_naming,
    )


def _write_wrap(
    transaction: WrapTransaction,
    prepared: _PreparedWrap,
    args: argparse.Namespace,
    *,
    store: TeghStore,
    project: Path,
    harness: Harness,
) -> None:
    """Write the files of a prepared wrap: manifest, lock, backup, config.

    In that order, with the harness config LAST, and all of it before the
    commit point: an error or a signal between two of these is rolled back
    like any other. What the order is for is the stop nothing can roll back,
    the process killed outright or the machine lost, and what each such stop
    leaves:

    - after the manifest: it carries the confirmed classifications and grant
      classes and names NO tool, as it has since the review began. A tool the
      manifest does not name is uncallable whatever the store holds, so no
      call for this project executes. tegh's other files are part-written (a
      relocated credential is in the credential map as well as the config),
      and the next `tegh wrap` starts over and overwrites them.
    - after the lock: the same, beside a lock that pins what was reviewed.
      Still no call executes; the next `tegh wrap` replaces the lock.
    - after the backup, with the config not yet or only partly rewritten: the
      backup holds every server definition as it was. The next `tegh wrap`
      refuses and names `tegh unwrap` (`_displaced_by_an_earlier_wrap`), which
      restores each site from the backup, puts the credentials back, and
      leaves a project a wrap can then run on.

    The backup going to disk before the first site is written is what makes
    the third case recoverable: the other order has an instant at which the
    servers are out of the config and recorded nowhere. And the lock going to
    disk before the commit point is what lets `_admit_committed` promise that
    no tool is ever callable at a definition the lock on disk does not pin.
    """
    _write_manifest(store.manifest_path(project), prepared.manifest_naming(()))

    # TL2/TL3: sign with the tegh home's issuer key unless the operator has
    # deliberately asked for an unsigned lock. `--allow-unsigned` SUPPRESSES the
    # signer rather than merely tolerating a missing one, so the flag means the
    # same thing on a provisioned home as on one without a key — a flag whose
    # effect depends on unrelated local state is a flag nobody can reason about.
    signer = None if args.allow_unsigned else signer_from_pem(
        store.issuer_key_id(), store.issuer_signing_pem()
    )
    write_lock(
        prepared.lock, project=project, signer=signer, allow_unsigned=args.allow_unsigned
    )
    plan = prepared.plan
    if plan is None:
        return

    backup = interpose.backup_of(
        plan,
        wrapped_at=_now(),
        project=project,
        harness=harness.value,
        relocated=prepared.decisions.relocated,
    )
    backup_path = store.backup_path(project)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(backup.to_json(), encoding="utf-8")

    # Recorded NOW and not when the wrap began: the harness writes its own
    # state into these files while a wrap waits at its questions, and a
    # rollback must undo this write and nothing of the harness's. Put back
    # before anything else, because the credentials they held are otherwise
    # only in the credential map this same rollback empties (`transaction.py`).
    transaction.record(
        *dict.fromkeys(site.path for site in plan.sites), restore_first=True
    )
    interpose.write_interposition(plan)


def _report_wrapped(
    prepared: _PreparedWrap,
    progress: _WrapProgress,
    args: argparse.Namespace,
    *,
    store: TeghStore,
    project: Path,
) -> None:
    """Say what a committed wrap wrote, and what it left for the person to do."""
    lock_path, signature_path = lock_paths(project)
    servers = prepared.lock.servers
    total = sum(len(server.admitted) for server in servers)
    print(f"\nwrote {lock_path} — {total} tool(s) pinned across {len(servers)} server(s)")
    if args.allow_unsigned:
        print("  UNSIGNED (--allow-unsigned): this lock carries no evidence of origin.")
    else:
        print(f"  signed by {store.issuer_key_id()} -> {signature_path.name}")

    plan = prepared.plan
    unreachable = ", ".join(server.server_id for server in progress.unreachable)
    if plan is None:
        print(
            "\n--no-rewrite: the harness config was NOT touched, so the wrapped "
            "agent still reaches its original servers directly. The lock records "
            "what WOULD be pinned; nothing is interposed."
        )
        moved = sum(
            len(values) for values in prepared.decisions.credentials_by_server.values()
        )
        if moved:
            print(
                f"\n  !! {moved} credential value(s) are now in TWO places: your "
                "harness config, where --no-rewrite left them,\n     and "
                f"{store.secrets_path(project)},\n     where this wrap copied "
                "them. A wrap without --no-rewrite takes them out of the config."
            )
        if unreachable:
            print(
                f"\n  !! not wrapped, because it could not be reached: {unreachable}\n"
                "     Fix it, then run `tegh wrap` again."
            )
        return

    backup_path = store.backup_path(project)
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
    if unreachable:
        # The project is wrapped now, so "run `tegh wrap` again" would only be
        # refused. The way back to a wrap goes through an unwrap.
        print(
            f"\n  !! not wrapped, because it could not be reached: {unreachable}\n"
            "     It was displaced with the rest, so the gateway does not serve it "
            "and the harness no\n     longer loads it. To wrap it: run `tegh "
            f"unwrap --project {_path(project)}`,\n     fix the server, then run "
            "`tegh wrap` again."
        )
    print(
        f"\nSee what the broker records: tegh audit --verify --project {_path(project)}"
    )
    print(f"Restore with: tegh unwrap --project {_path(project)}")


def _seed_grants(transaction: WrapTransaction, store: TeghStore, project: Path) -> None:
    """Issue the floor grants for this project's confirmed action classes.

    The gateway advertises `served_registry()`, which is built from GRANTS —
    admission alone makes a tool callable, it does not make it served. Before
    this step a wrapped project got a gateway that advertised nothing and denied
    everything, which is how interposing the gateway turned out to be more than a config rewrite.

    The base's ceremony does the writing, as ever: `seed` creates each grant with
    a bootstrap-typed, issuer-signed ledger record beside it, and never
    overwrites an existing one (so re-wrapping is idempotent rather than a
    silent re-mint).

    A grant is not a file and nothing takes one back, so this runs after the
    commit point. It is safe there at every instant: a grant is for one action
    class under one envelope hash, both read from the manifest on disk, which
    is already the confirmed one; and a granted tool the manifest does not
    name yet is still uncallable.
    """
    # `ceremony_env` is the ADMISSION ceremony's environment and deliberately
    # names no manifest — MCP admission does not read one. The grant ceremony
    # does: it derives the principal, the granted classes and the envelope hash
    # from the manifest the operator NAMED, and refuses to default it.
    env = store.ceremony_env(role="maker", project=project)
    env["BROKER_MANIFEST"] = str(store.manifest_path(project))
    seed = transaction.run(
        python_module_argv("safe_agents.broker.grants.commands", "seed"), env
    )
    if seed.returncode != 0:
        raise _ceremony_failed(
            "issuing grants for this project's admitted tools", seed.stdout + seed.stderr
        )
    print(f"  grants issued for the admitted coordinates (store: {store.db_path})")


def _admit_committed(
    transaction: WrapTransaction,
    prepared: _PreparedWrap,
    progress: _WrapProgress,
    args: argparse.Namespace,
    *,
    store: TeghStore,
    project: Path,
) -> int:
    """The commit point, and everything after it. Returns the exit status.

    What is left is what cannot be taken back: the grants, and the checker's
    ratification of each proposal, which writes the row a call is checked
    against. So nothing here is rolled back, and the ORDER has to make every
    place this can stop a safe one, including the stops no handler sees
    (SIGKILL, a power cut). A call executes only when the manifest names the
    tool, the store's admitted row matches what the server advertises now,
    and a grant covers it. The order uses the first of those as the switch:

    1. the grants, while the manifest names no tool;
    2. for each tool, the ratification, and only THEN the manifest rewritten
       to name that tool as well.

    A stop anywhere leaves each tool either not named, and uncallable, or
    named with its row at the definition that was reviewed, which is the one
    the lock already on disk pins and under the classification that was
    confirmed. Naming a tool before its ratification would be wrong whenever
    the store still holds an EARLIER admission of it: the server could
    advertise that earlier definition again and be served under the new
    classification. Ratifying under the manifest the wrap found would be wrong
    the other way round.

    A stop signal does not raise here. It is held (`transaction.py`,
    "Signals") and looked at before each step, so a ceremony is never cut in
    half, and the report below is printed inside the same hold.
    """
    manifest_path = store.manifest_path(project)
    with signals_held(deliver=False) as arrived:
        transaction.commit()
        stopped_by: Optional[BaseException] = None
        try:
            print("\n" + "=" * 78)
            print("ADMITTING (checker ratifies, one tool at a time)")
            print("=" * 78)
            _seed_grants(transaction, store, project)
            for proposal in list(progress.proposed):
                if arrived:
                    raise Stopped(arrived[0])
                error = _ratify(transaction.run, store, project, proposal)
                if error:
                    raise _ceremony_failed(
                        f"the admission ceremony for {proposal.coordinate}", error
                    )
                named = [*progress.served, proposal]
                _write_manifest(
                    manifest_path,
                    prepared.manifest_naming(
                        {(served.server_id, served.tool_name) for served in named}
                    ),
                )
                progress.served.append(proposal)
                print(f"  admitted  {proposal.coordinate}")
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - reported below
            stopped_by = exc
        if stopped_by is None:
            try:
                _report_wrapped(prepared, progress, args, store=store, project=project)
            except OSError:
                pass  # the wrap is done; only the terminal it was telling has gone
            return 0
        try:
            transaction.stop_children()
            progress.withdrawn = _withdraw(transaction.run, store, project, progress.waiting)
        except Exception as failed:  # noqa: BLE001 - tidying; the report still has to be made
            transaction.steps_failed.append(failed)
        return _report_part_admitted(
            transaction, stopped_by, prepared, progress, args, store=store, project=project
        )


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

    argv = python_module_argv("safe_agents.broker.gateway")
    os.execve(argv[0], argv, env)  # noqa: S606 — fixed argv, no shell


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

    argv = python_module_argv("safe_agents.broker.approval.release_cli", args.intent_id)
    if args.yes:
        argv.append("--yes")
    if args.json:
        argv.append("--json")
    # Not captured: the confirmation prompt needs this terminal's stdin, and the
    # render needs its stdout.
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
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

    argv = python_module_argv("safe_agents.broker.auditor.tape_cli", "--path", str(tape))
    if args.verify:
        argv.append("--verify")
    if args.json:
        argv.append("--json")
    # No broker environment: the tape reader opens no store, manifest or
    # connector. Looking at what was recorded must not be able to change it.
    # What it does get is the machine's settings by name, as every other child
    # does, and nothing else from the shell: it reads the one file it is given
    # and no variable.
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv, env=inherited_env(BROKER_INHERITED_ENV_VARS), check=False
    ).returncode


def unwrap_command(
    args: argparse.Namespace, *, prompt: Optional[Prompt] = None
) -> int:
    """Restore the harness config this project's wrap displaced.

    Says what it will change and asks first; `--yes` answers in advance. The
    plan, the refusals, the order of the writes and the report are all
    `tegh.unwrap`'s.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    return unwrap.run(store, project, yes=args.yes, prompt=prompt)


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
        snapshot, error = _snapshot_server(_run, store, project, server.server_id, env)
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
        description=(
            "Take the tegh gateway entry out of the harness config, put back "
            "the servers the wrap displaced, and move any relocated credential "
            "from tegh's store back into that config. Servers added since the "
            "wrap are kept. The plan is printed first and nothing changes "
            "until it is agreed to. Quit the coding agent in this project "
            "first: tegh does not check for a running session."
        ),
    )
    unwrap_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "agree to the plan in advance. Without it the prompt needs a "
            "terminal on both stdin and stdout, and is refused otherwise"
        ),
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
