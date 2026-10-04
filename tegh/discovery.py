"""tegh discovery — the harness-agnostic half: result types and MCP entry parsing.

The FIRST half of `tegh wrap <harness>`. This module holds the machinery that
does NOT change between coding harnesses — the result/report vocabulary, the
`mcpServers` entry parser, and the shadowing resolver — and nothing that knows
where a particular harness keeps its config. **The per-harness readers live in
`tegh/harnesses/`, and the Claude Code adapter is descriptor #1**:
`from tegh.harnesses.claude_code import discover`.

The split is deliberate and load-bearing. There will be more harness adapters
(OpenClaw, OpenCode, Hermes, Cursor); a core that knows about `~/.claude.json`
is a core that acquires a Claude Code accent, and every later adapter then
inherits assumptions nobody stated. What survives here is what a *second*
adapter would otherwise have to restate.

**This module writes nothing** — discovery is a pure, side-effect-free read
that returns a value.

Three properties are load-bearing, and each is a security property rather than
a convenience:

1. **Scopes are shadowed, never merged.** When the same server NAME collides
   across scopes the highest-precedence entry wins *as a whole entry* — fields
   are never merged — but different names at different scopes ALL load side by
   side. Rewriting one scope leaves real servers reachable from the others, so
   the result carries both the effective set and the shadowed losers: a human
   wrapping a project needs to see that their project-scope server is being
   overridden. The RESOLUTION lives here (`_resolve_shadowing`); the LADDER —
   which scope beats which, and which scopes deduplicate by endpoint rather
   than by name — is a harness's own model and is passed in by its adapter.

2. **What cannot be enumerated is reported, loudly.** A scope a local read
   cannot list (a cloud-side connector set) and a scope that suppresses every
   other scope (an exclusive enterprise-managed file) both surface as
   first-class fields — `CoverageReport.claude_ai` and `CoverageReport.managed`
   — never footnotes. Silently omitting an unenumerable scope is the failure
   mode: it leaks straight past a local-only wrap.

3. **No secret ever enters the result (TL10).** Only the NAMES of `env` and
   `headers` keys are extracted. Values are never copied into a returned
   object, a finding's detail, or an error message — this code runs against
   real user configs holding real API keys. `${VAR}` / `${VAR:-default}`
   expansion is deliberately NOT performed: expanding could pull a live secret
   out of the process environment and into memory. Discovery records that a
   field carries an unexpanded reference and the referenced variable's NAME —
   never the `:-default` text, which can itself be a literal secret.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from tegh.lock import Harness, LockedServer

# ---------------------------------------------------------------------------
# Closed catalogs and constants
# ---------------------------------------------------------------------------

#: `type` values the harness accepts, normalized. `streamable-http` is a
#: documented alias for `http`.
_TRANSPORT_ALIASES = {"streamable-http": "http"}
_REMOTE_TRANSPORTS = frozenset({"http", "sse", "ws"})
HarnessTransport = Literal["stdio", "http", "sse", "ws"]

#: `${VAR}` / `${VAR:-default}`. Only group 1 (the NAME) is ever captured —
#: the default text is dropped unread, because it can be a literal secret.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


class ConfigScope(str, Enum):
    """Which harness config scope the winning server definition was read from.

    A CLOSED catalog of the load-side scopes a wrapped harness resolves an MCP
    server from. It lives HERE, in the harness-agnostic core, rather than in
    `lock.py`, because the committed format deliberately does not enumerate
    scopes: `tegh.lock` records `scope` as a plain string and the ADAPTER owns
    the closed set (TL9a). Core's own result types — findings, coverage,
    discovered servers, the shadowing ladder — are all keyed on this enum, so
    this is the one place it can live without core importing an adapter.

    **Known residual:** these members are still Claude Code's scope model, and
    adapter #1 is not evidence of the shape of the interface. Adapter #2 will
    either map its own scopes onto these names or force this catalog to
    generalize (most likely into a per-adapter scope catalog handed in the way
    `precedence` and `endpoint_keyed_scopes` already are). That generalization
    is deliberately NOT attempted on one adapter's evidence — but it is written
    down here so the next adapter's author meets it as a known open question
    rather than an assumption nobody stated.

    Scope is recorded rather than inferred because it is load-bearing twice
    over: unwrap must restore to the file it took from, and the same
    `server_id` appearing at a HIGHER-precedence scope shadows an admitted
    entry while every tool definition — and therefore every `def_hash` — stays
    byte-identical.
    """

    LOCAL = "local"
    PROJECT = "project"
    USER = "user"
    PLUGIN = "plugin"
    MANAGED = "managed"
    CLAUDE_AI = "claude-ai"


class ActivationState(str, Enum):
    """Whether a discovered server actually loads today.

    A CLOSED catalog. Listing a server is not the same as it being live: a
    project-scope (`.mcp.json`) server is *pending* until a human approves it,
    and any server can be sitting in a rejected or disabled list. Assuming
    everything listed is active would overstate the wrap's coverage in one
    direction and manufacture phantom servers in the other.
    """

    ACTIVE = "active"
    PENDING_APPROVAL = "pending-approval"
    REJECTED = "rejected"
    DISABLED = "disabled"


class FindingKind(str, Enum):
    """Conditions a human wrapping a project must see. A CLOSED catalog."""

    MANAGED_EXCLUSIVE = "managed-exclusive"
    CLAUDE_AI_NOT_ENUMERABLE = "claude-ai-not-enumerable"
    CLAUDE_AI_EVER_CONNECTED = "claude-ai-ever-connected"
    SHADOWED_BY_HIGHER_SCOPE = "shadowed-by-higher-scope"
    NAME_COLLIDES_BUT_BOTH_LOAD = "name-collides-but-both-load"
    INACTIVE_SHADOWER = "inactive-shadower"
    CLAUDE_AI_SETTING_CONFLICT = "claude-ai-setting-conflict"
    UNEXPANDED_ENV_REF = "unexpanded-env-ref"
    MALFORMED_ENTRY = "malformed-entry"
    RESERVED_SERVER_NAME = "reserved-server-name"
    UNREADABLE_CONFIG = "unreadable-config"
    NOT_ACTIVE = "not-active"


Severity = Literal["blocking", "warning", "info"]


class UnrepresentableTransportError(ValueError):
    """A discovered transport the frozen lock format cannot record.

    `tegh.lock` (TL9) mirrors `McpServerDecl`: `stdio` or `streamable-http` and
    nothing else. A harness `sse` or `ws` server is real and does load, so
    discovery reports it — but it cannot be pinned, and a wrap that silently
    dropped it would leave an unpinned server live behind a lock claiming
    completeness. Raising is the honest outcome: tegh cannot wrap what it
    cannot pin.
    """


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class DiscoveryFinding(BaseModel):
    """One condition worth a human's attention. Carries no secret values."""

    model_config = ConfigDict(extra="forbid")

    kind: FindingKind
    severity: Severity
    detail: str
    server_id: Optional[str] = None
    scope: Optional[ConfigScope] = None


