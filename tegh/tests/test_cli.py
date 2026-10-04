"""The `tegh` command surface.

The load-bearing test here is `TestSynthesizedManifestIsAccepted`: `wrap`
writes key #1 as an `AgentManifest` and the base parses it back, so a manifest
this module can produce but the base refuses is a wrap that dies at the first
snapshot. The live drill found exactly that (a constructible server must also
be wired in `connectors`), which is a bug a renderer test would never catch —
so the guard validates against the real model rather than against a fixture of
what this module happens to emit.

The subprocess ceremony itself is NOT re-tested here; it is the base's, with
its own suite. What is tested is tegh's half: what it hands the ceremony, and
what it refuses to do before reaching it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from safe_agents.broker.schemas.manifest import AgentManifest
from tegh.cli import (
    ADAPTERS,
    HARNESS_ALIASES,
    _synthesize_manifest,
    main,
)
from tegh.discovery import ActivationState, ConfigScope, DiscoveredServer
from tegh.lock import Harness

_STAMP = "2026-07-25T14:03:11+00:00"


def _stdio(server_id: str = "notes") -> DiscoveredServer:
    return DiscoveredServer(
        server_id=server_id,
        scope=ConfigScope.PROJECT,
        transport="stdio",
        command="python",
        args=["server.py"],
        discovered_at=_STAMP,
        activation=ActivationState.ACTIVE,
        source_path="/proj/.mcp.json",
    )


def _remote(server_id: str = "ctx7") -> DiscoveredServer:
    return DiscoveredServer(
        server_id=server_id,
        scope=ConfigScope.USER,
        transport="http",
        url="https://mcp.example.com/mcp",
        discovered_at=_STAMP,
        activation=ActivationState.ACTIVE,
        source_path="/home/.claude.json",
    )


#: The project/cap context every synthesis now takes. A wrapped project's
#: manifest carries a principal DERIVED from its path and a NAMED daily cap:
#: without the first the manifest cannot build a runtime at all, and without
#: the second the base refuses to default a granted write's budget.
_MANIFEST_CTX = {"project": Path("/proj"), "daily_cap": 200}


class TestSynthesizedManifestIsAccepted:
    """Key #1 must parse as a real AgentManifest — the drill's actual bug."""

    def test_a_namespace_only_pass_validates(self) -> None:
        manifest = AgentManifest.model_validate(
            _synthesize_manifest([_stdio()], polarity="abstain", **_MANIFEST_CTX)
        )
        assert manifest.mcp_servers["notes"].command == "python"
        assert manifest.mcp_servers["notes"].tools == []

    def test_a_constructible_server_is_wired_as_a_connector(self) -> None:
        """The base refuses a construction block nothing wires: a server the
        manifest can spawn but no connector reaches is dead config that reads
        like a live capability."""
        payload = _synthesize_manifest([_stdio(), _remote()], polarity="abstain", **_MANIFEST_CTX)
        assert sorted(payload["connectors"]) == ["ctx7", "notes"]
        AgentManifest.model_validate(payload)

    def test_a_remote_server_validates_with_its_url(self) -> None:
        manifest = AgentManifest.model_validate(
            _synthesize_manifest([_remote()], polarity="abstain", **_MANIFEST_CTX)
        )
        decl = manifest.mcp_servers["ctx7"]
        assert decl.transport == "streamable-http" and decl.command is None

    def test_the_confirmed_pass_carries_tools_and_tool_ops(self) -> None:
        from safe_agents.broker.schemas import McpToolDef, ToolOp

        definition = McpToolDef(
            server_id="notes",
            tool_name="read_note",
            input_schema={"type": "object"},
            description="Read.",
        )
        # external=True is REQUIRED of every declared MCP tool by the base.
        op = ToolOp(tool="notes", op="read_note", effect="read", external=True)
        manifest = AgentManifest.model_validate(
            _synthesize_manifest(
                [_stdio()],
                polarity="abstain",
                tools_by_server={"notes": [(definition, op)]},
                **_MANIFEST_CTX,
            )
        )
        assert [t.tool_name for t in manifest.mcp_servers["notes"].tools] == ["read_note"]
        assert manifest.tool_ops[0].op == "read_note"

    @pytest.mark.parametrize("polarity", ["abstain", "act"])
    def test_polarity_is_carried_not_defaulted_in_this_module(self, polarity: str) -> None:
        """The safe-default polarity is re-derived per agent by rule; baking one
        in here would be the latent safety bug the platform rules out by
        holding no polarity of its own."""
        payload = _synthesize_manifest([_stdio()], polarity=polarity, **_MANIFEST_CTX)
        assert payload["envelope"]["polarity"] == polarity


