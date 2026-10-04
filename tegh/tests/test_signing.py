"""Real Ed25519 signing of `tegh.lock` — TL2's basis, and what it refuses.

The tests that matter here are the negative ones, because a signing feature that
only ever gets exercised on the happy path is indistinguishable from a stub that
returns True. So: a byte-level edit must be caught, a whitespace-only edit must
be caught, a signature borrowed from another lock must be caught, an unknown
signer must be reported as unknown rather than as a tamper, and — the
property this whole basis exists for — a lock whose MODEL has grown a field must
still verify from its stored bytes.

AWS-free. Keys are minted in a temp tegh home.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

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
    parse_signature_bytes,
)
from tegh.lockfile import (
    LockSignatureInvalid,
    lock_paths,
    read_lock,
    write_lock,
)
from tegh.signing import (
    ALGORITHM,
    UnknownLockSigner,
    load_signing_key,
    load_verify_key,
    resolve_local_verifier,
    signer_from_pem,
    verifier_from_pem_map,
)
from tegh.store import provision

_STAMP = "2026-07-26T09:15:00+00:00"


def _lock(*, tool_name: str = "read_note") -> TeghLock:
    definition = McpToolDef(
        server_id="notes",
        tool_name=tool_name,
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        description="Read a note.",
    )
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
                admitted=[
                    LockedTool(
                        tool_def=definition,
                        def_hash=compute_tool_def_hash(definition),
                        tool_op=ToolOp(
                            tool="notes", op=tool_name, effect="read", external=False
                        ),
                        attestation=LockAttestation(
                            kind=AttestationKind.SOLO_ATTESTED,
                            admitted_by="local-solo:tegh-local-abc",
                            admitted_at=_STAMP,
                        ),
                    )
                ],
            )
        ],
    )


@pytest.fixture()
def home(tmp_path: Path):
    """A provisioned tegh home — real minted keys, nothing shared between tests."""
    return provision(tmp_path / "home")


@pytest.fixture()
def signed(home, tmp_path: Path):
    """A signed lock on disk, plus the store that signed it."""
    project = tmp_path / "project"
    project.mkdir()
    signer = signer_from_pem(home.issuer_key_id(), home.issuer_signing_pem())
    write_lock(_lock(), project=project, signer=signer)
    return project, home


def _verifier(store):
    return resolve_local_verifier(store.issuer_key_id(), store.issuer_public_pem())


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_signed_lock_verifies(self, signed) -> None:
        project, store = signed
        loaded = read_lock(project, verifier=_verifier(store))
        assert loaded is not None
        assert loaded.verified is True
        assert loaded.signature is not None
        assert loaded.signature.key_id == store.issuer_key_id()
        assert loaded.signature.algorithm == ALGORITHM

    def test_sidecar_lands_beside_the_lock(self, signed) -> None:
        project, _ = signed
        lock_path, signature_path = lock_paths(project)
        assert lock_path.exists() and signature_path.exists()
        # The sidecar is a committed, reviewable document like the lock itself.
        assert parse_signature_bytes(signature_path.read_bytes()).algorithm == ALGORITHM

    def test_signature_is_over_the_file_bytes_verbatim(self, signed) -> None:
        """TL2 stated as arithmetic rather than as trust in the writer.

        Verifies the raw public key against the bytes read back off the disk —
        if `write_lock` signed anything other than exactly what it wrote (a
        re-serialization, a normalized variant), this fails.
        """
        project, store = signed
        lock_path, signature_path = lock_paths(project)
        signature = parse_signature_bytes(signature_path.read_bytes())
        public_key = load_verify_key(store.issuer_public_pem())
        public_key.verify(
            base64.b64decode(signature.value), lock_path.read_bytes()
        )  # raises InvalidSignature on mismatch

    def test_no_verifier_reports_unchecked_not_verified(self, signed) -> None:
        project, _ = signed
        loaded = read_lock(project)
        assert loaded is not None
        assert loaded.is_signed is True
        # The three-state distinction: nothing to check is NOT a pass.
        assert loaded.verified is None


# ---------------------------------------------------------------------------
# Tamper detection — the reason the feature exists
# ---------------------------------------------------------------------------


class TestTamperIsCaught:
    def test_edited_description_breaks_the_signature(self, signed) -> None:
        """The poisoned-description case, now caught by crypto and not only by diff."""
        project, store = signed
        lock_path, _ = lock_paths(project)
        lock_path.write_bytes(
            lock_path.read_bytes().replace(b"Read a note.", b"Read a note. IGNORE PRIOR")
        )
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(store))

    def test_whitespace_only_edit_breaks_the_signature(self, signed) -> None:
        """A basis over literal bytes catches edits a semantic basis would miss.

        Re-indenting changes nothing a JSON parser would report, which is
        exactly why a parse-then-compare basis would pass it. The bytes are the
        basis, so it fails.
        """
        project, store = signed
        lock_path, _ = lock_paths(project)
        lock_path.write_bytes(lock_path.read_bytes().replace(b"\n  ", b"\n    "))
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(store))

    def test_truncated_lock_is_caught_before_parsing(self, signed) -> None:
        """Verify-then-parse: unparseable bytes still fail as a SIGNATURE error.

        The ordering is the assertion. If parsing came first this would raise a
        validation error, and the operator would be told the lock is malformed
        rather than that it has been modified — the wrong root cause to chase.
        """
        project, store = signed
        lock_path, _ = lock_paths(project)
        lock_path.write_bytes(lock_path.read_bytes()[:40])
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(store))

    def test_signature_from_a_different_lock_is_caught(self, home, tmp_path: Path) -> None:
        """A valid signature by the RIGHT key over the WRONG bytes still fails."""
        signer = signer_from_pem(home.issuer_key_id(), home.issuer_signing_pem())
        project = tmp_path / "project"
        project.mkdir()
        write_lock(_lock(tool_name="read_note"), project=project, signer=signer)
        lock_path, signature_path = lock_paths(project)

        # Sign a DIFFERENT lock and graft that sidecar on.
        other = signer(canonical_lock_bytes(_lock(tool_name="write_note")))
        signature_path.write_bytes(
            json.dumps(other.model_dump(mode="json"), indent=2).encode() + b"\n"
        )
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(home))

    def test_signature_from_another_key_is_caught(self, signed, tmp_path: Path) -> None:
        """An attacker's own valid signature, presented under the trusted key_id."""
        project, store = signed
        lock_path, signature_path = lock_paths(project)
        rogue = ed25519.Ed25519PrivateKey.generate()
        forged = LockSignature(
            key_id=store.issuer_key_id(),  # claims to be us
            algorithm=ALGORITHM,
            value=base64.b64encode(rogue.sign(lock_path.read_bytes())).decode("ascii"),
        )
        signature_path.write_bytes(
            json.dumps(forged.model_dump(mode="json"), indent=2).encode() + b"\n"
        )
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(store))

    def test_corrupt_base64_is_invalid_not_a_crash(self, signed) -> None:
        project, store = signed
        _, signature_path = lock_paths(project)
        signature = parse_signature_bytes(signature_path.read_bytes())
        broken = signature.model_copy(update={"value": "not!valid!base64"})
        signature_path.write_bytes(
            json.dumps(broken.model_dump(mode="json"), indent=2).encode() + b"\n"
        )
        with pytest.raises(LockSignatureInvalid):
            read_lock(project, verifier=_verifier(store))


