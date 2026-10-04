"""Lock I/O, the TL1 structural guard, and the local store's refusals.

The centrepiece is `TestTL1NoRuntimeReader`. TL1 — the lock is never read by
the per-call path — is the invariant that makes `tegh.lock` safe to put in a
directory the wrapped agent can write, and it is the one a convenient
implementation destroys silently: wiring `parse_lock_bytes` into the connect
path is the obvious shortcut and it hands an injected agent both keys of
two-key admission. A docstring cannot hold that line, so it is asserted.

AWS-free, crypto-free except where the store mints its own keys.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from safe_agents.broker.schemas import McpToolDef, ToolOp, compute_tool_def_hash
from tegh.lock import (
    LOCK_FORMAT_VERSION,
    AttestationKind,
    Harness,
    LockAttestation,
    LockedServer,
    LockedTool,
    LockSignature,
    TeghLock,
    canonical_lock_bytes,
)
from tegh.lockfile import (
    LOCK_FILENAME,
    SIGNATURE_FILENAME,
    LockSignatureInvalid,
    UnsignedLockRefused,
    lock_paths,
    read_lock,
    write_lock,
)
from tegh.store import TeghStore, TeghStoreError, project_slug, provision

_STAMP = "2026-07-25T14:03:11+00:00"


def _tool(name: str = "read_note") -> LockedTool:
    definition = McpToolDef(
        server_id="notes",
        tool_name=name,
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        description="Read a note.",
    )
    return LockedTool(
        tool_def=definition,
        def_hash=compute_tool_def_hash(definition),
        tool_op=ToolOp(tool="notes", op=name, effect="read", external=False),
        attestation=LockAttestation(
            kind=AttestationKind.SOLO_ATTESTED, admitted_by="local-solo:x", admitted_at=_STAMP
        ),
    )


def _lock(*, admitted: bool = True) -> TeghLock:
    return TeghLock(
        format_version=LOCK_FORMAT_VERSION,
        generated_at=_STAMP,
        servers=[
            LockedServer(
                server_id="notes",
                harness=Harness.CLAUDE_CODE,
                scope="project",
                transport="stdio",
                command="python",
                args=["server.py"],
                discovered_at=_STAMP,
                admitted=[_tool()] if admitted else [],
            )
        ],
    )


def _signer(payload: bytes) -> LockSignature:
    return LockSignature(key_id="k1", algorithm="ed25519", value="c2ln")


class TestTL1NoRuntimeReader:
    """No module under `safe_agents/broker/` may import tegh, at all.

    Stated as a whole-package ban rather than "must not import `lockfile`"
    deliberately: the danger is not one function name, it is the broker growing
    ANY dependency on a project-tree artifact. A guard naming one module would
    pass the day someone imports `tegh.lock` instead.
    """

    #: The INSTALLED platform broker (the pinned `safe-agents` dependency), which
    #: is the only place broker code exists for this suite to sweep. Located by
    #: spec rather than by import, because the import-boundary guard in
    #: test_lock.py forbids tegh importing `safe_agents.broker` itself.
    _BROKER = Path(
        importlib.util.find_spec("safe_agents.broker").submodule_search_locations[0]
    ).resolve()

    def test_sweep_reaches_the_broker(self) -> None:
        """A sweep over an empty or wrong directory passes vacuously."""
        assert (self._BROKER / "schemas").exists() or (self._BROKER / "schemas.py").exists()
        assert len(list(self._BROKER.rglob("*.py"))) > 10

    def test_no_broker_module_imports_tegh(self) -> None:
        offenders: list[str] = []
        for path in self._BROKER.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover — not our concern here
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(name == "tegh" or name.startswith("tegh.") for name in names):
                    offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], (
            "the broker imports tegh: "
            + ", ".join(offenders)
            + ". TL1 — the lock lives where the wrapped agent can write, so a "
            "broker that reads it hands an injected agent both keys of two-key "
            "admission. Authority stays store-side."
        )


class TestWriterIsDeterministic:
    """TL2a — a re-export that changed nothing must produce an empty git diff,
    or reviewers learn to skim the one artifact whose value is that it is read."""

    def test_identical_content_serializes_identically(self) -> None:
        assert canonical_lock_bytes(_lock()) == canonical_lock_bytes(_lock())

    def test_server_and_tool_order_is_normalized_at_construction(self) -> None:
        forward = TeghLock(
            format_version=LOCK_FORMAT_VERSION,
            generated_at=_STAMP,
            servers=[
                LockedServer(
                    server_id="notes",
                    harness=Harness.CLAUDE_CODE,
                    scope="project",
                    transport="stdio",
                    discovered_at=_STAMP,
                    admitted=[_tool("a_tool"), _tool("z_tool")],
                )
            ],
        )
        backward = TeghLock(
            format_version=LOCK_FORMAT_VERSION,
            generated_at=_STAMP,
            servers=[
                LockedServer(
                    server_id="notes",
                    harness=Harness.CLAUDE_CODE,
                    scope="project",
                    transport="stdio",
                    discovered_at=_STAMP,
                    admitted=[_tool("z_tool"), _tool("a_tool")],
                )
            ],
        )
        assert canonical_lock_bytes(forward) == canonical_lock_bytes(backward)

    def test_the_file_stays_diffable(self, tmp_path: Path) -> None:
        """Pretty-printed on purpose: TL2's sidecar exists so the lock itself
        can stay reviewable in a code-review diff."""
        write_lock(_lock(), project=tmp_path, allow_unsigned=True)
        text = (tmp_path / LOCK_FILENAME).read_text(encoding="utf-8")
        assert text.endswith("\n") and "\n  " in text


class TestUnsignedPosture:
    """TL3 — optional at schema, required at ceremony."""

    def test_writing_unsigned_is_refused_by_default(self, tmp_path: Path) -> None:
        with pytest.raises(UnsignedLockRefused):
            write_lock(_lock(), project=tmp_path)
        assert not (tmp_path / LOCK_FILENAME).exists(), "a refused write must write nothing"

    def test_the_override_is_explicit(self, tmp_path: Path) -> None:
        lock_path, signature_path = write_lock(_lock(), project=tmp_path, allow_unsigned=True)
        assert lock_path.exists() and signature_path is None

    def test_a_signed_write_produces_the_sidecar(self, tmp_path: Path) -> None:
        _, signature_path = write_lock(_lock(), project=tmp_path, signer=_signer)
        assert signature_path is not None and signature_path.name == SIGNATURE_FILENAME

    def test_a_forced_unsigned_write_removes_a_stale_sidecar(self, tmp_path: Path) -> None:
        """A signature over bytes that are no longer there verifies as a tamper
        and teaches the reader to disbelieve the alarm."""
        write_lock(_lock(), project=tmp_path, signer=_signer)
        _, signature_path = lock_paths(tmp_path)
        assert signature_path.exists()

        write_lock(_lock(admitted=False), project=tmp_path, allow_unsigned=True)
        assert not signature_path.exists()


class TestReadOrdering:
    def test_absent_lock_is_none_not_an_error(self, tmp_path: Path) -> None:
        """'this project is not wrapped' is a normal state `status` reports."""
        assert read_lock(tmp_path) is None

    def test_round_trip(self, tmp_path: Path) -> None:
        write_lock(_lock(), project=tmp_path, allow_unsigned=True)
        loaded = read_lock(tmp_path)
        assert loaded is not None
        assert loaded.lock.servers[0].admitted[0].tool_def.tool_name == "read_note"
        assert loaded.verified is None and not loaded.is_signed

    def test_a_sidecar_without_a_verifier_is_never_verified_by_default(
        self, tmp_path: Path
    ) -> None:
        """`verified` is three-state on purpose: collapsing 'checked out' and
        'nothing to check' is how an unsigned artifact reads as a signed one."""
        write_lock(_lock(), project=tmp_path, signer=_signer)
        loaded = read_lock(tmp_path)
        assert loaded is not None and loaded.is_signed and loaded.verified is None

    def test_verification_happens_before_parsing(self, tmp_path: Path) -> None:
        """A lock whose signature fails is not a lock with a problem — it is
        bytes of unknown origin, and must not be parsed into a usable model."""
        write_lock(_lock(), project=tmp_path, signer=_signer)
        seen: list[bytes] = []

        def reject(raw: bytes, signature: LockSignature) -> bool:
            seen.append(raw)
            return False

        with pytest.raises(LockSignatureInvalid):
            read_lock(tmp_path, verifier=reject)
        assert seen and seen[0] == (tmp_path / LOCK_FILENAME).read_bytes()

    def test_a_valid_signature_reports_verified(self, tmp_path: Path) -> None:
        write_lock(_lock(), project=tmp_path, signer=_signer)
        loaded = read_lock(tmp_path, verifier=lambda raw, sig: True)
        assert loaded is not None and loaded.verified is True

    def test_a_tampered_lock_is_caught(self, tmp_path: Path) -> None:
        """The basis is the file's literal bytes (TL2), so any edit moves it."""
        write_lock(_lock(), project=tmp_path, signer=_signer)
        lock_path, _ = lock_paths(tmp_path)
        original = lock_path.read_bytes()
        lock_path.write_bytes(original.replace(b'"read"', b'"write"'))

        with pytest.raises(LockSignatureInvalid):
            read_lock(tmp_path, verifier=lambda raw, sig: raw == original)