class TestHarnessSelection:
    def test_the_friendly_alias_resolves_to_the_frozen_catalog_value(self) -> None:
        assert HARNESS_ALIASES["claude"] is Harness.CLAUDE_CODE
        assert HARNESS_ALIASES["claude-code"] is Harness.CLAUDE_CODE

    def test_every_alias_has_an_adapter(self) -> None:
        assert all(harness in ADAPTERS for harness in HARNESS_ALIASES.values())


class TestRefusals:
    def test_wrap_refuses_without_a_provisioned_home(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        code = main(
            ["wrap", "claude", "--project", str(project), "--home", str(tmp_path / "nohome")]
        )
        assert code == 2
        assert "tegh init" in capsys.readouterr().err

    def test_status_on_an_unwrapped_project_is_not_an_error_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code = main(["status", "--project", str(tmp_path)])
        assert code == 1
        assert "not wrapped" in capsys.readouterr().out

    def test_unwrapped_verdict_does_not_depend_on_the_tegh_home(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Whether a project is wrapped is a fact about the PROJECT.

        Regression: resolving the signature verifier before checking for a lock
        made this answer read the local tegh home, so `status` on an unwrapped
        project varied with unrelated machine state (and, in a test, reached the
        developer's real `~/.tegh`).
        """
        code = main(
            ["status", "--project", str(tmp_path), "--home", str(tmp_path / "absent-home")]
        )
        assert code == 1
        assert "not wrapped" in capsys.readouterr().out


class TestStatusReportsSignatureState:
    """`status` through `main()` on each of the three provenance states.

    These exist because the signing wiring was added at three call sites and the
    CLI suite was green without exercising any of them — a signer nothing calls
    end-to-end is indistinguishable from a stub (second-arm blindness).
    The write half's happy path needs the real ceremony and is covered by the
    live drill; the read half is fully reachable here.
    """

    @staticmethod
    def _wrapped(tmp_path: Path, *, sign: bool = True):
        from tegh.lockfile import write_lock
        from tegh.signing import signer_from_pem
        from tegh.store import provision
        from tegh.tests.test_signing import _lock

        home = provision(tmp_path / "home")
        project = tmp_path / "proj"
        project.mkdir()
        signer = (
            signer_from_pem(home.issuer_key_id(), home.issuer_signing_pem())
            if sign
            else None
        )
        write_lock(_lock(), project=project, signer=signer, allow_unsigned=not sign)
        return home, project

    def test_a_signed_lock_reports_verified(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        home, project = self._wrapped(tmp_path)
        code = main(["status", "--project", str(project), "--home", str(home.home)])
        assert code == 0
        assert "VERIFIED" in capsys.readouterr().out

    def test_an_unsigned_lock_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        home, project = self._wrapped(tmp_path, sign=False)
        code = main(["status", "--project", str(project), "--home", str(home.home)])
        assert code == 0
        assert "ABSENT" in capsys.readouterr().out

    def test_a_tampered_lock_exits_3_and_says_tamper(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from tegh.lockfile import lock_paths

        home, project = self._wrapped(tmp_path)
        lock_path, _ = lock_paths(project)
        lock_path.write_bytes(lock_path.read_bytes().replace(b"Read a note.", b"Owned."))
        code = main(["status", "--project", str(project), "--home", str(home.home)])
        assert code == 3
        assert "TAMPER" in capsys.readouterr().err

    def test_an_unknown_signer_exits_4_not_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """The distinct exit code is the point: a script must tell these apart.

        A lock from another machine is not a modified lock, and collapsing them
        onto one code would make "TAMPER" the routine outcome of ordinary
        single-player use.
        """
        home, project = self._wrapped(tmp_path)
        # A second home has a different key_id AND a different key, so this
        # lock's signer is simply unknown here.
        other = tmp_path / "other-home"
        from tegh.store import provision

        provision(other)
        code = main(["status", "--project", str(project), "--home", str(other)])
        assert code == 4
        assert "UNVERIFIABLE" in capsys.readouterr().err
