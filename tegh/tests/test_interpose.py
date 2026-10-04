"""Conformance for the wrap's rewrite half.

The property that matters is not "the gateway got written" — it is that a user
gets their config back. So most of this file is about restore, and the two cases
that are easy to get subtly wrong: a scope that had NO block must end with no
block (not an empty one), and state the harness wrote WHILE wrapped must survive
the unwrap rather than being rolled back with it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tegh.configvalues import value_digest
from tegh.discovery import ConfigScope
from tegh.harnesses import claude_code
from tegh.interpose import (
    ConfigFormatUnreproducible,
    ConfigSite,
    InlineSecretRefused,
    WrapBackup,
    apply_interposition,
    plan_interposition,
    restore,
)

_PROJECT = "/Users/someone/Code/widget"


def _claude_json(tmp_path: Path, servers: dict) -> Path:
    """A `~/.claude.json`-shaped file: MCP config beside live session telemetry."""
    path = tmp_path / ".claude.json"
    document = {
        "numStartups": 1659,
        "installMethod": "native",
        "projects": {
            _PROJECT: {
                "hasTrustDialogAccepted": True,
                "lastCost": 0.42,
                "lastSessionId": "abc-123",
                # Non-ASCII on purpose. The real file contains it (a promo
                # banner with a `·`), and it is what makes `ensure_ascii=False`
                # load-bearing rather than cosmetic: the default escapes it to
                # ·, so a plain `indent=2` round-trip is NOT byte-identical
                # and the format guard would refuse every write.
                "tipsHistory": {"text": "50% weekly limits promo · through Aug 19"},
                "mcpServers": servers,
            },
            "/Users/someone/Code/other": {"lastSessionId": "zzz"},
        },
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _sites(claude_json: Path, project_mcp: Path) -> tuple[list[ConfigSite], ConfigSite]:
    local = ConfigSite(
        scope=ConfigScope.LOCAL,
        path=claude_json,
        pointer=("projects", _PROJECT, "mcpServers"),
    )
    return (
        [
            local,
            ConfigSite(scope=ConfigScope.PROJECT, path=project_mcp, pointer=("mcpServers",)),
            ConfigSite(scope=ConfigScope.USER, path=claude_json, pointer=("mcpServers",)),
        ],
        local,
    )


_GATEWAY = {"command": "/usr/local/bin/tegh", "args": ["gateway", "--project", _PROJECT]}


def _cleared(coordinate: str, value: str) -> dict[str, str]:
    """What `wrap` hands the gate: the coordinate AND the digest of the value
    the human actually classified."""
    return {coordinate: value_digest(value)}



def _wrap(
    tmp_path: Path,
    servers: dict,
    project_mcp_servers: dict | None = None,
    cleared_config: dict[str, str] | None = None,
):
    claude_json = _claude_json(tmp_path, servers)
    project_mcp = tmp_path / "project" / ".mcp.json"
    if project_mcp_servers is not None:
        project_mcp.parent.mkdir(parents=True, exist_ok=True)
        project_mcp.write_text(
            json.dumps({"mcpServers": project_mcp_servers}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    sites, gateway_site = _sites(claude_json, project_mcp)
    plan = plan_interposition(
        sites=sites,
        gateway_site=gateway_site,
        gateway_entry=_GATEWAY,
        cleared_config=cleared_config,
    )
    return claude_json, project_mcp, plan


class TestRoundTrip:
    def test_unwrap_restores_the_file_byte_for_byte(self, tmp_path: Path) -> None:
        claude_json, _, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        before = claude_json.read_bytes()

        backup = apply_interposition(
            plan, wrapped_at="2026-07-27T00:00:00+00:00", project=Path(_PROJECT), harness="claude-code"
        )
        assert claude_json.read_bytes() != before

        restore(backup)
        assert claude_json.read_bytes() == before

    def test_the_wrapped_config_names_only_the_gateway(self, tmp_path: Path) -> None:
        claude_json, _, plan = _wrap(
            tmp_path, {"notes": {"command": "notes-server"}, "fetch": {"command": "fetch"}}
        )
        apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        document = json.loads(claude_json.read_text())
        assert document["projects"][_PROJECT]["mcpServers"] == {"tegh": _GATEWAY}

    def test_a_scope_that_had_no_block_ends_with_no_block(self, tmp_path: Path) -> None:
        """Not `{}` — the difference is what byte-identity means for user scope.

        User scope is included in every wrap (a user-scope server survives a
        local-only rewrite and would sit beside the gateway), so the common case
        is a site with nothing in it. Writing `{}` back would leave a key the
        file never had.
        """
        claude_json, _, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        before = claude_json.read_bytes()
        assert "mcpServers" not in json.loads(before)  # no USER-scope block

        backup = apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        restore(backup)
        assert "mcpServers" not in json.loads(claude_json.read_text())
        assert claude_json.read_bytes() == before

    def test_a_project_mcp_json_that_never_existed_is_not_created(self, tmp_path: Path) -> None:
        _, project_mcp, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        backup = apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        assert not project_mcp.exists()
        restore(backup)
        assert not project_mcp.exists()

    def test_every_configured_scope_is_cleared_not_just_the_local_one(
        self, tmp_path: Path
    ) -> None:
        """Scopes shadow rather than merge, and different NAMES at different
        scopes all load side by side — so a project-scope server left in place
        would sit beside the gateway and the wrap would not be a wrap."""
        _, project_mcp, plan = _wrap(
            tmp_path,
            {"notes": {"command": "notes-server"}},
            project_mcp_servers={"shared": {"command": "shared-server"}},
        )
        before = project_mcp.read_bytes()
        backup = apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        assert "mcpServers" not in json.loads(project_mcp.read_text())
        restore(backup)
        assert project_mcp.read_bytes() == before


class TestLiveStateSurvives:
    def test_telemetry_written_while_wrapped_is_not_rolled_back(self, tmp_path: Path) -> None:
        """`~/.claude.json` is a LIVE state file, so the backup holds the BLOCK.

        A whole-file snapshot would restore `lastSessionId` and friends to their
        pre-wrap values, silently discarding every session that ran while the
        project was wrapped.
        """
        claude_json, _, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        backup = apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )

        # The harness runs a session while wrapped.
        document = json.loads(claude_json.read_text())
        document["projects"][_PROJECT]["lastSessionId"] = "session-written-while-wrapped"
        document["numStartups"] = 1660
        claude_json.write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        restore(backup)
        after = json.loads(claude_json.read_text())
        assert after["projects"][_PROJECT]["lastSessionId"] == "session-written-while-wrapped"
        assert after["numStartups"] == 1660
        assert after["projects"][_PROJECT]["mcpServers"] == {"notes": {"command": "notes-server"}}

    def test_another_projects_entry_is_never_touched(self, tmp_path: Path) -> None:
        claude_json, _, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        document = json.loads(claude_json.read_text())
        assert document["projects"]["/Users/someone/Code/other"] == {"lastSessionId": "zzz"}


class TestRefusals:
    def test_an_unclassified_literal_refuses_the_wrap(self, tmp_path: Path) -> None:
        """Displacing it might copy a secret into tegh's backup — a second
        plaintext copy, not a boundary. tegh cannot tell which it is, so
        an unanswered value is refused."""
        # The literal is deliberately NOT shaped like a real vendor key. The gate
        # keys on "this is not a ${VAR} reference", never on the value's shape, so
        # a realistic-looking one buys no coverage — and it would trip the repo's
        # secret scanner, whose only remedy is exempting this file, which would
        # then never catch a real secret pasted here later.
        with pytest.raises(InlineSecretRefused) as excinfo:
            _wrap(tmp_path, {"api": {"command": "x", "env": {"API_KEY": "a-literal-value"}}})
        assert "env.API_KEY" in str(excinfo.value)

    def test_a_literal_header_refuses_too(self, tmp_path: Path) -> None:
        with pytest.raises(InlineSecretRefused):
            _wrap(
                tmp_path,
                {"api": {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer a-literal-value"}}},
            )

    def test_a_value_classified_as_configuration_passes(self, tmp_path: Path) -> None:
        """The human's answer is what clears the gate, per coordinate."""
        _, _, plan = _wrap(
            tmp_path,
            {"notes": {"command": "x", "env": {"MEMORY_FILE_PATH": "/tmp/m.json"}}},
            cleared_config=_cleared("local:notes.env.MEMORY_FILE_PATH", "/tmp/m.json"),
        )
        assert plan.displaced_count == 1

    def test_clearing_one_field_does_not_clear_its_neighbour(self, tmp_path: Path) -> None:
        """Per COORDINATE, never per server: a server can hold both a path and a
        credential, and clearing the path must not carry the other one through."""
        with pytest.raises(InlineSecretRefused) as excinfo:
            _wrap(
                tmp_path,
                {
                    "notes": {
                        "command": "x",
                        "env": {"MEMORY_FILE_PATH": "/tmp/m.json", "TOKEN": "a-literal-value"},
                    }
                },
                cleared_config=_cleared("local:notes.env.MEMORY_FILE_PATH", "/tmp/m.json"),
            )
        assert "env.TOKEN" in str(excinfo.value)
        assert "MEMORY_FILE_PATH" not in str(excinfo.value)

    def test_a_clearance_for_another_server_does_not_transfer(self, tmp_path: Path) -> None:
        """The coordinate is server-qualified, so the same variable name on a
        second server is a second question."""
        with pytest.raises(InlineSecretRefused):
            _wrap(
                tmp_path,
                {
                    "notes": {"command": "x", "env": {"MEMORY_FILE_PATH": "/tmp/a.json"}},
                    "other": {"command": "y", "env": {"MEMORY_FILE_PATH": "/tmp/b.json"}},
                },
                cleared_config=_cleared("local:notes.env.MEMORY_FILE_PATH", "/tmp/m.json"),
            )

    def test_a_value_that_moved_since_it_was_classified_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The clearance carries a DIGEST, not just a name — and the gate compares it.

        This function reads the config fresh, and it runs at the END of a wrap:
        after snapshot, review, admission, the lock write and grant seeding. So
        the value it is about to copy into tegh's backup is not necessarily the
        one anyone classified. `~/.claude.json` is live state this repo has
        OBSERVED changing mid-session, and the wrapped agent has a shell.

        Matching on the coordinate alone would make the clearance a name
        heuristic with the whole ceremony as its window — precisely what the
        classification step refused to be, arrived at by a slower route.
        """
        with pytest.raises(InlineSecretRefused) as excinfo:
            _wrap(
                tmp_path,
                {"notes": {"command": "x", "env": {"MEMORY_FILE_PATH": "swapped-value"}}},
                cleared_config=_cleared(
                    "local:notes.env.MEMORY_FILE_PATH", "/tmp/what-was-classified.json"
                ),
            )
        # Named as a change, not as a first-time omission: the two are different
        # situations and an operator who classified this value needs to know it
        # moved under them.
        assert "CHANGED during this wrap" in str(excinfo.value)

    def test_the_gate_defaults_to_refusing(self, tmp_path: Path) -> None:
        """`cleared_config` defaults to empty, so a caller that forgets to ask
        gets the blanket refusal rather than a silent copy."""
        with pytest.raises(InlineSecretRefused):
            _wrap(tmp_path, {"api": {"command": "x", "env": {"K": "a-literal-value"}}})

    @pytest.mark.parametrize("value", ["${API_KEY}", "  ${API_KEY:-default}  "])
    def test_a_variable_reference_is_not_a_secret(self, tmp_path: Path, value: str) -> None:
        """The harness expands these from the environment at load; the config
        holds a name, not material."""
        _, _, plan = _wrap(tmp_path, {"api": {"command": "x", "env": {"API_KEY": value}}})
        assert plan.displaced_count == 1

    def test_a_config_tegh_cannot_reproduce_refuses_rather_than_reformatting(
        self, tmp_path: Path
    ) -> None:
        """Every write is parse -> modify -> re-serialize. Against a file whose
        style the serializer does not reproduce, that rewrites the WHOLE file to
        change a few lines — so the round-trip is proven before any edit."""
        path = tmp_path / ".claude.json"
        path.write_text('{"projects":{"' + _PROJECT + '":{"mcpServers":{}}}}', encoding="utf-8")
        sites, gateway_site = _sites(path, tmp_path / "nope" / ".mcp.json")
        with pytest.raises(ConfigFormatUnreproducible):
            plan_interposition(sites=sites, gateway_site=gateway_site, gateway_entry=_GATEWAY)

    def test_invalid_json_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude.json"
        path.write_text("{not json", encoding="utf-8")
        sites, gateway_site = _sites(path, tmp_path / "nope" / ".mcp.json")
        with pytest.raises(Exception, match="not valid JSON"):
            plan_interposition(sites=sites, gateway_site=gateway_site, gateway_entry=_GATEWAY)


class TestBackupSerialization:
    def test_a_backup_round_trips_through_json(self, tmp_path: Path) -> None:
        _, _, plan = _wrap(tmp_path, {"notes": {"command": "notes-server"}})
        backup = apply_interposition(
            plan, wrapped_at="t", project=Path(_PROJECT), harness="claude-code"
        )
        again = WrapBackup.from_json(backup.to_json())
        assert again.sites[0].block == backup.sites[0].block
        assert [s.block_existed for s in again.sites] == [s.block_existed for s in backup.sites]


class TestGatewayEntry:
    def test_the_entry_carries_no_credential_material(self) -> None:
        """The whole point of the launcher indirection: the harness config the
        wrapped agent can read names a command and a path, and nothing else."""
        entry = claude_code.gateway_entry(
            _PROJECT, launcher=["/usr/local/bin/tegh"], home="/home/someone/.tegh"
        )
        assert set(entry) == {"command", "args"}
        assert "env" not in entry and "headers" not in entry

    def test_a_module_launcher_is_carried_whole(self) -> None:
        """An editable checkout has no `tegh` on PATH, so the launcher is a full
        argv; dropping its `-m` prefix would write an unlaunchable entry."""
        entry = claude_code.gateway_entry(
            _PROJECT,
            launcher=["/venv/bin/python", "-m", "tegh.cli"],
            home="/home/someone/.tegh",
        )
        assert entry["command"] == "/venv/bin/python"
        assert entry["args"][:3] == ["-m", "tegh.cli", "gateway"]

    def test_the_entry_names_its_tegh_home(self) -> None:
        """A spawned stdio child gets a minimal environment, so TEGH_HOME set in
        the operator's shell at wrap time is absent at spawn time. Inherited
        resolution is missing resolution."""
        entry = claude_code.gateway_entry(
            _PROJECT, launcher=["/usr/local/bin/tegh"], home="/home/someone/.tegh"
        )
        assert entry["args"][-2] == "--home"
        # Absolute, not compared literally: `resolve()` follows macOS firmlinks,
        # so pinning the exact string would assert the platform's path semantics
        # rather than this function's contract.
        written = Path(entry["args"][-1])
        assert written.is_absolute() and written.name == ".tegh"

    def test_the_gateway_lands_at_local_scope(self) -> None:
        """Not project scope: `.mcp.json` is committable and would be shared,
        and a project-scope server sits `⏸ Pending approval` until a human
        accepts it interactively."""
        _, gateway_site = claude_code.config_sites(_PROJECT)
        assert gateway_site.scope is ConfigScope.LOCAL