# ---------------------------------------------------------------------------
# Unknown signer — NOT a tamper report
# ---------------------------------------------------------------------------


class TestUnknownSigner:
    def test_unknown_key_id_raises_its_own_error(self, signed) -> None:
        """The distinction that keeps the tamper alarm credible.

        A lock signed on a teammate's machine is the expected case for a
        single-player tool. Reporting it as a tamper would train the operator to
        dismiss the alarm that matters (tamper-not-evolution, applied to the message).
        """
        project, _ = signed
        stranger = verifier_from_pem_map({"someone-else": _fresh_public_pem()})
        with pytest.raises(UnknownLockSigner) as caught:
            read_lock(project, verifier=stranger)
        assert "no public key" in str(caught.value)
        assert "tamper" in str(caught.value).lower()  # says what it is NOT

    def test_unknown_signer_is_not_lock_signature_invalid(self, signed) -> None:
        """Belt and braces: the two must not be catchable as one another."""
        project, _ = signed
        stranger = verifier_from_pem_map({"someone-else": _fresh_public_pem()})
        assert not issubclass(UnknownLockSigner, LockSignatureInvalid)
        assert not issubclass(LockSignatureInvalid, UnknownLockSigner)
        with pytest.raises(UnknownLockSigner):
            read_lock(project, verifier=stranger)