class SkippedEntry(BaseModel):
    """A config entry the harness itself would not load — recorded, not dropped.

    An entry with `url` and no `type` is a documented load-time error (treated
    as a malformed stdio server and skipped) [claude-code.md §1 "Option 1"].
    Skipping it silently would make a broken-but-present server indistinguishable
    from an absent one, which is the same collapse TL8 forbids for admissions.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: str
    scope: ConfigScope
    reason: str
    source_path: str


class DiscoveredServer(BaseModel):
    """One server as discovered — the pre-admission shape of `LockedServer`.

    A discovery-time type rather than a `LockedServer` directly, for two
    reasons the conversion makes explicit:

    - **The vocabularies differ.** The harness accepts four transports
      (`stdio`/`http`/`sse`/`ws`); the frozen lock records two. Constructing a
      `LockedServer` in the reader would force the `sse`/`ws` decision at parse
      time, where the only available answers are "crash" or "drop".
    - **Discovery carries facts admission does not.** Activation state, the
      source file, the shadowing scope, and unexpanded-reference flags are
      inputs to the human's review; they are deliberately not in the committed
      lock. Widening `LockedServer` to hold them would put review scratch into
      a frozen public artifact.

    `to_locked_server()` is the conversion, and it produces `admitted=[]` —
    TL8's first-class "discovered, nothing admitted" state, which is the
    correct and meaningful output of a discovery pass. It takes the `harness`
    as an argument because this module is the harness-agnostic half and
    genuinely does not know which one it is reading for; the adapter supplies
    it (see `harnesses.claude_code.to_locked_servers`).
    """

    model_config = ConfigDict(extra="forbid")

    server_id: str
    scope: ConfigScope
    transport: HarnessTransport

    command: Optional[str] = None
    args: list[str] = []
    cwd: Optional[str] = None
    url: Optional[str] = None

    # TL10: NAMES ONLY, sorted. Never values, never a location convention.
    env_names: list[str] = []
    header_names: list[str] = []

    discovered_at: str

    activation: ActivationState
    activation_reason: Optional[str] = None

    #: Which file this definition was read from — unwrap must restore to the
    #: file it took from (TL9).
    source_path: str
    #: `<plugin>@<marketplace>`, plugin scope only.
    plugin_id: Optional[str] = None

    #: Field paths carrying an unexpanded `${VAR}` reference, and the referenced
    #: variable NAMES. Never the `:-default` text.
    unexpanded_fields: list[str] = []
    unexpanded_vars: list[str] = []

    #: Set on a shadowed entry: the scope of the entry that beat it.
    shadowed_by: Optional[ConfigScope] = None

    @property
    def is_lockable(self) -> bool:
        """Can the frozen lock format represent this server's transport?"""
        return self.transport in ("stdio", "http")

    def to_locked_server(self, harness: Harness) -> LockedServer:
        """Project onto the committed-record shape, with nothing admitted (TL8).

        `harness` is REQUIRED and has no default. This module is the
        harness-agnostic half: it cannot know which harness produced the config
        it just read, and a default would make one adapter's identity the value
        every other adapter silently inherits — mislabeling exactly the field
        TL9a made load-bearing. Callers should prefer their adapter's own
        projection helper, which supplies the identity and cannot get it wrong.

        `scope` crosses here as the enum's `.value`: the committed format
        records a plain string in the harness's own vocabulary (TL9a), and the
        `ConfigScope` catalog is core's internal model, not the format's.
        """
        if not self.is_lockable:
            raise UnrepresentableTransportError(
                f"server {self.server_id!r} uses transport {self.transport!r}, which "
                "tegh.lock cannot represent (the format records 'stdio' or "
                "'streamable-http' only); it loads in the harness but cannot be "
                "pinned, so it must be resolved before wrapping — not dropped"
            )
        return LockedServer(
            server_id=self.server_id,
            harness=harness,
            scope=self.scope.value,
            transport="stdio" if self.transport == "stdio" else "streamable-http",
            command=self.command,
            args=list(self.args),
            cwd=self.cwd,
            url=self.url,
            env_names=list(self.env_names),
            header_names=list(self.header_names),
            discovered_at=self.discovered_at,
            admitted=[],
        )


