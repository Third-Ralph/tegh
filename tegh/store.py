"""tegh's own directory — local authority, deliberately OUTSIDE the project tree.

This is where key #1 and key #2 of two-key admission live on a laptop:

===========================  =====================================  ============
Role                         Cloud                                  tegh (local)
===========================  =====================================  ============
key #1 namespace + ToolOp    image-baked ``AgentManifest``          ``<home>/projects/<slug>/manifest.yaml``
key #2 activation row        DynamoDB registry row                  ``<home>/tegh.db`` (sqlite)
committed record             (none)                                 ``tegh.lock`` in the PROJECT tree
===========================  =====================================  ============

**Why not in the project directory.** The wrapped agent has ordinary write
access to the project, and the harness's protected-path list is hard-coded
vendor-side — tegh cannot add a file to it. A manifest or database living there
would be editable by an injected agent with no prompt, collapsing both keys at
once. Putting them under a separate home is what makes the lock a *projection*
(TL1) rather than the authority.

**Posture-1 honesty, restated because it is easy to overclaim.** This defeats the
*ordinary* injected-agent edit — an agent following instructions in a poisoned
tool description, writing where it normally writes. It does NOT defeat a
determined same-user adversary, who can read and write this directory too:
same-user process separation is not privilege separation. Posture 2
(``tegh contain``) is what turns this into a real boundary. `tegh posture` must
say so.

## Provisioning

``tegh init`` mints two secrets into this directory at 0600 and never anywhere
else:

- the store-integrity HMAC key (``BROKER_HMAC_KEY``), which makes a tampered
  registry row detectable, and
- the local issuer Ed25519 signing key (``ISSUER_SIGNING_KEY_FILE``), which
  signs the admission ledger records.

Both are NAMED to the ceremony through the environment rather than defaulted,
because the base refuses to fall back to a dev key: a durable store under
an unnamed key is unnamed authority whatever the backend.

**No secret-location convention is published here** (TL10). These paths are
tegh's internal layout, not part of the frozen lock format, and nothing in
`tegh.lock` records where a secret lives — so the resolution stays changeable
store-side.

What a relocated credential is NAMED, as opposed to where it sits, is settled:
a manifest names a bare LEAF by ruling, and the deploying topology supplies
the scope (`docs/config-provenance.md` § "Secret naming — the leaf rule").
Locally the per-project directory plays the topology's part, so the leaf is the
bare `server_id` — see :meth:`TeghStore.secrets_path`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping

#: Override for the tegh home. Named rather than defaulted-and-hidden so a
#: second, isolated tegh (a test, a second identity) is one env var away.
TEGH_HOME_ENV = "TEGH_HOME"

_DB_NAME = "tegh.db"
_HMAC_NAME = "hmac.key"
_ISSUER_KEY_NAME = "issuer.pem"
_ISSUER_PUB_NAME = "issuer.pub"
_SECRETS_NAME = "secrets.json"
_OWNER_ONLY = 0o600


class TeghStoreError(RuntimeError):
    """The local store is missing, unreadable, or unsafely permissioned."""


def tegh_home() -> Path:
    """The tegh home directory — `$TEGH_HOME`, else `~/.tegh`."""
    named = os.environ.get(TEGH_HOME_ENV, "").strip()
    return Path(named).expanduser() if named else Path.home() / ".tegh"


def project_slug(project: Path | str) -> str:
    """A stable, readable directory name for one wrapped project.

    The basename carries the readable half and a digest of the ABSOLUTE path
    carries uniqueness: two checkouts of the same repository at different paths
    are different wraps (different discovered scopes, possibly different
    admitted sets), and collapsing them would let one project's admissions
    silently govern the other.
    """
    resolved = Path(project).expanduser().resolve()
    readable = re.sub(r"[^A-Za-z0-9_.-]", "-", resolved.name) or "project"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


@dataclass(frozen=True)
class TeghStore:
    """Paths into one tegh home, plus the environment the ceremony needs."""

    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / _DB_NAME

    @property
    def hmac_key_path(self) -> Path:
        return self.home / _HMAC_NAME

    @property
    def issuer_key_path(self) -> Path:
        return self.home / _ISSUER_KEY_NAME

    @property
    def issuer_public_key_path(self) -> Path:
        """The PUBLIC half — what verifies a lock or a ledger record."""
        return self.home / _ISSUER_PUB_NAME

    def project_dir(self, project: Path | str) -> Path:
        return self.home / "projects" / project_slug(project)

    def manifest_path(self, project: Path | str) -> Path:
        """Key #1 for one project — the namespace + ToolOp declaration."""
        return self.project_dir(project) / "manifest.yaml"

    def snapshot_path(self, project: Path | str, server_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", server_id)
        return self.project_dir(project) / "snapshots" / f"{safe}.json"

    def backup_path(self, project: Path | str) -> Path:
        """The harness config this wrap displaced, for `tegh unwrap`.

        Under the tegh home rather than the project, for the reason everything
        else is: the wrapped agent can write the project tree, and a backup it
        can edit is a backup that cannot be trusted to restore.
        """
        return self.project_dir(project) / "wrap-backup.json"

    def config_decisions_path(self, project: Path | str) -> Path:
        """Which of this project's literal config values are configuration.

        Under the tegh home for the same reason the manifest is: it records a
        judgment that authorizes tegh to carry a value, and a file the wrapped
        agent could edit would let it authorize its own. Only CONFIG decisions
        land here, each bound to a hash of the value it was made about, so the
        record can never become a name heuristic the operator trained by hand.
        """
        return self.project_dir(project) / "config-values.json"

    def audit_path(self, project: Path | str) -> Path:
        """This project's hash-chained audit tape.

        One tape per project, because the gateway serves one project's principal
        and a shared file would interleave two principals' chains into a
        sequence neither can verify alone.
        """
        return self.project_dir(project) / "audit.jsonl"

    def secrets_path(self, project: Path | str) -> Path:
        """The local credential map the broker's `file` secrets arm reads.

        **Per-project, not per-home**, and the placement is the naming
        convention. A manifest names a bare LEAF by ruling, and the
        DEPLOYING TOPOLOGY supplies the scope
        (`docs/config-provenance.md` § "Secret naming"); here this directory is
        that topology, exactly as the mount root is for the `dir` arm. So a
        relocated credential is keyed by the bare `server_id` and needs no
        project qualification inside its own name — which is what keeps the map
        portable to the two arms that refuse a name containing a separator.

        Home-level would collide. The leaf defaults to the connector name
        (`broker_server.py`'s `secret_name_for`), which for tegh is the
        `server_id`, so two wrapped projects each configuring a `github` server
        would both want the leaf `github` in one shared file — and one
        project's gateway would be handed the other's credential.
        """
        return self.project_dir(project) / _SECRETS_NAME

    def ensure_secrets_file(self, project: Path | str) -> Path:
        """Create the 0600 credential map if absent; never clobber an existing one."""
        path = self.secrets_path(project)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            path.chmod(_OWNER_ONLY)
        return path

    def read_secrets(self, project: Path | str) -> dict[str, str]:
        """This project's credential map, `{leaf: value}` — `{}` when absent.

        The shape `LocalFileSecretsProvider` expects: a flat map of leaf to an
        opaque string. For a relocated MCP credential that string is itself a
        JSON object, because `env_map` resolves FIELDS inside one leaf
        (`broker/CONNECTOR-AUTH.md` § "Spawn-time delivery").
        """
        path = self.secrets_path(project)
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TeghStoreError(
                f"the credential map at {path} is not valid JSON ({exc}). Refusing "
                "to overwrite it — a wrap that silently replaced an unreadable "
                "secret store would destroy credentials it cannot read."
            ) from exc
        if not isinstance(loaded, dict):
            raise TeghStoreError(f"the credential map at {path} is not a JSON object")
        return {str(key): str(value) for key, value in loaded.items()}

    def write_secret_leaf(
        self, project: Path | str, leaf: str, fields: Mapping[str, str]
    ) -> None:
        """Store one server's credential fields under a bare LEAF.

        `leaf` is bare by ruling — the per-project directory supplies the scope,
        so nothing qualifies the name — and that is asserted rather than trusted:
        a separator here would resolve fine on this arm (the `file` provider is a
        flat map with no path semantics) and then refuse on the `dir` arm the
        moment the same map moved to a container. Catching it at the write is
        what keeps `tegh migrate` a straight pump.

        The whole map is rewritten atomically at 0600. Other leaves are
        preserved: one project can wrap several credentialed servers, and each
        wrap must not evict the last one's secret.
        """
        if not leaf or "/" in leaf or "\\" in leaf or leaf in (".", ".."):
            raise TeghStoreError(
                f"refusing to store a credential under {leaf!r}: a secret name is a "
                "bare leaf, and the directory supplies the scope "
                "(docs/config-provenance.md, 'Secret naming — the leaf rule')."
            )
        secrets_map = self.read_secrets(project)
        secrets_map[leaf] = json.dumps(dict(fields), sort_keys=True)
        self._write_secrets_map(project, secrets_map)

    def remove_secret_fields(
        self, project: Path | str, fields_by_leaf: Mapping[str, Collection[str]]
    ) -> None:
        """Drop the named fields from this project's credential map, for `unwrap`.

        Only what is named goes. A leaf keeps any field the caller did not name,
        and a leaf left with no fields is dropped whole, because a leaf holding
        `{}` would make the next `env_map` lookup fail on a missing field
        instead of on a missing secret. A field or leaf that is already absent
        is not an error: the end state asked for is the one that holds.

        When the last leaf goes the file STAYS, as an empty map at 0600. That is
        the state `ensure_secrets_file` creates and `gateway_env` requires, and
        `read_secrets` answers `{}` for it exactly as it does for an absent
        file, so removing the file would only have the next gateway start put
        it back.
        """
        secrets_map = self.read_secrets(project)
        path = self.secrets_path(project)
        for leaf, names in fields_by_leaf.items():
            if leaf not in secrets_map:
                continue
            try:
                stored = json.loads(secrets_map[leaf])
            except json.JSONDecodeError as exc:
                raise TeghStoreError(
                    f"the credential stored under {leaf!r} in {path} is not a JSON "
                    f"object of fields ({exc.msg}), so tegh cannot remove part of "
                    "it. Nothing in that file was changed."
                ) from exc
            if not isinstance(stored, dict):
                raise TeghStoreError(
                    f"the credential stored under {leaf!r} in {path} is not a JSON "
                    "object of fields, so tegh cannot remove part of it. Nothing "
                    "in that file was changed."
                )
            for name in names:
                stored.pop(name, None)
            if stored:
                secrets_map[leaf] = json.dumps(stored, sort_keys=True)
            else:
                del secrets_map[leaf]
        self._write_secrets_map(project, secrets_map)

    def _write_secrets_map(
        self, project: Path | str, secrets_map: Mapping[str, str]
    ) -> None:
        """Replace the credential map atomically, owner-only from creation."""
        path = self.secrets_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tegh-tmp")
        # 0600 at CREATION, not by a chmod afterwards: the latter leaves a window
        # in which the credential is on disk world-readable. `os.open` with the
        # mode is the only way to have no such window.
        handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(dict(secrets_map), stream, indent=2, sort_keys=True)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(path)

    @property
    def is_provisioned(self) -> bool:
        return self.hmac_key_path.exists() and self.issuer_key_path.exists()

    # -- reading provisioned material -------------------------------------

    def _read_owner_only(self, path: Path, label: str) -> str:
        """Read a secret file, REFUSING one readable beyond its owner.

        The same posture `issuer_keys.py` takes on the issuer key, applied to
        every secret this directory holds: a private key that is not private
        cannot carry attribution, and warning-and-continuing would leave the
        operator believing in evidence the file cannot support.
        """
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise TeghStoreError(
                f"{label} at {path} could not be read ({exc}). Run `tegh init` "
                "to provision this tegh home."
            ) from exc
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise TeghStoreError(
                f"{label} at {path} is mode {stat.S_IMODE(mode):04o} — refusing "
                f"to use a secret readable beyond its owner. Run: chmod 600 {path}"
            )
        return path.read_text(encoding="utf-8").strip()

    def hmac_key(self) -> str:
        return self._read_owner_only(self.hmac_key_path, "the store-integrity HMAC key")

    def issuer_signing_pem(self) -> str:
        """The issuer PRIVATE key, under the same 0600-or-refuse posture.

        Used to sign `tegh.lock` (TL2). The base's ceremony reads this file
        itself, from the path `ceremony_env` names — this accessor exists for
        the lock signature, which tegh produces in-process because its basis is
        the lock file's literal bytes rather than a DSSE payload
        (`signing.py` states why at length).
        """
        return self._read_owner_only(self.issuer_key_path, "the local issuer signing key")

    def issuer_public_pem(self) -> str | None:
        """The issuer PUBLIC key, or None when this home has none on disk.

        None rather than an exception, and never a permission refusal: a public
        key is not a secret (`provision` writes it world-readable on purpose),
        and a home provisioned before this file existed must still be usable —
        it just cannot verify, which the caller reports honestly instead of
        treating as verified.
        """
        path = self.issuer_public_key_path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip()

    def issuer_key_id(self) -> str:
        """The key_id a ledger verifier resolves this issuer by.

        Derived from the tegh home's own path rather than stored: it must be
        stable for a given home and distinct across homes, and deriving it
        removes a file that could disagree with the key beside it.
        """
        digest = hashlib.sha256(str(self.home.resolve()).encode("utf-8")).hexdigest()[:12]
        return f"tegh-local-{digest}"

    # -- the ceremony environment ------------------------------------------

    def ceremony_env(self, *, role: str, project: Path | str) -> dict[str, str]:
        """The environment for one ceremony subprocess (`role` is maker/checker).

        Every value is NAMED here rather than left to the caller's ambient
        environment, because the base refuses defaults for exactly these:
        an unnamed HMAC key or store path is unnamed authority. The
        role selects from the base's CLOSED two-value local-identity catalog —
        tegh cannot assert an arbitrary identity, and the `who` half stays
        OS-derived.

        `BROKER_ENVELOPE_LOAD` is deliberately absent: the MCP admission
        ceremony does not read an envelope, and naming one here would be config
        with no consumer.
        """
        environment = dict(os.environ)
        environment.update(
            {
                "BROKER_STORE": "sqlite",
                "BROKER_SQLITE_PATH": str(self.db_path),
                "BROKER_HMAC_KEY": self.hmac_key(),
                "BROKER_LOCAL_IDENTITY": role,
                "ISSUER_SIGNING_KEY_FILE": str(self.issuer_key_path),
                "ISSUER_SIGNING_KEY_ID": self.issuer_key_id(),
                "ISSUER_SIGNING_ZONE": "local",
                # The SNAPSHOT leg of the ceremony spawns the real server, so a
                # credentialed one needs its credential resolvable here and not
                # only at gateway time — this is precisely where a credentialed
                # server used to fail. `commands._secrets_provider` reads
                # the same closed catalog the broker boot does, so naming the
                # arm is what makes `env_map` deliver at snapshot.
                "BROKER_SECRETS": "file",
                "BROKER_SECRETS_FILE": str(self.ensure_secrets_file(project)),
            }
        )
        # A stale AWS-arm signing source would collide with the file arm and
        # the base refuses when both are set — correctly, since picking one
        # silently would attribute records to a key the operator did not mean.
        environment.pop("ISSUER_SIGNING_KEY_SECRET_ARN", None)
        return environment

    def gateway_env(self, *, project: Path | str) -> dict[str, str]:
        """The environment `tegh gateway` runs the broker's MCP mouth under.

        **This is why the harness config holds no secrets.** The entry written
        into `~/.claude.json` names `tegh gateway` and a project path and nothing
        else; every authority-shaping value is resolved HERE, in a process the
        wrapped agent does not configure. Putting `BROKER_HMAC_KEY` in the
        harness config to save this indirection would recreate exactly the
        plaintext-credential exposure the wrap exists to remove
        (`docs/references/harnesses/claude-code.md` §2).

        Every value is NAMED because sqlite is a durable arm and the base
        refuses defaults for all of them. `BROKER_GRANT_LOAD=read`
        is not a choice either: the base REFUSES `seed` on sqlite, so the gateway
        serves what the ceremony wrote and nothing else — absent grants are
        denied, fail-closed.
        """
        environment = dict(os.environ)
        environment.update(
            {
                "BROKER_STORE": "sqlite",
                "BROKER_SQLITE_PATH": str(self.db_path),
                "BROKER_HMAC_KEY": self.hmac_key(),
                "BROKER_MANIFEST": str(self.manifest_path(project)),
                "BROKER_GRANT_LOAD": "read",
                "BROKER_AUDIT_PATH": str(self.audit_path(project)),
                "BROKER_SECRETS": "file",
                "BROKER_SECRETS_FILE": str(self.ensure_secrets_file(project)),
            }
        )
        # The secrets arm is NAMED because the base refuses a durable store paired
        # with fake credentials AT BOOT: leaving it unset does not defer the
        # question, it prevents the gateway from starting. This file
        # holds the credentials `tegh wrap` relocated out of the harness config,
        # keyed by bare leaf; it stays an empty 0600 map for a project with no
        # credentialed server, so the arm it names always resolves.
        for stale in ("BROKER_AUDIT_BUCKET", "BROKER_ENVELOPE_LOAD", "BROKER_LOCAL_IDENTITY"):
            environment.pop(stale, None)
        return environment


def _write_secret(path: Path, payload: str) -> None:
    """Create a 0600 file, refusing to clobber an existing secret.

    Never overwrites: re-minting an HMAC key would orphan every admitted row
    (each verifies under the old key and would quarantine), and re-minting the
    issuer key would silently break attribution for every record already
    signed. Both failures are quiet and both are worse than a refusal.
    """
    if path.exists():
        raise TeghStoreError(
            f"{path} already exists — refusing to overwrite it. Re-minting this "
            "secret would orphan everything already written under it (rows "
            "quarantine, signed records stop verifying). Remove the tegh home "
            "deliberately if that is what you want."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _OWNER_ONLY)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(payload)


def provision(home: Path | None = None) -> TeghStore:
    """Mint this tegh home's secrets. Idempotent only in that it refuses twice.

    Generates a fresh HMAC key and a fresh Ed25519 issuer signing key, both at
    0600, and creates nothing else — the sqlite database is created by the
    store on first use, and a project's manifest is written at wrap time.
    """
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: PLC0415

    store = TeghStore(home=home if home is not None else tegh_home())
    store.home.mkdir(parents=True, exist_ok=True)
    os.chmod(store.home, 0o700)

    _write_secret(store.hmac_key_path, secrets.token_hex(32))

    private_key = ed25519.Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    _write_secret(store.issuer_key_path, pem)

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    # The PUBLIC half is not a secret and is written world-readable on purpose:
    # verifying a lock or a ledger record on a second machine needs it, and a
    # public key kept at 0600 is a public key nobody can use.
    store.issuer_public_key_path.write_text(public_pem, encoding="utf-8")

    return store
