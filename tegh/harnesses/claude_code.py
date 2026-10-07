"""Claude Code discovery adapter — descriptor #1 for `tegh wrap claude`.

Everything that knows where Claude Code keeps MCP configuration: the
`~/.claude.json` structure, the `.mcp.json` project scope, the settings files
carrying approval and enablement, the plugin manifest, the enterprise
`managed-mcp.json`, and the claude.ai connector signals. The harness-agnostic
machinery it stands on — the result types, the `mcpServers` entry parser, and
the shadowing resolver — is `tegh/discovery.py`.

This is the FIRST half of `tegh wrap claude`. It is a pure, side-effect-free
read of every config scope Claude Code resolves MCP servers from, producing the
`LockedServer`-shaped record `tegh.lock` needs (TL8/TL9/TL10) plus an explicit
account of what discovery could NOT see.

**This module writes nothing.** The rewrite half — replacing real servers with
tegh's gateway — is a separate slice, deliberately not built yet: `claude mcp
add` does not validate the endpoint it is pointed at
[`docs/references/harnesses/claude-code.md` §7], so a rewrite landing before the
gateway is live leaves the user with a `failed` server and no diagnostic.

Two of the three load-bearing properties stated in `discovery.py` take their
concrete Claude Code form here:

1. **Scopes are shadowed, never merged** (§1 "Scope hierarchy and precedence").
   `SCOPE_PRECEDENCE` below is Claude Code's ladder — the *order* is this
   harness's model, handed to the generic resolver rather than baked into it.

2. **What cannot be enumerated is reported, loudly.** claude.ai connectors are
   cloud-side with no local file; discovery cannot list them, and silently
   omitting them is the failure mode — they leak straight past a local-only
   wrap. `managed-mcp.json`, when present, has EXCLUSIVE control: nothing else
   loads and `claude mcp add` fails outright. Both surface as first-class
   fields, never footnotes.

The authority for WHERE to look is `docs/references/harnesses/claude-code.md`
§1, corrected in three places by a live read of one developer machine's `~/.claude.json`
on 2026-07-25: there is no top-level `mcpServers` key when the user has no
user-scope servers (absence means empty, never an error); the per-project
approval/enablement keys live in `~/.claude.json` under the project entry, not
only in `settings.json` as the reference implies; and both a legacy
(`*McprcServers`) and a current (`*McpjsonServers`) vocabulary are present, so
both are tolerated.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from tegh.discovery import (
    ActivationState,
    ClaudeAiSignals,
    ConfigScope,
    CoverageReport,
    DiscoveredServer,
    DiscoveryFinding,
    DiscoveryResult,
    FindingKind,
    ManagedControl,
    ScopeCoverage,
    SkippedEntry,
    _JsonRead,
    _entry_findings,
    _parse_servers_block,
    _read_json,
    _resolve_shadowing,
    _string_set,
)
from tegh.interpose import ConfigSite, UnwritableSource
from tegh.launch import gateway_argv
from tegh.lock import Harness, LockedServer
from tegh.posture import PostureLine

# ---------------------------------------------------------------------------
# Closed catalogs and constants
# ---------------------------------------------------------------------------

#: Can a wrapped Claude Code agent undo its own wrap? (`tegh posture`.)
#:
#: Deliberately weaker than the summary of this that was in use before
#: 2026-07-26, which said "structural, unbypassed except bypassPermissions". Checking that
#: against the reference found three problems, and the third is why this
#: line says `unknown` rather than `partial`:
#:
#: 1. There are FIVE documented protected-path outcomes, not two — `dontAsk`
#:    DENIES (stronger than a prompt), `auto`/`plan` route to a classifier.
#: 2. An escape lives inside the DEFAULT mode: a prompted write offers a
#:    session-scoped "allow Claude to edit its own settings for this session"
#:    opt-in, so one persuaded prompt — the thing an injected agent is
#:    optimized to obtain — buys the rest of the session.
#: 3. The protected-path table names an OUTCOME and never names which TOOLS it
#:    covers, while the same doc states that `claude mcp remove` and direct
#:    file edits from a shell are "not gated by Claude Code's own permission
#:    system at all". That ungated path is documented as TEGH'S OWN installer
#:    lever — and the wrapped agent has a Bash tool. Whether the agent can walk
#:    the same path is not addressed anywhere.
#:
#: Point 3 may make this the hole other harnesses have between a gated
#: file-write tool and an ungated terminal tool, with better documentation. Resolving it needs a behavioural test, not another
#: doc read; see docs/posture-ladder.md on the coverage artifact.
DURABILITY = PostureLine(
    claim="Whether a wrapped Claude Code agent can undo its own wrap is UNRESOLVED",
    holds="unknown",
    source="docs/references/harnesses/claude-code.md:755-803, 860-874 "
    "(the vendor's permission-modes page for the first range; the second records a "
    "statement with no vendor source)",
    detail=".mcp.json and .claude.json are hard-coded protected paths evaluated BEFORE "
    "permission rules, so permissions.allow cannot pre-approve an in-session write — but "
    "the outcome is a PROMPT, with a session-scoped 'allow Claude to edit its own "
    "settings for this session' opt-in inside the default mode, and no prompt at all "
    "under bypassPermissions. Crucially the docs never say which TOOLS the gate covers, "
    "while the reference records, with no vendor source, that `claude mcp remove` from a "
    "shell is ungated — the agent has a shell. All of this is a documentation claim: no command was run against a running "
    "binary when the documentation was read [claude-code.md:963-967]",
)

#: Which member of the format's one closed catalog this adapter speaks for
#: (TL9a). The identity is the ADAPTER's to state: core reads config files and
#: cannot know whose they are, and `tegh.lock` records the harness precisely so
#: a lock written for one harness is never replayed against another.
HARNESS: Harness = Harness.CLAUDE_CODE

#: Scope precedence, highest first [claude-code.md §1 "Scope hierarchy and
#: precedence"]. `managed` is deliberately absent: it is not a step on this
#: ladder but an exclusive mode that replaces the whole ladder.
SCOPE_PRECEDENCE: tuple[ConfigScope, ...] = (
    ConfigScope.LOCAL,
    ConfigScope.PROJECT,
    ConfigScope.USER,
    ConfigScope.PLUGIN,
    ConfigScope.CLAUDE_AI,
)

#: The scopes that deduplicate by ENDPOINT rather than by name [claude-code.md
#: §1]. Also Claude Code's model, not a universal one.
ENDPOINT_KEYED_SCOPES: frozenset[ConfigScope] = frozenset(
    {ConfigScope.PLUGIN, ConfigScope.CLAUDE_AI}
)

MANAGED_MCP_PATHS: dict[str, str] = {
    "darwin": "/Library/Application Support/ClaudeCode/managed-mcp.json",
    "linux": "/etc/claude-code/managed-mcp.json",
    "win32": r"C:\Program Files\ClaudeCode\managed-mcp.json",
}

#: Names Claude Code refuses or silently skips [claude-code.md §1].
RESERVED_SERVER_NAMES = frozenset(
    {"workspace", "claude-in-chrome", "computer-use", "Claude Preview", "Claude Browser"}
)

_APPROVED_KEYS = ("approvedMcpjsonServers", "approvedMcprcServers")
_REJECTED_KEYS = ("rejectedMcpjsonServers", "rejectedMcprcServers")
_ENABLED_KEYS = ("enabledMcpjsonServers", "enabledMcpServers")
_DISABLED_KEYS = ("disabledMcpjsonServers", "disabledMcpServers")


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def default_managed_mcp_path(platform: Optional[str] = None) -> Path:
    """The fixed OS path `managed-mcp.json` is deployed to [claude-code.md §1]."""
    key = platform if platform is not None else sys.platform
    return Path(MANAGED_MCP_PATHS.get(key, MANAGED_MCP_PATHS["linux"]))


def discover(
    project_path: str | Path,
    *,
    home: Optional[Path] = None,
    claude_json_path: Optional[Path] = None,
    project_mcp_path: Optional[Path] = None,
    user_settings_path: Optional[Path] = None,
    project_settings_paths: Optional[Sequence[Path]] = None,
    managed_path: Optional[Path] = None,
    installed_plugins_path: Optional[Path] = None,
    platform: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> DiscoveryResult:
    """Enumerate every MCP server the harness would load for `project_path`.

    Pure and side-effect-free: it reads files and returns a value. Every
    filesystem location is an injectable parameter with a real default, so the
    whole pass runs hermetically against a tmp_path tree — a discovery function
    that could only be exercised against the caller's real `~` would be tested
    by nobody.

    `now` must be timezone-aware; it is stamped as each server's
    `discovered_at` in the canonical `+00:00` form the base's record
    timestamps use.
    """
    project = Path(project_path).resolve()
    home_dir = home if home is not None else Path.home()
    claude_json = claude_json_path or (home_dir / ".claude.json")
    project_mcp = project_mcp_path or (project / ".mcp.json")
    user_settings = user_settings_path or (home_dir / ".claude" / "settings.json")
    project_settings = list(
        project_settings_paths
        if project_settings_paths is not None
        else (
            project / ".claude" / "settings.json",
            project / ".claude" / "settings.local.json",
        )
    )
    managed = managed_path or default_managed_mcp_path(platform)
    plugins_manifest = installed_plugins_path or (
        home_dir / ".claude" / "plugins" / "installed_plugins.json"
    )

    stamp = now if now is not None else datetime.datetime.now(datetime.UTC)
    if stamp.tzinfo is None:
        raise ValueError(
            "discover(now=...) must be timezone-aware; a naive clock read would "
            "stamp discovered_at in an unstated timezone"
        )
    discovered_at = stamp.astimezone(datetime.UTC).isoformat()

    findings: list[DiscoveryFinding] = []
    skipped: list[SkippedEntry] = []
    coverage: list[ScopeCoverage] = []

    def note_unreadable(path: Path, error: str, scope: Optional[ConfigScope]) -> None:
        findings.append(
            DiscoveryFinding(
                kind=FindingKind.UNREADABLE_CONFIG,
                severity="blocking",
                scope=scope,
                detail=(
                    f"{path} is present but could not be read: {error}. A scope that "
                    "cannot be read cannot be wrapped — its servers stay live."
                ),
            )
        )

    def collect(
        outcome: tuple[list[DiscoveredServer], list[SkippedEntry], ScopeCoverage],
    ) -> list[DiscoveredServer]:
        servers, bad, scope_coverage = outcome
        skipped.extend(bad)
        coverage.append(scope_coverage)
        return servers

    settings_blobs = _read_settings_scopes(
        (user_settings, *project_settings), on_unreadable=note_unreadable
    )
    claude = _ClaudeJson.load(claude_json, project, on_unreadable=note_unreadable)

    # Managed first: its presence decides whether anything else loads at all.
    managed_servers, managed_skipped, managed_control, managed_findings = (
        _discover_managed(
            managed, discovered_at=discovered_at, on_unreadable=note_unreadable
        )
    )
    skipped.extend(managed_skipped)
    findings.extend(managed_findings)

    local_servers = collect(claude.local_scope(project, discovered_at))
    project_servers = collect(
        _discover_project_scope(
            project_mcp, discovered_at=discovered_at, on_unreadable=note_unreadable
        )
    )
    user_servers = collect(claude.user_scope(discovered_at))
    plugin_servers = collect(
        _discover_plugins(
            plugins_manifest,
            settings_blobs,
            discovered_at=discovered_at,
            on_unreadable=note_unreadable,
        )
    )

    # claude.ai: reported, never enumerated.
    claude_ai = _claude_ai_signals(claude_json, claude.data, settings_blobs)
    claude_ai_coverage, claude_ai_findings = _claude_ai_report(claude_ai)
    coverage.append(claude_ai_coverage)
    findings.extend(claude_ai_findings)

    approvals = _ApprovalState.from_config(claude.project_entry, settings_blobs)
    scoped = [
        *(approvals.apply(s) for s in local_servers),
        *(approvals.apply(s) for s in project_servers),
        *(approvals.apply(s) for s in user_servers),
        *plugin_servers,
    ]

    if managed_control.present:
        effective, shadowed, shadow_findings = _apply_managed_exclusivity(
            managed_servers, scoped
        )
    else:
        effective, shadowed, shadow_findings = _resolve_shadowing(
            scoped,
            precedence=SCOPE_PRECEDENCE,
            endpoint_keyed_scopes=ENDPOINT_KEYED_SCOPES,
        )
    findings.extend(shadow_findings)
    findings.extend(_entry_findings(effective, shadowed, skipped))

    return DiscoveryResult(
        project_path=str(project),
        discovered_at=discovered_at,
        effective=effective,
        shadowed=shadowed,
        skipped=skipped,
        coverage=CoverageReport(
            scopes=coverage, claude_ai=claude_ai, managed=managed_control
        ),
        findings=findings,
    )


def to_locked_servers(result: DiscoveryResult) -> list[LockedServer]:
    """Project a Claude Code discovery pass onto the committed lock shape.

    The mechanism stays in core (`DiscoveryResult.to_locked_servers`) — the
    transport mapping and the refuse-rather-than-drop rule for `sse`/`ws` are
    harness-agnostic, and duplicating them per adapter is how five adapters end
    up with five slightly different lock projections. What this function adds is
    the one thing core cannot know and a caller should never have to supply: the
    HARNESS IDENTITY.

    That is worth a function rather than a documented convention. `harness` is
    the format's one closed catalog (TL9a) and the field that decides which
    adapter's scope vocabulary a `scope` string is read against, so a caller
    free to pass any member is a caller free to mislabel a lock — a `user`-scope
    Claude Code server recorded as some other harness's `user` scope would
    unwrap to the wrong file. Routing the projection through the adapter makes
    the identity structurally unmislabelable instead of merely documented.
    """
    return result.to_locked_servers(HARNESS)


# ---------------------------------------------------------------------------
# The rewrite half — coordinates only; interpose.py owns the mechanism
# ---------------------------------------------------------------------------


def config_sites(
    project_path: str | Path,
    *,
    home: Optional[Path] = None,
    claude_json_path: Optional[Path] = None,
    project_mcp_path: Optional[Path] = None,
) -> tuple[list[ConfigSite], ConfigSite]:
    """The writable `mcpServers` blocks for this project, and where the gateway goes.

    Three of Claude Code's six sources have a local block tegh can rewrite:

    ==========  ==============================  ==================================
    Scope       File                            Pointer
    ==========  ==============================  ==================================
    local       ``~/.claude.json``              ``projects.<abs project>.mcpServers``
    project     ``<project>/.mcp.json``         ``mcpServers``
    user        ``~/.claude.json``              ``mcpServers``
    ==========  ==============================  ==================================

    All three are returned even when empty, because "empty" is a fact that must
    be RE-ESTABLISHED on unwrap: a scope that had no block must end with no
    block, not with `{}`.

    **User scope is included deliberately**, though it is shared across every
    project. Scopes shadow rather than merge, and different names at different
    scopes all load side by side (§1) — so a user-scope server survives a wrap
    that only cleared the local one, and the gateway would be one server among
    several while `tegh posture` claimed exclusivity. The cost is real and the
    caller must surface it: wrapping one project displaces user-scope servers
    for all of them until unwrap. That is why unwrap puts back the servers the
    wrap displaced, as recorded, and not tegh's idea of them.

    The gateway lands at **local** scope: it stays out of the committable
    `.mcp.json`, so it is never accidentally shared, and it does not trigger the
    project-scope approval flow that would leave the gateway `⏸ Pending
    approval` until a human accepted it interactively (§1).
    """
    project = Path(project_path).expanduser().resolve()
    home_dir = home if home is not None else Path.home()
    claude_json = claude_json_path or (home_dir / ".claude.json")
    project_mcp = project_mcp_path or (project / ".mcp.json")

    local = ConfigSite(
        scope=ConfigScope.LOCAL,
        path=claude_json,
        pointer=("projects", str(project), "mcpServers"),
    )
    sites = [
        local,
        ConfigSite(scope=ConfigScope.PROJECT, path=project_mcp, pointer=("mcpServers",)),
        ConfigSite(scope=ConfigScope.USER, path=claude_json, pointer=("mcpServers",)),
    ]
    return sites, local


def unwritable_sources(result: DiscoveryResult) -> list[UnwritableSource]:
    """Server sources this adapter cannot rewrite, derived from a discovery pass.

    Not a static list: each entry is reported only when discovery actually saw
    the thing. A machine with no plugins and no claude.ai connectors has nothing
    here, and saying otherwise would train the operator to ignore the warning
    that matters.

    Plugin-provided servers are managed by enabling/disabling the plugin and are
    not addressable in any `mcpServers` block; claude.ai connectors are
    cloud-side with no local file at all; a `managed-mcp.json` has EXCLUSIVE
    control, under which `claude mcp add` fails outright and a wrap is not a
    wrap (§1, §6).
    """
    out: list[UnwritableSource] = []
    if result.coverage.managed.present:
        out.append(
            UnwritableSource(
                scope=ConfigScope.MANAGED,
                detail=(
                    "an enterprise managed-mcp.json is present and has EXCLUSIVE "
                    "control: no other scope loads, and tegh cannot write it "
                    "(root-owned). A local wrap is not a wrap here."
                ),
            )
        )
    plugin_servers = sorted(
        {
            s.server_id
            for s in [*result.effective, *result.shadowed]
            if s.scope is ConfigScope.PLUGIN
        }
    )
    if plugin_servers:
        out.append(
            UnwritableSource(
                scope=ConfigScope.PLUGIN,
                detail=(
                    f"plugin-provided server(s) {plugin_servers} auto-connect on "
                    "session start and live in no rewritable mcpServers block — "
                    "disable the plugin, or they stay live past the wrap."
                ),
            )
        )
    if not result.coverage.claude_ai.connectors_disabled:
        out.append(
            UnwritableSource(
                scope=ConfigScope.CLAUDE_AI,
                detail=(
                    "claude.ai connectors are cloud-side with no local file; they "
                    "are matched by endpoint, not name, and survive any local "
                    "rewrite. Set disableClaudeAiConnectors to close this."
                ),
            )
        )
    return out


def gateway_entry(
    project: Path | str, *, launcher: Sequence[str], home: Path | str
) -> dict[str, Any]:
    """The `mcpServers` entry that points Claude Code at tegh's gateway.

    A stdio entry naming tegh's gateway command and **nothing else**. No `env`
    block, no headers, no store path, no key — every value the gateway needs is
    resolved inside that process from the tegh home, which is why this entry is
    safe to write into a file the wrapped agent can read. Putting
    `BROKER_HMAC_KEY` here to save a lookup would recreate, in tegh's own
    config, precisely the plaintext-credential exposure the wrap exists to
    remove (`docs/references/harnesses/claude-code.md` §2).

    `launcher` is a full argv prefix rather than one executable because tegh is
    not always reachable as a console script — an editable checkout has no
    `tegh` on PATH, and the harness spawns stdio children with a minimal
    environment, so `["<python>", "-P", "-m", "tegh.cli"]` is a real and
    common form of the same command.

    `home` is written EXPLICITLY for the same reason the launcher is absolute:
    the harness gives a spawned stdio child a minimal environment, so
    `TEGH_HOME` set in the operator's shell at wrap time is not there at spawn
    time. Without it the gateway silently resolves the DEFAULT `~/.tegh`, finds
    no manifest for this project, and refuses — a server the user sees as
    `failed` for a reason that is nowhere near the cause. Every resolution input
    the gateway needs is on this line; nothing is inherited.
    """
    # The command itself is `launch.gateway_argv`, shared with `tegh call`, so
    # what this writes into the config and what `tegh call` spawns cannot drift.
    # This function contributes only Claude Code's `command` + `args` shape.
    command, *args = gateway_argv(project, launcher=launcher, home=home)
    return {"command": command, "args": args}


# ---------------------------------------------------------------------------
# Per-scope readers
# ---------------------------------------------------------------------------


def _read_settings_scopes(
    paths: Iterable[Path], *, on_unreadable: Any
) -> list[tuple[Path, dict]]:
    """Read the settings files that carry approval, enablement, and plugin state.

    User and project settings only. Managed settings and the CLI's `--settings`
    override are NOT read — they can also carry these keys, so their absence
    from this list is a real coverage limit, recorded as such on each scope.
    """
    blobs: list[tuple[Path, dict]] = []
    for path in paths:
        read = _read_json(path)
        if read.error is not None:
            on_unreadable(path, read.error, None)
        elif read.present and isinstance(read.data, dict):
            blobs.append((path, read.data))
    return blobs


class _ClaudeJson:
    """`~/.claude.json`, read ONCE and indexed.

    One file hosts three separate things — the user scope (top-level
    `mcpServers`), the local scope (`projects[<path>].mcpServers`), and the
    per-project approval lists — so reading it once and handing out views keeps
    a single parse behind all three and makes the shared failure mode (the file
    is corrupt) fail identically for each.
    """

    __slots__ = ("path", "read", "data", "project_entry")

    def __init__(self, path: Path, read: _JsonRead, project_entry: dict) -> None:
        self.path = path
        self.read = read
        self.data: dict = read.data if isinstance(read.data, dict) else {}
        self.project_entry = project_entry

    @classmethod
    def load(cls, path: Path, project: Path, *, on_unreadable: Any) -> "_ClaudeJson":
        read = _read_json(path)
        if read.error is not None:
            on_unreadable(path, read.error, None)
        data = read.data if isinstance(read.data, dict) else {}
        entry: dict = {}
        projects = data.get("projects")
        if isinstance(projects, dict):
            raw = projects.get(str(project))
            if isinstance(raw, dict):
                entry = raw
        return cls(path, read, entry)

    @property
    def _sources(self) -> tuple[list[str], list[str]]:
        """(read, absent). A present-but-unparseable file is NEITHER.

        Claiming a corrupt file as "read" would let coverage assert it covered
        a scope it in fact dropped — the exact silent incompleteness the
        coverage field exists to make impossible.
        """
        if self.read.error is not None:
            return [], []
        if self.read.present:
            return [str(self.path)], []
        return [], [str(self.path)]

    def local_scope(
        self, project: Path, discovered_at: str
    ) -> tuple[list[DiscoveredServer], list[SkippedEntry], ScopeCoverage]:
        servers, skipped = _parse_servers_block(
            self.project_entry.get("mcpServers"),
            scope=ConfigScope.LOCAL,
            source_path=f"{self.path}#/projects/{project}/mcpServers",
            discovered_at=discovered_at,
            reserved_names=RESERVED_SERVER_NAMES,
        )
        read, absent = self._sources
        return (
            servers,
            skipped,
            ScopeCoverage(
                scope=ConfigScope.LOCAL,
                enumerable=True,
                sources_read=read,
                sources_absent=absent,
                detail=(
                    None
                    if self.project_entry
                    else "no entry for this project path — treated as empty"
                ),
            ),
        )

    def user_scope(
        self, discovered_at: str
    ) -> tuple[list[DiscoveredServer], list[SkippedEntry], ScopeCoverage]:
        # Absence of the top-level key means EMPTY, never an error: a machine
        # with no user-scope servers simply has no `mcpServers` key at all.
        servers, skipped = _parse_servers_block(
            self.data.get("mcpServers"),
            scope=ConfigScope.USER,
            source_path=f"{self.path}#/mcpServers",
            discovered_at=discovered_at,
            reserved_names=RESERVED_SERVER_NAMES,
        )
        read, absent = self._sources
        return (
            servers,
            skipped,
            ScopeCoverage(
                scope=ConfigScope.USER,
                enumerable=True,
                sources_read=read,
                sources_absent=absent,
                detail=(
                    None
                    if "mcpServers" in self.data
                    else "no top-level 'mcpServers' key — absence means empty"
                ),
            ),
        )


def _discover_project_scope(
    project_mcp: Path, *, discovered_at: str, on_unreadable: Any
) -> tuple[list[DiscoveredServer], list[SkippedEntry], ScopeCoverage]:
    """Read `.mcp.json` from the project root — the committable, shared scope."""
    read = _read_json(project_mcp)
    if read.error is not None:
        on_unreadable(project_mcp, read.error, ConfigScope.PROJECT)
    servers, skipped = _parse_servers_block(
        read.data.get("mcpServers") if isinstance(read.data, dict) else None,
        scope=ConfigScope.PROJECT,
        source_path=str(project_mcp),
        discovered_at=discovered_at,
        reserved_names=RESERVED_SERVER_NAMES,
    )
    ok = read.present and read.error is None
    return (
        servers,
        skipped,
        ScopeCoverage(
            scope=ConfigScope.PROJECT,
            enumerable=True,
            sources_read=[str(project_mcp)] if ok else [],
            sources_absent=[] if read.present else [str(project_mcp)],
        ),
    )


def _discover_managed(
    managed: Path, *, discovered_at: str, on_unreadable: Any
) -> tuple[
    list[DiscoveredServer], list[SkippedEntry], ManagedControl, list[DiscoveryFinding]
]:
    """Read `managed-mcp.json`, whose mere PRESENCE is the decisive fact.

    Presence is what makes it exclusive — not its contents. An empty or even
    unparseable managed file still suppresses every other scope, so the
    `exclusive` flag keys on presence and a corrupt file yields exclusive
    control over zero servers (nothing loads), which is the conservative
    reading and the safe one.
    """
    read = _read_json(managed)
    if read.error is not None:
        on_unreadable(managed, read.error, ConfigScope.MANAGED)
    servers, skipped = _parse_servers_block(
        read.data.get("mcpServers") if isinstance(read.data, dict) else None,
        scope=ConfigScope.MANAGED,
        source_path=str(managed),
        discovered_at=discovered_at,
        reserved_names=RESERVED_SERVER_NAMES,
    )
    control = ManagedControl(
        present=read.present,
        path=str(managed),
        exclusive=read.present,
        server_ids=sorted(s.server_id for s in servers),
    )
    findings: list[DiscoveryFinding] = []
    if read.present:
        findings.append(
            DiscoveryFinding(
                kind=FindingKind.MANAGED_EXCLUSIVE,
                severity="blocking",
                scope=ConfigScope.MANAGED,
                detail=(
                    f"{managed} is deployed and has EXCLUSIVE control: no server of any "
                    "other scope or source loads, and `claude mcp add` fails outright. "
                    "Wrapping this machine requires admin/root to edit that file — not "
                    "a lever a user-level install can assume it has."
                ),
            )
        )
    return servers, skipped, control, findings


def _claude_ai_report(
    signals: ClaudeAiSignals,
) -> tuple[ScopeCoverage, list[DiscoveryFinding]]:
    """State the un-enumerable scope as a coverage entry AND a finding.

    Both, deliberately. The coverage entry is the structural record that a
    scope exists and was not enumerated; the finding is what a human actually
    reads. A gap present only in one of the two is a gap that gets skimmed.
    """
    coverage = ScopeCoverage(
        scope=ConfigScope.CLAUDE_AI,
        enumerable=False,
        detail=(
            "claude.ai connectors are cloud-side and matched by endpoint URL; no local "
            "file governs them, so discovery CANNOT list them"
        ),
    )
    closed = signals.connectors_disabled is True
    findings = [
        DiscoveryFinding(
            kind=FindingKind.CLAUDE_AI_NOT_ENUMERABLE,
            severity="info" if closed else "blocking",
            scope=ConfigScope.CLAUDE_AI,
            detail=(
                "claude.ai connectors cannot be enumerated from local disk. A "
                "local-only wrap does not cover them; they stay reachable unless "
                "`disableClaudeAiConnectors` is set or matching `deniedMcpServers` "
                "entries are configured."
                + (" `disableClaudeAiConnectors` is set — the gap is closed." if closed else "")
            ),
        )
    ]
    if signals.sources_conflict:
        findings.append(
            DiscoveryFinding(
                kind=FindingKind.CLAUDE_AI_SETTING_CONFLICT,
                severity="warning",
                scope=ConfigScope.CLAUDE_AI,
                detail=(
                    "`disableClaudeAiConnectors` is set BOTH ways across config "
                    "scopes — disabled by "
                    + ", ".join(signals.disabled_sources)
                    + "; enabled by "
                    + ", ".join(signals.enabled_sources)
                    + ". tegh resolves a disagreement toward 'connectors are live' "
                    "rather than guessing this harness's precedence for the key, so "
                    "the gap above is reported as OPEN. Make the sources agree to "
                    "close it."
                ),
            )
        )
    if signals.ever_connected:
        findings.append(
            DiscoveryFinding(
                kind=FindingKind.CLAUDE_AI_EVER_CONNECTED,
                severity="warning",
                scope=ConfigScope.CLAUDE_AI,
                detail=(
                    "`claudeAiMcpEverConnected` is true: this account HAS connected a "
                    "claude.ai connector at some point. The set is still not enumerable "
                    "locally — this is a reason to expect the gap is real, not a "
                    "listing of it."
                ),
            )
        )
    return coverage, findings


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _apply_managed_exclusivity(
    managed_servers: Sequence[DiscoveredServer], scoped: Sequence[DiscoveredServer]
) -> tuple[list[DiscoveredServer], list[DiscoveredServer], list[DiscoveryFinding]]:
    """Managed control replaces the precedence ladder rather than topping it."""
    shadowed = [s.model_copy(update={"shadowed_by": ConfigScope.MANAGED}) for s in scoped]
    findings = [
        DiscoveryFinding(
            kind=FindingKind.SHADOWED_BY_HIGHER_SCOPE,
            severity="warning",
            server_id=s.server_id,
            scope=s.scope,
            detail=(
                f"{s.scope.value}-scope server {s.server_id!r} does not load: "
                "managed-mcp.json has exclusive control"
            ),
        )
        for s in scoped
    ]
    return list(managed_servers), shadowed, findings


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


class _ApprovalState:
    """The per-project approval/enablement lists, merged across their homes.

    Ground truth (live `~/.claude.json`, 2026-07-25) corrects the reference doc
    here: these keys live on the PROJECT ENTRY in `~/.claude.json`, not only in
    a `settings.json`. Both a current (`*McpjsonServers`) and a legacy
    (`*McprcServers`) vocabulary occur, so both are read.
    """

    __slots__ = ("approved", "rejected", "enabled", "disabled", "enable_all")

    def __init__(
        self,
        approved: set[str],
        rejected: set[str],
        enabled: set[str],
        disabled: set[str],
        enable_all: bool,
    ) -> None:
        self.approved = approved
        self.rejected = rejected
        self.enabled = enabled
        self.disabled = disabled
        self.enable_all = enable_all

    @classmethod
    def from_config(
        cls,
        project_entry: Mapping[str, Any],
        settings_blobs: Sequence[tuple[Path, Mapping[str, Any]]],
    ) -> "_ApprovalState":
        sources: list[Mapping[str, Any]] = [project_entry, *(b for _, b in settings_blobs)]
        approved: set[str] = set()
        rejected: set[str] = set()
        enabled: set[str] = set()
        disabled: set[str] = set()
        enable_all = False
        for source in sources:
            approved |= _string_set(source, _APPROVED_KEYS)
            rejected |= _string_set(source, _REJECTED_KEYS)
            enabled |= _string_set(source, _ENABLED_KEYS)
            disabled |= _string_set(source, _DISABLED_KEYS)
            enable_all = enable_all or source.get("enableAllProjectMcpServers") is True
        return cls(approved, rejected, enabled, disabled, enable_all)

    def apply(self, server: DiscoveredServer) -> DiscoveredServer:
        """Compute activation. Rejection beats disablement beats approval."""
        name = server.server_id
        if name in self.rejected:
            return server.model_copy(
                update={
                    "activation": ActivationState.REJECTED,
                    "activation_reason": "listed in a rejected-servers list",
                }
            )
        if name in self.disabled:
            return server.model_copy(
                update={
                    "activation": ActivationState.DISABLED,
                    "activation_reason": "listed in a disabled-servers list",
                }
            )
        if server.scope is not ConfigScope.PROJECT:
            return server
        if name in self.approved or name in self.enabled:
            return server
        if self.enable_all:
            return server.model_copy(
                update={"activation_reason": "enableAllProjectMcpServers is set"}
            )
        return server.model_copy(
            update={
                "activation": ActivationState.PENDING_APPROVAL,
                "activation_reason": (
                    "a .mcp.json server is not connected until a human accepts it "
                    "interactively; it is neither approved nor blanket-enabled"
                ),
            }
        )


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------


def _discover_plugins(
    manifest_path: Path,
    settings_blobs: Sequence[tuple[Path, Mapping[str, Any]]],
    *,
    discovered_at: str,
    on_unreadable: Any,
) -> tuple[list[DiscoveredServer], list[SkippedEntry], ScopeCoverage]:
    """Enumerate plugin-provided servers from the installed-plugin manifest.

    Enumerated from `installed_plugins.json` rather than by globbing the plugin
    cache, deliberately: the cache retains orphaned older versions of a plugin
    alongside the live one, and globbing would report servers from install
    directories the harness will never load.

    A plugin root may declare servers in `.mcp.json` at its root or inline in
    `.claude-plugin/plugin.json` under `mcpServers`. Enablement comes from
    `enabledPlugins` in the read settings scopes — a disabled plugin's servers
    do not load, but they are reported (as DISABLED) rather than omitted, since
    re-enabling the plugin makes them live again without any config edit tegh
    would see.
    """
    read = _read_json(manifest_path)
    if read.error is not None:
        on_unreadable(manifest_path, read.error, ConfigScope.PLUGIN)

    enabled_plugins: dict[str, bool] = {}
    for _, blob in settings_blobs:
        block = blob.get("enabledPlugins")
        if isinstance(block, dict):
            enabled_plugins.update(
                {str(k): bool(v) for k, v in block.items() if isinstance(v, bool)}
            )

    servers: list[DiscoveredServer] = []
    skipped: list[SkippedEntry] = []
    read_paths: list[str] = []
    plugins = (read.data or {}).get("plugins") if isinstance(read.data, dict) else None

    if isinstance(plugins, dict):
        for plugin_id in sorted(plugins):
            installs = plugins[plugin_id]
            if not isinstance(installs, list):
                continue
            for install in installs:
                if not isinstance(install, dict):
                    continue
                root = install.get("installPath")
                if not isinstance(root, str) or not root:
                    continue
                root_path = Path(root)
                for candidate, key in (
                    (root_path / ".mcp.json", "mcpServers"),
                    (root_path / ".claude-plugin" / "plugin.json", "mcpServers"),
                ):
                    plugin_read = _read_json(candidate)
                    if plugin_read.error is not None:
                        on_unreadable(candidate, plugin_read.error, ConfigScope.PLUGIN)
                        continue
                    if not plugin_read.present:
                        continue
                    read_paths.append(str(candidate))
                    found, bad = _parse_servers_block(
                        (plugin_read.data or {}).get(key),
                        scope=ConfigScope.PLUGIN,
                        source_path=str(candidate),
                        discovered_at=discovered_at,
                        reserved_names=RESERVED_SERVER_NAMES,
                        plugin_id=str(plugin_id),
                    )
                    is_enabled = enabled_plugins.get(str(plugin_id), True)
                    for server in found:
                        servers.append(
                            server
                            if is_enabled
                            else server.model_copy(
                                update={
                                    "activation": ActivationState.DISABLED,
                                    "activation_reason": (
                                        f"plugin {plugin_id} is disabled in enabledPlugins"
                                    ),
                                }
                            )
                        )
                    skipped.extend(bad)

    coverage = ScopeCoverage(
        scope=ConfigScope.PLUGIN,
        enumerable=True,
        sources_read=(
            [str(manifest_path)] if read.present and read.error is None else []
        )
        + sorted(read_paths),
        sources_absent=[] if read.present else [str(manifest_path)],
        detail=(
            "enumerated from installed_plugins.json install paths; plugin roots are "
            "read for .mcp.json and .claude-plugin/plugin.json"
        ),
    )
    return servers, skipped, coverage


# ---------------------------------------------------------------------------
# claude.ai signals
# ---------------------------------------------------------------------------


def _claude_ai_signals(
    claude_json: Path,
    claude_data: Mapping[str, Any],
    settings_blobs: Sequence[tuple[Path, Mapping[str, Any]]],
) -> ClaudeAiSignals:
    """Collect the only two local signals about a scope that cannot be listed."""
    sources: list[str] = []
    ever = claude_data.get("claudeAiMcpEverConnected")
    if isinstance(ever, bool):
        sources.append(f"{claude_json}#/claudeAiMcpEverConnected")
    else:
        ever = None

    # `disableClaudeAiConnectors`: collect EVERY source's assertion, then
    # resolve by the fail-toward-live rule (ClaudeAiSignals) — deliberately not
    # last-writer-wins. The previous implementation let whichever file was read
    # last decide, which silently resolved a disagreement in the optimistic
    # direction using a precedence order nobody had verified against the
    # (closed-source) harness.
    disabled_sources: list[str] = []
    enabled_sources: list[str] = []
    for path, blob in [(claude_json, claude_data), *settings_blobs]:
        value = blob.get("disableClaudeAiConnectors")
        if isinstance(value, bool):
            reference = f"{path}#/disableClaudeAiConnectors"
            (disabled_sources if value else enabled_sources).append(reference)
            sources.append(reference)

    if not disabled_sources and not enabled_sources:
        disabled = None  # nobody configured it — gates as "live", reported honestly as unset
    else:
        disabled = bool(disabled_sources) and not enabled_sources

    return ClaudeAiSignals(
        ever_connected=ever,
        connectors_disabled=disabled,
        disabled_sources=disabled_sources,
        enabled_sources=enabled_sources,
        signal_sources=sources,
    )