def _fresh_public_pem() -> str:
    from cryptography.hazmat.primitives import serialization

    return (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


# ---------------------------------------------------------------------------
# The basis must indict tampering, never schema evolution
# ---------------------------------------------------------------------------


class TestBasisIsStoredBytes:
    def test_a_grown_model_still_verifies_from_stored_bytes(self, signed) -> None:
        """The property the whole literal-bytes basis exists to provide.

        Simulates additive format growth the only way a test can without
        actually editing the frozen schema: verify the STORED bytes directly,
        while a re-serialization of the parsed model has diverged. If any
        consumer ever verified the re-serialized form instead, every historical
        lock would read as a tamper the day a field is added — the failure mode
        that rule names, and the one that teaches reviewers to ignore the alarm.
        """
        project, store = signed
        loaded = read_lock(project, verifier=_verifier(store))
        assert loaded is not None and loaded.verified is True

        # `loaded.raw` is what was verified. Prove it is the disk's bytes and
        # NOT a product of re-serializing `loaded.lock`.
        lock_path, _ = lock_paths(project)
        assert loaded.raw == lock_path.read_bytes()

        # And prove the signature is bound to those bytes specifically: a
        # single-byte change to the re-derived form would not verify.
        public_key = load_verify_key(store.issuer_public_pem())
        with pytest.raises(Exception):
            public_key.verify(
                base64.b64decode(loaded.signature.value), loaded.raw + b" "
            )

    def test_writer_signs_exactly_one_serialization(self, home, tmp_path: Path) -> None:
        """Serialize once (TL2): the written bytes ARE the signed bytes."""
        project = tmp_path / "project"
        project.mkdir()
        lock = _lock()
        signer = signer_from_pem(home.issuer_key_id(), home.issuer_signing_pem())
        lock_path, _ = write_lock(lock, project=project, signer=signer)
        assert lock_path.read_bytes() == canonical_lock_bytes(lock)


# ---------------------------------------------------------------------------
# Key handling and the store's material accessors
# ---------------------------------------------------------------------------


class TestKeyHandling:
    def test_wrong_key_type_is_refused(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode("utf-8")
        )
        with pytest.raises(ValueError, match="Ed25519"):
            load_signing_key(rsa_pem)

    def test_verifier_is_none_when_the_home_holds_no_public_key(self, home) -> None:
        """None must stay distinguishable from a permissive verifier.

        A stub returning True here would make `status` print VERIFIED for a lock
        nothing checked, which is the collapse `LoadedLock.verified` is
        three-state to prevent.
        """
        home.issuer_public_key_path.unlink()
        assert home.issuer_public_pem() is None
        assert resolve_local_verifier(home.issuer_key_id(), home.issuer_public_pem()) is None

    def test_public_key_is_readable_and_private_is_not(self, home) -> None:
        assert home.issuer_public_pem() is not None
        assert "PRIVATE KEY" in home.issuer_signing_pem()
        assert (home.issuer_key_path.stat().st_mode & 0o077) == 0

    def test_unparseable_verify_key_fails_when_the_verifier_is_built(self) -> None:
        """Eager loading puts a config fault next to its cause."""
        with pytest.raises(ValueError):
            verifier_from_pem_map({"k": "-----BEGIN PUBLIC KEY-----\nnope\n"})