class TestStoreProvisioning:
    def test_secrets_are_owner_only(self, tmp_path: Path) -> None:
        store = provision(tmp_path / "home")
        for path in (store.hmac_key_path, store.issuer_key_path):
            mode = path.stat().st_mode
            assert not mode & (stat.S_IRWXG | stat.S_IRWXO), f"{path} is readable beyond owner"

    def test_the_public_key_is_readable(self, tmp_path: Path) -> None:
        """A public key kept at 0600 is a public key nobody can use."""
        store = provision(tmp_path / "home")
        assert (store.home / "issuer.pub").read_text().startswith("-----BEGIN PUBLIC KEY-----")

    def test_reprovisioning_refuses_rather_than_orphaning(self, tmp_path: Path) -> None:
        """Re-minting the HMAC key quarantines every admitted row; re-minting
        the issuer key breaks attribution for every signed record. Both are
        quiet failures, so the refusal is loud."""
        provision(tmp_path / "home")
        with pytest.raises(TeghStoreError, match="already exists"):
            provision(tmp_path / "home")

    def test_a_loosened_secret_is_refused_at_use(self, tmp_path: Path) -> None:
        store = provision(tmp_path / "home")
        os.chmod(store.hmac_key_path, 0o644)
        with pytest.raises(TeghStoreError, match="readable beyond its owner"):
            store.hmac_key()

    def test_an_unprovisioned_home_is_not_provisioned(self, tmp_path: Path) -> None:
        assert not TeghStore(home=tmp_path / "nope").is_provisioned


