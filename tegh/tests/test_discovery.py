"""Conformance + behavior tests for `tegh wrap claude`'s discovery half.

Every test is HERMETIC: a `home` and a project tree are built under `tmp_path`
and every filesystem location is injected. Nothing here reads the developer's
real `~/.claude.json` — which matters twice over. Once for reproducibility (a
test whose result depends on which MCP servers the author happens to have
installed is not a test), and once because that file holds real API keys, and a
failing assertion prints what it compared.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from tegh.discovery import (
    ActivationState,
    ConfigScope,
    DiscoveryResult,
    FindingKind,
    UnrepresentableTransportError,
)
from tegh.harnesses.claude_code import (
    default_managed_mcp_path,
    discover,
    to_locked_servers,
)
from tegh.lock import Harness

NOW = datetime.datetime(2026, 7, 25, 18, 30, 0, tzinfo=datetime.UTC)
STAMP = "2026-07-25T18:30:00+00:00"


# ---------------------------------------------------------------------------
# Hermetic fixture builder
# ---------------------------------------------------------------------------


class Bench:
    """A throwaway `home` + project tree, with every path injected into discover()."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.project = tmp_path / "proj"
        self.home.mkdir()
        self.project.mkdir()
        self.managed = tmp_path / "managed" / "managed-mcp.json"
        self.claude_json = self.home / ".claude.json"
        self.user_settings = self.home / ".claude" / "settings.json"
        self.plugins_manifest = self.home / ".claude" / "plugins" / "installed_plugins.json"

    # -- writers (fixtures only; discovery itself writes nothing) -----------
    def write_json(self, path: Path, payload: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def claude(
        self,
        *,
        user_servers: Optional[dict] = None,
        local_servers: Optional[dict] = None,
        project_entry: Optional[dict] = None,
        **top_level: Any,
    ) -> None:
        entry: dict[str, Any] = dict(project_entry or {})
        if local_servers is not None:
            entry["mcpServers"] = local_servers
        payload: dict[str, Any] = dict(top_level)
        if user_servers is not None:
            payload["mcpServers"] = user_servers
        if entry:
            payload["projects"] = {str(self.project.resolve()): entry}
        self.write_json(self.claude_json, payload)

    def mcp_json(self, servers: dict) -> None:
        self.write_json(self.project / ".mcp.json", {"mcpServers": servers})

    def settings(self, **payload: Any) -> None:
        self.write_json(self.user_settings, payload)

    def managed_file(self, servers: dict) -> None:
        self.write_json(self.managed, {"mcpServers": servers})

    def plugin(self, plugin_id: str, servers: dict, *, inline: bool = False) -> Path:
        root = self.home / "pluginroots" / plugin_id.replace("@", "_")
        manifest = self.plugins_manifest
        existing = json.loads(manifest.read_text()) if manifest.exists() else {"plugins": {}}
        existing.setdefault("plugins", {})[plugin_id] = [
            {"scope": "user", "installPath": str(root)}
        ]
        self.write_json(manifest, existing)
        if inline:
            self.write_json(root / ".claude-plugin" / "plugin.json", {"mcpServers": servers})
        else:
            self.write_json(root / ".mcp.json", {"mcpServers": servers})
        return root

    def run(self, **overrides: Any) -> DiscoveryResult:
        kwargs: dict[str, Any] = dict(
            home=self.home,
            managed_path=self.managed,
            platform="linux",
            now=NOW,
        )
        kwargs.update(overrides)
        return discover(self.project, **kwargs)


@pytest.fixture()
def bench(tmp_path: Path) -> Bench:
    return Bench(tmp_path)


STDIO = {"command": "/usr/bin/server", "args": ["--flag"]}
REMOTE = {"type": "http", "url": "https://mcp.example.com/mcp"}


def ids(servers) -> list[str]:
    return sorted(s.server_id for s in servers)


def kinds(result: DiscoveryResult) -> set[FindingKind]:
    return {f.kind for f in result.findings}


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------


class TestAbsence:
    def test_nothing_on_disk_is_empty_not_an_error(self, bench: Bench) -> None:
        result = bench.run()
        assert result.effective == []
        assert result.shadowed == []
        assert result.skipped == []
        assert result.coverage.managed.present is False
        assert result.discovered_at == STAMP

    def test_absent_files_are_recorded_as_absent_per_scope(self, bench: Bench) -> None:
        result = bench.run()
        by_scope = {c.scope: c for c in result.coverage.scopes}
        assert str(bench.claude_json) in by_scope[ConfigScope.LOCAL].sources_absent
        assert str(bench.project / ".mcp.json") in by_scope[ConfigScope.PROJECT].sources_absent

    def test_no_top_level_mcpservers_key_means_empty(self, bench: Bench) -> None:
        """Live ground truth: the key is simply absent when there are no user servers."""
        bench.claude(claudeAiMcpEverConnected=False)
        result = bench.run()
        assert result.effective == []
        user = next(c for c in result.coverage.scopes if c.scope is ConfigScope.USER)
        assert "absence means empty" in (user.detail or "")

    def test_project_path_absent_from_projects_map_is_empty(self, bench: Bench) -> None:
        bench.write_json(bench.claude_json, {"projects": {"/somewhere/else": {"mcpServers": {"x": STDIO}}}})
        assert bench.run().effective == []


# ---------------------------------------------------------------------------
# Per-scope enumeration
# ---------------------------------------------------------------------------


class TestScopes:
    def test_local_scope(self, bench: Bench) -> None:
        bench.claude(local_servers={"loc": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["loc"]
        assert result.effective[0].scope is ConfigScope.LOCAL
        assert result.effective[0].source_path.endswith("/mcpServers")

    def test_project_scope(self, bench: Bench) -> None:
        bench.mcp_json({"proj": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["proj"]
        assert result.effective[0].scope is ConfigScope.PROJECT
        assert result.effective[0].source_path == str(bench.project / ".mcp.json")

    def test_user_scope(self, bench: Bench) -> None:
        bench.claude(user_servers={"usr": REMOTE})
        result = bench.run()
        assert ids(result.effective) == ["usr"]
        assert result.effective[0].scope is ConfigScope.USER
        assert result.effective[0].transport == "http"

    def test_plugin_scope_from_root_mcp_json(self, bench: Bench) -> None:
        bench.plugin("demo@market", {"plug": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["plug"]
        assert result.effective[0].scope is ConfigScope.PLUGIN
        assert result.effective[0].plugin_id == "demo@market"

    def test_plugin_scope_from_inline_plugin_json(self, bench: Bench) -> None:
        bench.plugin("inline@market", {"plug": STDIO}, inline=True)
        assert ids(bench.run().effective) == ["plug"]

    def test_plugin_disabled_in_settings_is_reported_not_omitted(self, bench: Bench) -> None:
        bench.plugin("off@market", {"plug": STDIO})
        bench.settings(enabledPlugins={"off@market": False})
        server = bench.run().effective[0]
        assert server.activation is ActivationState.DISABLED
        assert "off@market" in (server.activation_reason or "")

    def test_managed_scope(self, bench: Bench) -> None:
        bench.managed_file({"mgd": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["mgd"]
        assert result.coverage.managed.server_ids == ["mgd"]

    def test_default_managed_path_is_platform_specific(self) -> None:
        assert "Application Support" in str(default_managed_mcp_path("darwin"))
        assert str(default_managed_mcp_path("linux")) == "/etc/claude-code/managed-mcp.json"


# ---------------------------------------------------------------------------
# Shadowing
# ---------------------------------------------------------------------------


class TestShadowing:
    def test_same_name_across_three_scopes_leaves_one_effective(self, bench: Bench) -> None:
        bench.claude(
            local_servers={"dup": {"command": "/local"}},
            user_servers={"dup": {"command": "/user"}},
        )
        bench.mcp_json({"dup": {"command": "/project"}})
        result = bench.run()
        assert ids(result.effective) == ["dup"]
        assert result.effective[0].scope is ConfigScope.LOCAL
        assert result.effective[0].command == "/local"
        assert sorted(s.scope.value for s in result.shadowed) == ["project", "user"]
        assert all(s.shadowed_by is ConfigScope.LOCAL for s in result.shadowed)
        assert FindingKind.SHADOWED_BY_HIGHER_SCOPE in kinds(result)

    def test_entries_are_never_field_merged(self, bench: Bench) -> None:
        """The whole entry wins — a losing scope's `args`/`env` must not leak in."""
        bench.claude(local_servers={"dup": {"command": "/local"}})
        bench.mcp_json({"dup": {"command": "/project", "args": ["--x"], "env": {"LOSER": "v"}}})
        winner = bench.run().effective[0]
        assert winner.args == []
        assert winner.env_names == []

    def test_different_names_at_different_scopes_all_load(self, bench: Bench) -> None:
        bench.claude(local_servers={"a": STDIO}, user_servers={"c": STDIO})
        bench.mcp_json({"b": STDIO})
        bench.plugin("p@m", {"d": {"command": "/d"}})
        result = bench.run()
        assert ids(result.effective) == ["a", "b", "c", "d"]
        assert result.shadowed == []

    @pytest.mark.parametrize(
        "higher_scope_setup,expected_winner",
        [
            ("local", ConfigScope.LOCAL),
            ("project", ConfigScope.PROJECT),
            ("user", ConfigScope.USER),
        ],
    )
    def test_precedence_ladder(
        self, bench: Bench, higher_scope_setup: str, expected_winner: ConfigScope
    ) -> None:
        """Each scope beats every scope below it; plugin is always the loser here."""
        bench.plugin("p@m", {"dup": {"command": "/plugin"}})
        if higher_scope_setup == "local":
            bench.claude(local_servers={"dup": {"command": "/plugin"}})
        elif higher_scope_setup == "project":
            bench.mcp_json({"dup": {"command": "/plugin"}})
        else:
            bench.claude(user_servers={"dup": {"command": "/plugin"}})
        result = bench.run()
        assert [s.scope for s in result.effective] == [expected_winner]
        assert [s.scope for s in result.shadowed] == [ConfigScope.PLUGIN]

    def test_plugin_dedupes_by_endpoint_so_same_command_is_shadowed(self, bench: Bench) -> None:
        bench.claude(user_servers={"named-one": {"command": "/same", "args": ["--a"]}})
        bench.plugin("p@m", {"named-two": {"command": "/same", "args": ["--a"]}})
        result = bench.run()
        assert ids(result.effective) == ["named-one"]
        assert ids(result.shadowed) == ["named-two"]

    def test_plugin_sharing_a_name_but_not_an_endpoint_still_loads(self, bench: Bench) -> None:
        """The documented name-vs-endpoint asymmetry: BOTH load, and that is surprising."""
        bench.claude(user_servers={"dup": {"command": "/user"}})
        bench.plugin("p@m", {"dup": {"command": "/plugin-different"}})
        result = bench.run()
        assert len(result.effective) == 2
        assert result.shadowed == []
        assert FindingKind.NAME_COLLIDES_BUT_BOTH_LOAD in kinds(result)

    def test_inactive_shadower_is_flagged(self, bench: Bench) -> None:
        """A pending project entry shadows a user entry — but may not itself load."""
        bench.claude(user_servers={"dup": {"command": "/user"}})
        bench.mcp_json({"dup": {"command": "/project"}})
        result = bench.run()
        assert FindingKind.INACTIVE_SHADOWER in kinds(result)


# ---------------------------------------------------------------------------
# Managed exclusivity
# ---------------------------------------------------------------------------


class TestManagedExclusive:
    def test_managed_present_suppresses_every_other_scope(self, bench: Bench) -> None:
        bench.claude(local_servers={"loc": STDIO}, user_servers={"usr": STDIO})
        bench.mcp_json({"proj": STDIO})
        bench.plugin("p@m", {"plug": STDIO})
        bench.managed_file({"mgd": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["mgd"]
        assert ids(result.shadowed) == ["loc", "plug", "proj", "usr"]
        assert all(s.shadowed_by is ConfigScope.MANAGED for s in result.shadowed)

    def test_managed_presence_is_a_blocking_finding(self, bench: Bench) -> None:
        bench.managed_file({})
        result = bench.run()
        blocking = {f.kind for f in result.blocking_findings}
        assert FindingKind.MANAGED_EXCLUSIVE in blocking
        assert result.coverage.managed.exclusive is True

    def test_empty_managed_file_still_takes_exclusive_control(self, bench: Bench) -> None:
        bench.claude(user_servers={"usr": STDIO})
        bench.managed_file({})
        result = bench.run()
        assert result.effective == []
        assert ids(result.shadowed) == ["usr"]


# ---------------------------------------------------------------------------
# claude.ai — the gap that must be reported, never listed
# ---------------------------------------------------------------------------


class TestClaudeAiGap:
    def test_gap_is_always_reported_even_on_a_bare_machine(self, bench: Bench) -> None:
        result = bench.run()
        assert result.coverage.claude_ai.enumerable is False
        assert ConfigScope.CLAUDE_AI in result.coverage.unenumerable_scopes
        gap = next(f for f in result.findings if f.kind is FindingKind.CLAUDE_AI_NOT_ENUMERABLE)
        assert gap.severity == "blocking"

    def test_ever_connected_signal_is_surfaced(self, bench: Bench) -> None:
        bench.claude(claudeAiMcpEverConnected=True)
        result = bench.run()
        assert result.coverage.claude_ai.ever_connected is True
        assert FindingKind.CLAUDE_AI_EVER_CONNECTED in kinds(result)

    def test_never_connected_does_not_raise_the_extra_finding(self, bench: Bench) -> None:
        bench.claude(claudeAiMcpEverConnected=False)
        result = bench.run()
        assert result.coverage.claude_ai.ever_connected is False
        assert FindingKind.CLAUDE_AI_EVER_CONNECTED not in kinds(result)

    def test_disable_flag_downgrades_the_gap_to_info(self, bench: Bench) -> None:
        bench.settings(disableClaudeAiConnectors=True)
        result = bench.run()
        assert result.coverage.claude_ai.connectors_disabled is True
        gap = next(f for f in result.findings if f.kind is FindingKind.CLAUDE_AI_NOT_ENUMERABLE)
        assert gap.severity == "info"

    def test_no_claude_ai_server_is_ever_invented(self, bench: Bench) -> None:
        bench.claude(claudeAiMcpEverConnected=True)
        assert all(s.scope is not ConfigScope.CLAUDE_AI for s in bench.run().effective)


class TestConnectorsDisabledResolution:
    """`disableClaudeAiConnectors` resolves toward "the gap is live"
    [ruling: maintainer, 2026-07-25].

    Deliberately NOT last-writer-wins: this key is one of two conditions
    deciding whether a local-only wrap is actually a wrap, and Claude Code's
    real precedence for it is unverified (closed source). Resolving a
    disagreement by a guessed precedence would report a closed gap tegh cannot
    establish is closed.
    """

    def test_unset_everywhere_stays_none_and_blocks(self, bench: Bench) -> None:
        """Nothing observed is reported as unset, not as an explicit `false` —
        different things to tell a user, identical at the gate."""
        signals = bench.run().coverage.claude_ai
        assert signals.connectors_disabled is None
        assert signals.disabled_sources == [] and signals.enabled_sources == []
        assert not signals.sources_conflict

    def test_a_single_false_keeps_the_gap_open(self, bench: Bench) -> None:
        bench.settings(disableClaudeAiConnectors=False)
        result = bench.run()
        assert result.coverage.claude_ai.connectors_disabled is False
        gap = next(f for f in result.findings if f.kind is FindingKind.CLAUDE_AI_NOT_ENUMERABLE)
        assert gap.severity == "blocking"

    def test_agreeing_true_sources_close_the_gap(self, bench: Bench) -> None:
        bench.claude(disableClaudeAiConnectors=True)
        bench.settings(disableClaudeAiConnectors=True)
        result = bench.run()
        signals = result.coverage.claude_ai
        assert signals.connectors_disabled is True
        assert len(signals.disabled_sources) == 2 and not signals.sources_conflict
        assert FindingKind.CLAUDE_AI_SETTING_CONFLICT not in kinds(result)

    def test_conflict_resolves_open_and_names_both_sides(self, bench: Bench) -> None:
        """The whole point of the ruling: a `true` somewhere does NOT beat a
        `false` somewhere else just because it was read later."""
        bench.claude(disableClaudeAiConnectors=False)
        bench.settings(disableClaudeAiConnectors=True)
        result = bench.run()
        signals = result.coverage.claude_ai

        assert signals.sources_conflict
        assert signals.connectors_disabled is False, "a conflict must not resolve optimistically"
        gap = next(f for f in result.findings if f.kind is FindingKind.CLAUDE_AI_NOT_ENUMERABLE)
        assert gap.severity == "blocking"

        conflict = next(
            f for f in result.findings if f.kind is FindingKind.CLAUDE_AI_SETTING_CONFLICT
        )
        # Naming the files is what makes the finding actionable — a bare
        # "your sources disagree" leaves the user with nowhere to go.
        assert str(bench.claude_json) in conflict.detail
        assert str(bench.user_settings) in conflict.detail

    def test_conflict_is_order_independent(self, bench: Bench) -> None:
        """The defect being fixed was order-sensitivity, so assert its absence
        directly: swapping which file holds which value changes nothing."""
        bench.claude(disableClaudeAiConnectors=True)
        bench.settings(disableClaudeAiConnectors=False)
        signals = bench.run().coverage.claude_ai
        assert signals.connectors_disabled is False and signals.sources_conflict


# ---------------------------------------------------------------------------
# Activation of project-scope servers
# ---------------------------------------------------------------------------


class TestActivation:
    @pytest.mark.parametrize(
        "project_entry,expected",
        [
            pytest.param({}, ActivationState.PENDING_APPROVAL, id="unapproved-is-pending"),
            pytest.param(
                {"approvedMcpjsonServers": ["srv"]}, ActivationState.ACTIVE, id="approved"
            ),
            pytest.param(
                {"approvedMcprcServers": ["srv"]},
                ActivationState.ACTIVE,
                id="legacy-mcprc-approved",
            ),
            pytest.param(
                {"enabledMcpjsonServers": ["srv"]}, ActivationState.ACTIVE, id="enabled"
            ),
            pytest.param(
                {"enableAllProjectMcpServers": True}, ActivationState.ACTIVE, id="enable-all"
            ),
            pytest.param(
                {"rejectedMcpjsonServers": ["srv"]}, ActivationState.REJECTED, id="rejected"
            ),
            pytest.param(
                {"rejectedMcprcServers": ["srv"]},
                ActivationState.REJECTED,
                id="legacy-mcprc-rejected",
            ),
            pytest.param(
                {"disabledMcpjsonServers": ["srv"]}, ActivationState.DISABLED, id="disabled"
            ),
            pytest.param(
                {"disabledMcpServers": ["srv"]},
                ActivationState.DISABLED,
                id="legacy-disabled-vocab",
            ),
            pytest.param(
                {"approvedMcpjsonServers": ["srv"], "rejectedMcpjsonServers": ["srv"]},
                ActivationState.REJECTED,
                id="rejection-beats-approval",
            ),
            pytest.param(
                {"enableAllProjectMcpServers": True, "disabledMcpjsonServers": ["srv"]},
                ActivationState.DISABLED,
                id="disable-beats-enable-all",
            ),
        ],
    )
    def test_project_scope_activation(
        self, bench: Bench, project_entry: dict, expected: ActivationState
    ) -> None:
        bench.claude(project_entry=project_entry)
        bench.mcp_json({"srv": STDIO})
        server = bench.run().effective[0]
        assert server.activation is expected

    def test_local_scope_servers_need_no_approval(self, bench: Bench) -> None:
        bench.claude(local_servers={"loc": STDIO})
        assert bench.run().effective[0].activation is ActivationState.ACTIVE

    def test_user_scope_server_can_still_be_disabled(self, bench: Bench) -> None:
        bench.claude(
            user_servers={"usr": STDIO}, project_entry={"disabledMcpjsonServers": ["usr"]}
        )
        assert bench.run().effective[0].activation is ActivationState.DISABLED

    def test_approval_keys_are_also_honored_from_settings_json(self, bench: Bench) -> None:
        bench.mcp_json({"srv": STDIO})
        bench.settings(enableAllProjectMcpServers=True)
        assert bench.run().effective[0].activation is ActivationState.ACTIVE

    def test_inactive_server_raises_an_informational_finding(self, bench: Bench) -> None:
        bench.mcp_json({"srv": STDIO})
        assert FindingKind.NOT_ACTIVE in kinds(bench.run())


# ---------------------------------------------------------------------------
# TL10 — no secret may enter the result
# ---------------------------------------------------------------------------


SECRET = "sk-live-DO-NOT-LEAK-3f9a1c7b"
HEADER_SECRET = "Bearer tok-DO-NOT-LEAK-8821"


class TestNoSecretsLeak:
    def _loaded(self, bench: Bench) -> DiscoveryResult:
        bench.claude(
            local_servers={
                "stdio-secret": {
                    "command": "/usr/bin/server",
                    "args": ["--flag"],
                    "env": {"API_KEY": SECRET, "REGION": "us-east-1"},
                },
                "http-secret": {
                    "type": "http",
                    "url": "https://mcp.example.com/mcp",
                    "headers": {"Authorization": HEADER_SECRET},
                },
            }
        )
        return bench.run()

    def test_only_names_are_recorded(self, bench: Bench) -> None:
        by_id = {s.server_id: s for s in self._loaded(bench).effective}
        assert by_id["stdio-secret"].env_names == ["API_KEY", "REGION"]
        assert by_id["http-secret"].header_names == ["Authorization"]

    def test_secret_values_appear_nowhere_in_the_serialized_result(self, bench: Bench) -> None:
        blob = self._loaded(bench).model_dump_json()
        assert SECRET not in blob
        assert HEADER_SECRET not in blob
        # And the sanity check that the assertion above is not vacuous.
        assert "API_KEY" in blob

    def test_secret_values_appear_in_no_finding_detail(self, bench: Bench) -> None:
        details = " ".join(f.detail for f in self._loaded(bench).findings)
        assert SECRET not in details
        assert HEADER_SECRET not in details

    def test_locked_projection_also_carries_no_values(self, bench: Bench) -> None:
        locked = to_locked_servers(self._loaded(bench))
        blob = json.dumps([s.model_dump() for s in locked])
        assert SECRET not in blob
        assert HEADER_SECRET not in blob

    def test_a_parse_error_message_quotes_no_file_content(self, bench: Bench) -> None:
        bench.claude_json.write_text(
            '{"mcpServers": {"x": {"env": {"K": "' + SECRET + '"}}}',  # truncated
            encoding="utf-8",
        )
        result = bench.run()
        assert SECRET not in result.model_dump_json()
        assert FindingKind.UNREADABLE_CONFIG in kinds(result)


# ---------------------------------------------------------------------------
# `${VAR}` references are recorded, never expanded
# ---------------------------------------------------------------------------


class TestEnvExpansion:
    def test_references_are_flagged_and_named_but_not_expanded(self, bench: Bench) -> None:
        bench.claude(
            local_servers={
                "parm": {
                    "command": "${TOOL_HOME}/bin/server",
                    "args": ["--key", "${API_KEY}"],
                    "env": {"TOKEN": "${TOKEN_VALUE}"},
                }
            }
        )
        server = bench.run().effective[0]
        assert server.unexpanded_fields == ["args[1]", "command", "env.TOKEN"]
        assert server.unexpanded_vars == ["API_KEY", "TOKEN_VALUE", "TOOL_HOME"]
        # The literal text is preserved, never resolved against os.environ.
        assert server.command == "${TOOL_HOME}/bin/server"
        assert FindingKind.UNEXPANDED_ENV_REF in kinds(bench.run())

    def test_default_text_is_never_recorded(self, bench: Bench) -> None:
        """`${VAR:-default}`'s default half can itself be a literal secret."""
        bench.claude(
            local_servers={"parm": {"command": "/s", "env": {"K": "${API_KEY:-" + SECRET + "}"}}}
        )
        result = bench.run()
        assert result.effective[0].unexpanded_vars == ["API_KEY"]
        assert SECRET not in result.model_dump_json()

    def test_a_plain_value_raises_no_reference_finding(self, bench: Bench) -> None:
        bench.claude(local_servers={"plain": {"command": "/s", "env": {"K": "literal"}}})
        result = bench.run()
        assert result.effective[0].unexpanded_fields == []
        assert FindingKind.UNEXPANDED_ENV_REF not in kinds(result)


# ---------------------------------------------------------------------------
# Malformed entries: skipped, recorded, never fatal
# ---------------------------------------------------------------------------


class TestMalformed:
    @pytest.mark.parametrize(
        "entry,reason_fragment",
        [
            pytest.param(
                {"url": "https://x.example/mcp"},
                "'url' with no 'type'",
                id="url-without-type-is-a-documented-load-error",
            ),
            pytest.param({"type": "carrier-pigeon", "url": "x"}, "unknown transport", id="unknown-type"),
            pytest.param({"type": "stdio"}, "no 'command'", id="stdio-without-command"),
            pytest.param({"command": "   "}, "no 'command'", id="stdio-blank-command"),
            pytest.param({"type": "http"}, "no 'url'", id="http-without-url"),
            pytest.param({"type": "sse", "url": ""}, "no 'url'", id="sse-blank-url"),
            pytest.param("not-an-object", "not a JSON object", id="entry-is-a-string"),
            pytest.param({"type": 7, "command": "/s"}, "'type' is not a string", id="type-not-a-string"),
        ],
    )
    def test_malformed_entry_is_skipped_with_a_reason(
        self, bench: Bench, entry: Any, reason_fragment: str
    ) -> None:
        bench.claude(local_servers={"bad": entry})
        result = bench.run()
        assert result.effective == []
        assert len(result.skipped) == 1
        assert reason_fragment in result.skipped[0].reason
        assert FindingKind.MALFORMED_ENTRY in kinds(result)

    def test_a_malformed_entry_does_not_take_its_siblings_down(self, bench: Bench) -> None:
        bench.claude(local_servers={"bad": {"url": "https://x/mcp"}, "good": STDIO})
        result = bench.run()
        assert ids(result.effective) == ["good"]
        assert ids(result.skipped) == ["bad"]

    @pytest.mark.parametrize("name", ["workspace", "claude-in-chrome", "Claude Browser"])
    def test_reserved_names_are_skipped(self, bench: Bench, name: str) -> None:
        bench.claude(local_servers={name: STDIO})
        result = bench.run()
        assert result.effective == []
        assert FindingKind.RESERVED_SERVER_NAME in kinds(result)

    def test_a_non_object_mcpservers_block_is_empty_not_fatal(self, bench: Bench) -> None:
        bench.write_json(bench.claude_json, {"mcpServers": ["not", "a", "map"]})
        assert bench.run().effective == []

    def test_unreadable_project_config_is_a_blocking_finding(self, bench: Bench) -> None:
        (bench.project / ".mcp.json").write_text("{ this is not json", encoding="utf-8")
        result = bench.run()
        blocking = [f for f in result.blocking_findings if f.kind is FindingKind.UNREADABLE_CONFIG]
        assert blocking and blocking[0].scope is ConfigScope.PROJECT

    @pytest.mark.parametrize(
        "scope", [ConfigScope.PROJECT, ConfigScope.LOCAL, ConfigScope.USER, ConfigScope.PLUGIN]
    )
    def test_a_corrupt_file_is_never_claimed_as_read(
        self, bench: Bench, scope: ConfigScope
    ) -> None:
        """Coverage must not assert it covered a scope it in fact dropped."""
        target = {
            ConfigScope.PROJECT: bench.project / ".mcp.json",
            ConfigScope.LOCAL: bench.claude_json,
            ConfigScope.USER: bench.claude_json,
            ConfigScope.PLUGIN: bench.plugins_manifest,
        }[scope]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ truncated", encoding="utf-8")
        entry = next(c for c in bench.run().coverage.scopes if c.scope is scope)
        assert entry.sources_read == []
        assert entry.sources_absent == []


# ---------------------------------------------------------------------------
# Projection onto the frozen lock shape
# ---------------------------------------------------------------------------


class TestLockProjection:
    def test_stdio_projects_with_nothing_admitted(self, bench: Bench) -> None:
        bench.claude(
            local_servers={"s": {"command": "/bin/s", "args": ["-x"], "env": {"A": "1"}}}
        )
        locked = to_locked_servers(bench.run())
        assert len(locked) == 1
        assert locked[0].transport == "stdio"
        assert locked[0].command == "/bin/s"
        assert locked[0].args == ["-x"]
        assert locked[0].env_names == ["A"]
        assert locked[0].scope == ConfigScope.LOCAL.value
        assert locked[0].discovered_at == STAMP
        # TL8: discovered-with-nothing-admitted is the correct post-discovery state.
        assert locked[0].admitted == []

    def test_the_adapter_supplies_the_harness_identity(self, bench: Bench) -> None:
        """TL9a: core reads config files and cannot know whose they are, so the
        ADAPTER names the harness — and the scope crosses as the harness's own
        vocabulary in string form, not as core's internal `ConfigScope` enum."""
        bench.claude(local_servers={"s": {"command": "/bin/s"}})
        result = bench.run()
        assert result.effective[0].scope is ConfigScope.LOCAL  # core: enum
        locked = to_locked_servers(result)
        assert locked[0].harness is Harness.CLAUDE_CODE
        assert locked[0].scope == "local" and isinstance(locked[0].scope, str)
        # Core will not guess the identity for a caller that skips the adapter.
        with pytest.raises(TypeError):
            result.to_locked_servers()  # type: ignore[call-arg]

    @pytest.mark.parametrize("declared", ["http", "streamable-http"])
    def test_http_and_its_alias_both_project_to_streamable_http(
        self, bench: Bench, declared: str
    ) -> None:
        bench.claude(local_servers={"r": {"type": declared, "url": "https://x.example/mcp"}})
        result = bench.run()
        assert result.effective[0].transport == "http"
        assert to_locked_servers(result)[0].transport == "streamable-http"

    @pytest.mark.parametrize("declared", ["sse", "ws"])
    def test_unlockable_transports_refuse_rather_than_drop(
        self, bench: Bench, declared: str
    ) -> None:
        bench.claude(local_servers={"r": {"type": declared, "url": "https://x.example/s"}})
        result = bench.run()
        assert result.effective[0].is_lockable is False
        with pytest.raises(UnrepresentableTransportError, match="cannot represent"):
            to_locked_servers(result)


# ---------------------------------------------------------------------------
# Purity + the clock
# ---------------------------------------------------------------------------


class TestPurityAndClock:
    def test_discovery_writes_nothing(self, bench: Bench, tmp_path: Path) -> None:
        bench.claude(local_servers={"a": STDIO})
        bench.mcp_json({"b": STDIO})
        before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
        bench.run()
        assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before

    def test_repeated_runs_are_identical(self, bench: Bench) -> None:
        bench.claude(local_servers={"a": STDIO})
        assert bench.run().model_dump_json() == bench.run().model_dump_json()

    def test_a_naive_clock_is_refused(self, bench: Bench) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            bench.run(now=datetime.datetime(2026, 7, 25, 18, 30, 0))

    def test_a_non_utc_clock_is_normalized_to_utc(self, bench: Bench) -> None:
        eastern = datetime.timezone(datetime.timedelta(hours=-4))
        stamp = datetime.datetime(2026, 7, 25, 14, 30, 0, tzinfo=eastern)
        assert bench.run(now=stamp).discovered_at == STAMP