class ClaudeAiSignals(BaseModel):
    """Everything local disk knows about claude.ai connectors — which is not much.

    Connectors are a cloud-side configuration Claude Code fetches automatically
    for subscription-authenticated users, matched by endpoint URL rather than
    name; **no local file governs them** [claude-code.md §1 "Use MCP servers
    from claude.ai"]. `enumerable` is therefore permanently False. The two
    signals below are hints about whether the gap is likely to matter, never a
    listing: `ever_connected` is the top-level `claudeAiMcpEverConnected` flag,
    and `connectors_disabled` reports `disableClaudeAiConnectors` across every
    scope that sets it.

    **`connectors_disabled` fails toward "the gap is live"**
    [ruling: maintainer, 2026-07-25]. It resolves to True ONLY when at least one read
    source sets it and NO source contradicts them; a single `false`, or sources
    disagreeing, resolves False and (when they disagree) raises a
    `CLAUDE_AI_SETTING_CONFLICT` finding naming both sides. Nothing observed
    stays None, which gates identically to False — the distinction is kept
    because "nobody configured this" and "somebody turned it on" are different
    things to tell a user, not because they differ in safety.

    The alternative — resolving by precedence — was declined deliberately: this
    setting is one of two conditions deciding whether a local-only wrap is
    actually a wrap, Claude Code is closed-source, and its real precedence for
    this key is unverified. Guessing a vendor's precedence and then reporting
    the optimistic side of a disagreement would claim a closed gap tegh cannot
    establish is closed. tegh READS this setting; it does not enforce it, so it
    must not pretend to know which file wins. Under-claiming coverage is safe;
    over-claiming is the overclaim the posture ladder exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    enumerable: Literal[False] = False
    ever_connected: Optional[bool] = None
    #: The RESOLVED value under the fail-toward-live rule above — never a
    #: last-writer-wins pick.
    connectors_disabled: Optional[bool] = None
    #: Paths asserting `true` (the gap is closed) and `false` (it is open).
    #: Both are carried so a conflict is inspectable rather than merely counted:
    #: telling a user their setting disagrees is only actionable if tegh names
    #: which files to go look at.
    disabled_sources: list[str] = []
    enabled_sources: list[str] = []
    signal_sources: list[str] = []

    @property
    def sources_conflict(self) -> bool:
        """Do read sources disagree about whether connectors are disabled?"""
        return bool(self.disabled_sources) and bool(self.enabled_sources)


class ManagedControl(BaseModel):
    """`managed-mcp.json` state — exclusive control when present.

    If deployed, no other server of any scope or source loads at all, and
    `claude mcp add` fails hard with "enterprise MCP configuration is active and
    has exclusive control over MCP servers" [claude-code.md §1, §5]. That makes
    it a blocking precondition for a wrap, not a footnote: the file needs
    admin/root to write, which a `pip install tegh` flow cannot assume it has.
    """

    model_config = ConfigDict(extra="forbid")

    present: bool
    path: str
    exclusive: bool
    server_ids: list[str] = []


class ScopeCoverage(BaseModel):
    """Per-scope account of what was read, what was absent, and what was skipped."""

    model_config = ConfigDict(extra="forbid")

    scope: ConfigScope
    enumerable: bool
    sources_read: list[str] = []
    sources_absent: list[str] = []
    detail: Optional[str] = None


class CoverageReport(BaseModel):
    """What discovery covered — and, structurally, what it could not.

    Coverage is a field rather than prose because silence about coverage is the
    failure mode: a result that lists six servers and says nothing about the
    seventh source reads as complete.
    """

    model_config = ConfigDict(extra="forbid")

    scopes: list[ScopeCoverage] = []
    claude_ai: ClaudeAiSignals
    managed: ManagedControl

    @property
    def unenumerable_scopes(self) -> list[ConfigScope]:
        return [entry.scope for entry in self.scopes if not entry.enumerable]


class DiscoveryResult(BaseModel):
    """The whole discovery pass: what loads, what is shadowed, what was missed."""

    model_config = ConfigDict(extra="forbid")

    project_path: str
    discovered_at: str

    #: Servers that actually load, after shadowing and managed exclusivity.
    effective: list[DiscoveredServer] = []
    #: Losers of a precedence collision — informational, each with `shadowed_by`.
    shadowed: list[DiscoveredServer] = []
    #: Entries the harness itself would not load.
    skipped: list[SkippedEntry] = []

    coverage: CoverageReport
    findings: list[DiscoveryFinding] = []

    @property
    def blocking_findings(self) -> list[DiscoveryFinding]:
        return [f for f in self.findings if f.severity == "blocking"]

    def to_locked_servers(self, harness: Harness) -> list[LockedServer]:
        """Project the effective set onto `LockedServer`s with nothing admitted.

        `harness` is required for the reason `DiscoveredServer.to_locked_server`
        states: core does not know which harness it read for, and guessing would
        mislabel the field TL9a made the format's one closed catalog.

        Raises `UnrepresentableTransportError` on the first server the frozen
        format cannot record — see that exception's docstring for why dropping
        would be worse.
        """
        return [server.to_locked_server(harness) for server in self.effective]


# ---------------------------------------------------------------------------
# Readers — tolerant of absence, never of silence
# ---------------------------------------------------------------------------


class _JsonRead:
    """Outcome of reading one JSON file: absent, unreadable, or parsed."""

    __slots__ = ("present", "data", "error")

    def __init__(self, present: bool, data: Any, error: Optional[str]) -> None:
        self.present = present
        self.data = data
        self.error = error


def _read_json(path: Path) -> _JsonRead:
    """Read a JSON object. Absence is NOT an error; it is emptiness.

    The live `~/.claude.json` on a machine with no user-scope servers has no
    top-level `mcpServers` key at all, and a project with no `.mcp.json` has no
    file. Treating either as an error would make the common case loud and the
    dangerous cases (a corrupt file that silently drops a scope) quiet.

    A parse failure is reported WITHOUT the file's content: a truncated JSON
    document's error message can quote the surrounding bytes, and those bytes
    can be an API key.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _JsonRead(False, None, None)
    except (OSError, UnicodeDecodeError) as exc:
        return _JsonRead(True, None, f"unreadable ({type(exc).__name__})")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # exc.msg only — never exc.doc, and never the surrounding bytes.
        return _JsonRead(True, None, f"invalid JSON ({exc.msg} at line {exc.lineno})")
    if not isinstance(parsed, dict):
        return _JsonRead(True, None, "top-level JSON value is not an object")
    return _JsonRead(True, parsed, None)