class TestCeremonyEnvironment:
    def test_every_authority_value_is_named(self, tmp_path: Path) -> None:
        """The base refuses defaults for exactly these: a durable store
        under an unnamed key is unnamed authority whatever the backend."""
        store = provision(tmp_path / "home")
        env = store.ceremony_env(role="maker", project=tmp_path / "proj")
        assert env["BROKER_STORE"] == "sqlite"
        assert env["BROKER_SQLITE_PATH"] == str(store.db_path)
        assert env["BROKER_HMAC_KEY"] == store.hmac_key()
        assert env["BROKER_LOCAL_IDENTITY"] == "maker"
        assert env["ISSUER_SIGNING_KEY_FILE"] == str(store.issuer_key_path)
        assert env["ISSUER_SIGNING_KEY_ID"] == store.issuer_key_id()

    def test_maker_and_checker_are_different_identities(self, tmp_path: Path) -> None:
        """maker != checker is what the base's ceremony compares; a single role
        for both halves would make self-ratification structurally possible."""
        store = provision(tmp_path / "home")
        maker = store.ceremony_env(role="maker", project=tmp_path)
        checker = store.ceremony_env(role="checker", project=tmp_path)
        assert maker["BROKER_LOCAL_IDENTITY"] != checker["BROKER_LOCAL_IDENTITY"]

    def test_an_ambient_aws_signing_source_is_cleared(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """The base refuses when both key sources are set — correctly, since
        picking one silently would attribute records to an unintended key."""
        monkeypatch.setenv("ISSUER_SIGNING_KEY_SECRET_ARN", "arn:aws:secretsmanager:...")
        store = provision(tmp_path / "home")
        env = store.ceremony_env(role="maker", project=tmp_path)
        assert "ISSUER_SIGNING_KEY_SECRET_ARN" not in env


class TestProjectSlug:
    def test_two_checkouts_of_one_name_do_not_collide(self, tmp_path: Path) -> None:
        """Different paths are different wraps — different discovered scopes and
        possibly different admitted sets. Collapsing them would let one
        project's admissions govern the other."""
        first = tmp_path / "a" / "repo"
        second = tmp_path / "b" / "repo"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        assert project_slug(first) != project_slug(second)

    def test_the_readable_half_survives(self, tmp_path: Path) -> None:
        target = tmp_path / "my-project"
        target.mkdir()
        assert project_slug(target).startswith("my-project-")
