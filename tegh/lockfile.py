"""Reading and writing `tegh.lock` on disk — the projection's I/O half.

`lock.py` owns the typed shape and the ONE serialization; this module owns
where those bytes live, how they get there atomically, and the verify-then-parse
ordering `lock.py` can state but not enforce.

**TL1 — this module must never be reached from the per-call path.** The lock
sits in the project directory, which the wrapped agent can write and which the
harness's protected-path list cannot be extended to cover. A broker that read
admissions from here would let an injected agent admit its own tools with one
`Edit`, collapsing both keys of two-key admission. Authority stays store-side:
key #1 is tegh's own store directory (outside the project tree) and key #2 is
the ceremony-written registry row. This file is written by the ceremony and
read by humans, `tegh status`, and `tegh diff`.

That is a claim a docstring cannot keep, so it is also a test:
`test_lockfile.py::TestTL1NoRuntimeReader` asserts no module under
`safe_agents/broker/` imports this package at all.

## Signing (TL2/TL3)

TL2's basis is the exact bytes of `tegh.lock` on disk, carried in a `tegh.lock.sig`
sidecar. This module owns the ORDERING that makes that basis mean something —
serialize once, sign those bytes, and on the way back in verify BEFORE parsing —
while `signing.py` owns the crypto, injected here as the `LockSigner` and
`LockVerifier` callables so this file stays pure I/O.

`write_lock` REFUSES to write an unsigned lock unless explicitly forced, which is
TL3 as frozen. Since a signer exists, `--allow-unsigned` is the rare deliberate
act it was meant to be rather than a flag on every invocation; the alternative —
writing unsigned by default and noting it in the output — would make the unsigned
state the silent norm.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tegh.lock import (
    LockSignature,
    TeghLock,
    canonical_lock_bytes,
    canonical_signature_bytes,
    parse_lock_bytes,
    parse_signature_bytes,
)

#: The committed artifact and its signature sidecar. Both live at the project
#: root and are committed together (TL2).
LOCK_FILENAME = "tegh.lock"
SIGNATURE_FILENAME = "tegh.lock.sig"

#: Produce a signature over the lock's exact on-disk bytes. Injected rather
#: than imported so the crypto seam is swappable and this module stays pure I/O.
LockSigner = Callable[[bytes], LockSignature]
#: Verify a signature against the lock's exact on-disk bytes. Returns True iff
#: the signature is valid for those bytes.
LockVerifier = Callable[[bytes, LockSignature], bool]


class UnsignedLockRefused(RuntimeError):
    """TL3 — refusing to write an unsigned lock without an explicit override."""


class LockSignatureInvalid(RuntimeError):
    """The sidecar does not verify against the lock's bytes.

    Raised BEFORE parsing, and never downgraded to a warning: a lock whose
    signature fails is not a lock with a problem, it is bytes of unknown
    origin. Parsing it first and reporting the failure afterwards would mean
    acting on content already shown to be untrustworthy.
    """


@dataclass(frozen=True)
class LoadedLock:
    """A parsed lock plus the honest provenance of the bytes it came from.

    `verified` is a THREE-state fact and is modelled as such:
    `True` (a sidecar was present and checked out), `False` (present and
    failed — unreachable here, since that raises), and `None` (nothing to
    check). Collapsing "verified" and "nothing to verify" into one boolean is
    how an unsigned artifact starts reading as a signed one.
    """

    lock: TeghLock
    path: Path
    raw: bytes
    signature: Optional[LockSignature]
    verified: Optional[bool]

    @property
    def is_signed(self) -> bool:
        return self.signature is not None


def lock_paths(project: Path | str) -> tuple[Path, Path]:
    """The lock and sidecar paths for a project root."""
    root = Path(project)
    return root / LOCK_FILENAME, root / SIGNATURE_FILENAME


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write via a temp file in the SAME directory, then `os.replace`.

    Same-directory matters: `os.replace` is only atomic within a filesystem, and
    a temp file under `/tmp` can land on a different one. A half-written lock is
    worse than no lock — it is a committed artifact a reviewer would read as the
    admitted set.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_lock(
    lock: TeghLock,
    *,
    project: Path | str,
    signer: Optional[LockSigner] = None,
    allow_unsigned: bool = False,
) -> tuple[Path, Optional[Path]]:
    """Serialize ONCE, sign those bytes, write both files (TL2/TL2a/TL3).

    The single serialization is the whole integrity story: `payload` is what
    lands in `tegh.lock`, what the signature is computed over, and what a
    verifier will digest verbatim. Nothing re-serializes the model.

    Refuses an unsigned write unless `allow_unsigned=True` (TL3) — the
    `ratify --allow-unsigned` posture. A stale sidecar from a previous signed
    write is REMOVED on a forced unsigned write: leaving it would present a
    signature over bytes that are no longer there, which verifies as a tamper
    and teaches the reader to disbelieve the alarm.
    """
    payload = canonical_lock_bytes(lock)
    lock_path, signature_path = lock_paths(project)

    if signer is None and not allow_unsigned:
        raise UnsignedLockRefused(
            f"refusing to write an unsigned {LOCK_FILENAME}: the lock is a "
            "portable, committed record of what a human admitted, and an "
            "unsigned one carries no evidence of who produced it. Configure a "
            "local issuer signing key, or pass --allow-unsigned to accept an "
            "unsigned lock deliberately (TL3)."
        )

    signature = signer(payload) if signer is not None else None
    _write_atomic(lock_path, payload)
    if signature is not None:
        _write_atomic(signature_path, canonical_signature_bytes(signature))
        return lock_path, signature_path

    signature_path.unlink(missing_ok=True)
    return lock_path, None


def read_lock(
    project: Path | str,
    *,
    verifier: Optional[LockVerifier] = None,
) -> Optional[LoadedLock]:
    """Read, VERIFY, then parse (TL2). Returns None when no lock exists.

    The ordering is the point, and it is enforced here because this is the
    layer that knows whether a sidecar exists — `parse_lock_bytes` cannot know,
    which is why it documents the requirement rather than implementing it.

    An absent lock is None rather than an exception: "this project has not been
    wrapped" is a normal state `tegh status` must report, not an error.

    A sidecar present with NO verifier configured is reported as unverified
    (`verified=None`), never as verified-by-default.

    A verifier returns False only for a signature that genuinely fails against
    these bytes. It may also RAISE — `signing.UnknownLockSigner` when the sidecar
    names a key this machine has no public half for — and that propagates
    deliberately: "signed by someone I don't know" is not "modified since
    signing", and reporting the first as the second is the false tamper alarm
    that teaches readers to dismiss the real one.
    """
    lock_path, signature_path = lock_paths(project)
    if not lock_path.exists():
        return None

    raw = lock_path.read_bytes()
    signature: Optional[LockSignature] = None
    verified: Optional[bool] = None

    if signature_path.exists():
        signature = parse_signature_bytes(signature_path.read_bytes())
        if verifier is not None:
            if not verifier(raw, signature):
                raise LockSignatureInvalid(
                    f"{lock_path} does not match its signature in {signature_path}: "
                    f"the sidecar was produced by key {signature.key_id!r} over "
                    "different bytes. The lock has been modified since it was "
                    "signed — root-cause that before trusting any entry in it."
                )
            verified = True

    return LoadedLock(
        lock=parse_lock_bytes(raw),
        path=lock_path,
        raw=raw,
        signature=signature,
        verified=verified,
    )
