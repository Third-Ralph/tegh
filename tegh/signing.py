"""Sign and verify `tegh.lock` — TL2's sidecar, over the file's literal bytes.

This is the crypto half of the seam `lockfile.py` declares: it produces the
`LockSigner` and `LockVerifier` callables that module takes by injection, so
`lockfile.py` stays pure I/O and this module never touches a path.

## Why tegh signs rather than borrowing the base's signer

The base signs two ledgers (the outbound provenance chain, the promotion and
admission records) and both are **DSSE**: the signature covers a
pre-authentication encoding of an in-toto statement whose `subject` is a digest
of the payload. `tegh.lock` is deliberately not that shape. TL2 makes the basis
the *literal bytes of the file on disk*, because the lock's whole value is that
a human reads it in a review diff — a signature over a digest of a re-derived
canonical form would verify a document nobody looked at.

So there is nothing to reuse. The base exposes no "sign these bytes" helper: its
two signing sites either build the PAE internally or take pre-encoded PAE bytes
(`RecordSigner.sign_pae`), and handing a JSON lock document to something named
`sign_pae` would be a lie at the call site. What *is* shared — parsing a PEM into
an Ed25519 key — is five lines whose home module is the EventTrigger chain
signer; importing it would couple tegh to the channels subsystem for a helper.
`store.py` already talks to `cryptography` directly to GENERATE this very
keypair, so signing with it here is the same layer doing the matching half, not
a re-derivation of a base mechanism.

## Domain separation: why the raw signature is safe here

A raw signature over arbitrary bytes normally invites cross-protocol confusion —
a signature the key produced for one purpose being replayed as another. It
cannot happen for this key, and the reason is structural rather than lucky: the
issuer key's other signatures are over DSSE PAE bytes, which by construction
begin `DSSEv1 `, while `canonical_lock_bytes` always begins `{` (a JSON object,
pretty-printed). The two byte-spaces are disjoint, so no ledger signature can
ever be presented as a lock signature or the reverse.

That is worth stating rather than assuming, because it is the property that
would silently stop holding if the lock format ever grew a non-JSON envelope —
at which point this sidecar needs an explicit domain prefix and that is a
format change (a ruling), not an implementation detail.

## What a lock signature does and does not buy

It binds *these bytes* to *this key*. It says the lock was produced by whoever
holds the tegh home's issuer key and has not been edited since. It says nothing
about whether the admission behind an entry was sound, and — since a same-user
attacker can read the key file (posture 1, see `store.py`) — nothing against an
adversary already running as this user. `tegh posture` is where those limits get
stated; overclaiming them here is the failure the posture ladder exists to prevent.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tegh.lock import LockSignature

#: The one algorithm the frozen sidecar admits. `LockSignature.algorithm` is a
#: `Literal["ed25519"]`, so parsing already refuses anything else; this constant
#: exists so the signer stamps the same string the schema expects rather than a
#: second literal that could drift from it.
ALGORITHM = "ed25519"


class UnknownLockSigner(RuntimeError):
    """The sidecar names a `key_id` this machine has no public key for.

    Deliberately NOT folded into `LockSignatureInvalid`. "This lock was signed
    by a key I do not know" and "this lock does not match its signature" call
    for different actions — the first is usually a lock from a teammate's
    machine on a single-player tool, the second is a modified artifact. Reporting
    the first as a tamper is the false alarm that teaches people to ignore the
    real one (integrity must indict tampering, never evolution — applied to the
    message rather than the basis).
    """


def load_signing_key(pem: str | bytes) -> Ed25519PrivateKey:
    """Parse a PEM-encoded Ed25519 private key, refusing any other key type."""
    data = pem.encode("utf-8") if isinstance(pem, str) else pem
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(
            "the tegh issuer signing key must be an Ed25519 private key; "
            f"found {type(key).__name__}"
        )
    return key


def load_verify_key(pem: str | bytes) -> Ed25519PublicKey:
    """Parse a PEM-encoded Ed25519 public key, refusing any other key type."""
    data = pem.encode("utf-8") if isinstance(pem, str) else pem
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(
            "a tegh lock verification key must be an Ed25519 public key; "
            f"found {type(key).__name__}"
        )
    return key


@dataclass(frozen=True)
class LockSigner:
    """A local issuer identity that can sign one lock's bytes.

    Key-injected and path-free: the caller resolves the PEM (from the tegh home,
    which is the only place it lives) and hands the material in. Callable, so it
    drops straight into `write_lock(signer=...)`.
    """

    key_id: str
    _private_key: Ed25519PrivateKey

    def __call__(self, payload: bytes) -> LockSignature:
        """Sign the lock's exact on-disk bytes (TL2).

        `payload` is what `write_lock` is about to write, serialized once. This
        function must never re-serialize a parsed model to obtain it — that is
        the basis the stored-bytes rule forbids, since it re-derives itself from whatever the
        model class looks like today and turns additive growth into a reported
        tamper.
        """
        return LockSignature(
            key_id=self.key_id,
            algorithm=ALGORITHM,
            value=base64.b64encode(self._private_key.sign(payload)).decode("ascii"),
        )


def signer_from_pem(key_id: str, pem: str | bytes) -> LockSigner:
    """Build a `LockSigner` from a PEM private key (the tegh-home cold start)."""
    return LockSigner(key_id=key_id, _private_key=load_signing_key(pem))


def verifier_from_keys(
    public_keys: Mapping[str, Ed25519PublicKey],
) -> Callable[[bytes, LockSignature], bool]:
    """A `LockVerifier` over a `key_id` -> public key map.

    Returns False only for a signature that genuinely does not verify, so
    `read_lock` raises `LockSignatureInvalid` for exactly that case. An
    UNKNOWN signer raises `UnknownLockSigner` instead of returning False: see
    that exception's docstring for why the two must not collapse.

    A malformed base64 `value` counts as not verifying rather than as an
    unknown signer — the sidecar is structurally corrupt, which is a fact about
    these bytes, not about which key produced them.
    """

    def verify(payload: bytes, signature: LockSignature) -> bool:
        public_key = public_keys.get(signature.key_id)
        if public_key is None:
            raise UnknownLockSigner(
                f"the lock is signed by key {signature.key_id!r}, which this "
                "machine has no public key for. This is normal for a lock "
                "written on another machine — tegh is single-player today, so "
                "there is no key distribution yet. It is NOT a tamper report: "
                "the signature has not been checked at all."
            )
        try:
            public_key.verify(base64.b64decode(signature.value, validate=True), payload)
        except (InvalidSignature, TypeError, ValueError, binascii.Error):
            return False
        return True

    return verify


def verifier_from_pem_map(
    pem_by_key_id: Mapping[str, str | bytes],
) -> Callable[[bytes, LockSignature], bool]:
    """`verifier_from_keys`, loading each PEM eagerly.

    Eagerly on purpose: an unparseable verification key is a configuration
    fault, and surfacing it when the verifier is BUILT puts the error next to
    its cause instead of at some later read of an unrelated lock.
    """
    return verifier_from_keys(
        {key_id: load_verify_key(pem) for key_id, pem in pem_by_key_id.items()}
    )


def resolve_local_verifier(
    key_id: str, public_pem: Optional[str]
) -> Optional[Callable[[bytes, LockSignature], bool]]:
    """The verifier for a tegh home, or None when it holds no public key.

    None means "nothing to verify with" and must stay distinguishable from a
    verifier that passes everything: `read_lock` reports `verified=None` for the
    former, which `status` prints as NOT VERIFIED. A stub returning True would
    make an unchecked lock read as a checked one, which is the exact collapse
    `LoadedLock.verified` is three-state to prevent.
    """
    if public_pem is None:
        return None
    return verifier_from_pem_map({key_id: public_pem})