def _string_set(source: Mapping[str, Any], keys: Iterable[str]) -> set[str]:
    """Union of several list-of-strings keys, tolerating absence and junk."""
    out: set[str] = set()
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            out.update(item for item in value if isinstance(item, str))
    return out


def _env_ref_names(value: str) -> list[str]:
    """Referenced variable NAMES in an unexpanded `${...}`; never the default."""
    return _ENV_REF.findall(value)


def _key_names(block: Any) -> list[str]:
    """TL10: the sorted NAMES of a mapping's keys. Values are never touched."""
    if not isinstance(block, dict):
        return []
    return sorted(str(key) for key in block)


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def _scan_unexpanded(
    entry: Mapping[str, Any], transport: str
) -> tuple[list[str], list[str]]:
    """Find `${VAR}` references without expanding them.

    Expansion is a documented harness feature and is deliberately NOT performed
    here: resolving `${API_KEY}` would pull a live secret out of the process
    environment into tegh's memory and, from there, into anything that
    serializes the result. What a reviewer needs is the *fact* that a field is
    parameterized and the variable's name — both of which are already public.
    """
    fields: list[str] = []
    names: set[str] = set()

    def visit(label: str, value: Any) -> None:
        if isinstance(value, str) and (found := _env_ref_names(value)):
            fields.append(label)
            names.update(found)

    visit("command", entry.get("command"))
    args = entry.get("args")
    if isinstance(args, list):
        for index, arg in enumerate(args):
            visit(f"args[{index}]", arg)
    if transport in _REMOTE_TRANSPORTS:
        visit("url", entry.get("url"))
    for block_name in ("env", "headers"):
        block = entry.get(block_name)
        if isinstance(block, dict):
            for key, value in block.items():
                visit(f"{block_name}.{key}", value)

    return sorted(set(fields)), sorted(names)


def _parse_entry(
    server_id: str,
    entry: Any,
    *,
    scope: ConfigScope,
    source_path: str,
    discovered_at: str,
    plugin_id: Optional[str],
    reserved_names: frozenset[str],
) -> tuple[Optional[DiscoveredServer], Optional[str]]:
    """Parse one `mcpServers` entry. Returns (server, skip_reason) — one is None.

    `reserved_names` is the calling adapter's list of names its harness refuses
    or silently skips. It is a required argument rather than a default so that
    a new adapter forgetting it fails loudly at the call site, instead of
    quietly admitting a name its harness would never load.
    """
    if not server_id:
        return None, "empty server name"
    if server_id in reserved_names:
        return None, f"reserved server name {server_id!r} — the harness refuses it"
    if not isinstance(entry, dict):
        return None, "entry is not a JSON object"

    declared = entry.get("type")
    if declared is not None and not isinstance(declared, str):
        return None, "'type' is not a string"
    transport = _TRANSPORT_ALIASES.get(declared or "", declared)

    if transport is None:
        # No `type`: stdio by default — UNLESS a `url` is present, which is a
        # documented load-time error (treated as a malformed stdio server and
        # skipped) [claude-code.md §1 "Option 1"].
        if entry.get("url") is not None:
            return None, "'url' with no 'type' — a documented load-time error"
        transport = "stdio"
    elif transport not in ("stdio", "http", "sse", "ws"):
        return None, f"unknown transport type {declared!r}"

    command = entry.get("command")
    url = entry.get("url")
    cwd = entry.get("cwd")

    if transport == "stdio":
        if not isinstance(command, str) or not command.strip():
            return None, "stdio server declares no 'command'"
        url = None
    else:
        if not isinstance(url, str) or not url.strip():
            return None, f"{transport} server declares no 'url'"
        command = None

    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []

    unexpanded_fields, unexpanded_vars = _scan_unexpanded(entry, transport)

    return (
        DiscoveredServer(
            server_id=server_id,
            scope=scope,
            transport=transport,  # type: ignore[arg-type]
            command=command,
            args=args,
            cwd=cwd if isinstance(cwd, str) else None,
            url=url,
            env_names=_key_names(entry.get("env")),
            header_names=_key_names(entry.get("headers")),
            discovered_at=discovered_at,
            activation=ActivationState.ACTIVE,  # refined by the caller
            source_path=source_path,
            plugin_id=plugin_id,
            unexpanded_fields=unexpanded_fields,
            unexpanded_vars=unexpanded_vars,
        ),
        None,
    )


def _parse_servers_block(
    block: Any,
    *,
    scope: ConfigScope,
    source_path: str,
    discovered_at: str,
    reserved_names: frozenset[str],
    plugin_id: Optional[str] = None,
) -> tuple[list[DiscoveredServer], list[SkippedEntry]]:
    """Parse an `mcpServers` mapping. A missing/odd block is empty, not an error."""
    if not isinstance(block, dict):
        return [], []
    servers: list[DiscoveredServer] = []
    skipped: list[SkippedEntry] = []
    for server_id in sorted(block):
        server, reason = _parse_entry(
            str(server_id),
            block[server_id],
            scope=scope,
            source_path=source_path,
            discovered_at=discovered_at,
            plugin_id=plugin_id,
            reserved_names=reserved_names,
        )
        if server is not None:
            servers.append(server)
        else:
            skipped.append(
                SkippedEntry(
                    server_id=str(server_id),
                    scope=scope,
                    reason=reason or "unparseable entry",
                    source_path=source_path,
                )
            )
    return servers, skipped


# ---------------------------------------------------------------------------
# Shadowing
# ---------------------------------------------------------------------------


def _endpoint_key(server: DiscoveredServer) -> tuple[str, ...]:
    """The identity plugins and connectors are deduplicated by.

    Plugins and claude.ai connectors match duplicates *by endpoint* (URL or
    command), not by name [claude-code.md §1 "Scope hierarchy and precedence"].
    """
    if server.url is not None:
        return ("url", server.url.strip().lower())
    return ("cmd", server.command or "", *server.args)


def _resolve_shadowing(
    candidates: Sequence[DiscoveredServer],
    *,
    precedence: Sequence[ConfigScope],
    endpoint_keyed_scopes: frozenset[ConfigScope],
) -> tuple[list[DiscoveredServer], list[DiscoveredServer], list[DiscoveryFinding]]:
    """Apply precedence. Returns (effective, shadowed, findings).

    Name-keyed for the file scopes; endpoint-keyed for plugin/connector entries,
    per the documented asymmetry. The asymmetry has a surprising consequence
    worth surfacing: a plugin server sharing a NAME with a higher-precedence
    server but pointing at a DIFFERENT endpoint is not a duplicate at all —
    both load. A wrap that assumed name-uniqueness would leave one of them live.

    Both the LADDER (`precedence`, highest first) and the set of scopes that
    deduplicate by endpoint are the *harness's* model, supplied by its adapter.
    Only the resolution is here: a scope order baked into this function would
    be a Claude Code assumption every later adapter silently inherited.
    """
    order = {scope: index for index, scope in enumerate(precedence)}
    ranked = sorted(
        candidates,
        key=lambda s: (order.get(s.scope, len(order)), s.server_id, s.plugin_id or ""),
    )

    effective: list[DiscoveredServer] = []
    shadowed: list[DiscoveredServer] = []
    findings: list[DiscoveryFinding] = []
    by_name: dict[str, DiscoveredServer] = {}
    by_endpoint: dict[tuple[str, ...], DiscoveredServer] = {}

    for server in ranked:
        endpoint_keyed = server.scope in endpoint_keyed_scopes
        winner = (
            by_endpoint.get(_endpoint_key(server))
            if endpoint_keyed
            else by_name.get(server.server_id)
        )
        if winner is not None:
            loser = server.model_copy(update={"shadowed_by": winner.scope})
            shadowed.append(loser)
            findings.append(
                DiscoveryFinding(
                    kind=FindingKind.SHADOWED_BY_HIGHER_SCOPE,
                    severity="warning",
                    server_id=server.server_id,
                    scope=server.scope,
                    detail=(
                        f"{server.scope.value}-scope server {server.server_id!r} is "
                        f"shadowed by the {winner.scope.value}-scope entry of the same "
                        f"{'endpoint' if endpoint_keyed else 'name'}; the whole entry "
                        "loses — fields are never merged across scopes"
                    ),
                )
            )
            if winner.activation is not ActivationState.ACTIVE:
                findings.append(
                    DiscoveryFinding(
                        kind=FindingKind.INACTIVE_SHADOWER,
                        severity="warning",
                        server_id=server.server_id,
                        scope=winner.scope,
                        detail=(
                            f"the shadowing {winner.scope.value}-scope entry is "
                            f"{winner.activation.value}, so it may not load; whether "
                            f"the {server.scope.value}-scope entry then takes over is "
                            "not documented — treat both as reachable when wrapping"
                        ),
                    )
                )
            continue

        if endpoint_keyed and server.server_id in by_name:
            findings.append(
                DiscoveryFinding(
                    kind=FindingKind.NAME_COLLIDES_BUT_BOTH_LOAD,
                    severity="warning",
                    server_id=server.server_id,
                    scope=server.scope,
                    detail=(
                        f"{server.scope.value}-scope server {server.server_id!r} shares "
                        "a name with a higher-precedence entry but a different "
                        "endpoint; plugins and connectors deduplicate by endpoint, not "
                        "name, so BOTH load"
                    ),
                )
            )

        effective.append(server)
        by_endpoint.setdefault(_endpoint_key(server), server)
        by_name.setdefault(server.server_id, server)

    return effective, shadowed, findings


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _entry_findings(
    effective: Sequence[DiscoveredServer],
    shadowed: Sequence[DiscoveredServer],
    skipped: Sequence[SkippedEntry],
) -> list[DiscoveryFinding]:
    """Per-entry findings: inactive servers, unexpanded references, skipped entries."""
    findings: list[DiscoveryFinding] = []
    for server in effective:
        if server.activation is not ActivationState.ACTIVE:
            reason = f": {server.activation_reason}" if server.activation_reason else ""
            findings.append(
                DiscoveryFinding(
                    kind=FindingKind.NOT_ACTIVE,
                    severity="info",
                    server_id=server.server_id,
                    scope=server.scope,
                    detail=f"{server.server_id!r} is {server.activation.value}{reason}",
                )
            )
    for server in [*effective, *shadowed]:
        if server.unexpanded_fields:
            findings.append(
                DiscoveryFinding(
                    kind=FindingKind.UNEXPANDED_ENV_REF,
                    severity="info",
                    server_id=server.server_id,
                    scope=server.scope,
                    detail=(
                        f"{server.server_id!r} carries unexpanded ${{...}} references in "
                        f"{', '.join(server.unexpanded_fields)} (variables: "
                        f"{', '.join(server.unexpanded_vars)}); tegh records the "
                        "variable names and deliberately does not expand them"
                    ),
                )
            )
    for entry in skipped:
        findings.append(
            DiscoveryFinding(
                kind=(
                    FindingKind.RESERVED_SERVER_NAME
                    if "reserved server name" in entry.reason
                    else FindingKind.MALFORMED_ENTRY
                ),
                severity="warning",
                server_id=entry.server_id,
                scope=entry.scope,
                detail=(
                    f"{entry.scope.value}-scope entry {entry.server_id!r} in "
                    f"{entry.source_path} was skipped: {entry.reason}"
                ),
            )
        )
    return findings
