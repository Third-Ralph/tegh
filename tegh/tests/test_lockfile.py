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
from tegh.launch import BROKER_INHERITED_ENV_VARS
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
        assert env["BROKER_SQLITE_PATH"] == str(store.db_path(tmp_path / "proj"))
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


#: The two environments tegh builds for the base's processes.
_BROKER_ENVIRONMENTS = {
    "ceremony": lambda store, project: store.ceremony_env(role="maker", project=project),
    "gateway": lambda store, project: store.gateway_env(project=project),
}


def _as_wrapped(
    store: TeghStore, project: Path, *, marker: str | None, backup: bool = False
) -> None:
    """The files of a project's directory that `layout_refusal` reads, and no wrap.

    `marker=None` is what a tegh with one database per home left: a manifest
    and no marker beside it.
    """
    store.manifest_path(project).parent.mkdir(parents=True, exist_ok=True)
    store.manifest_path(project).write_text("principal: {}\n", encoding="utf-8")
    if marker is not None:
        store.layout_path(project).write_text(marker, encoding="utf-8")
    if backup:
        store.backup_path(project).write_text("{}", encoding="utf-8")


def _an_unreadable_marker(store: TeghStore, project: Path) -> None:
    """Put a directory where the marker goes: there, and no reader gets bytes from it.

    A directory and not a file of mode 000, which root reads anyway.
    """
    store.layout_path(project).mkdir()


@pytest.mark.parametrize("environment", sorted(_BROKER_ENVIRONMENTS))
class TestOneDatabasePerProject:
    """The only database path either environment names is the project's own.

    The base keys an admitted tool by server name and tool name, so the file is
    the namespace: two projects in one file share every admission of a server
    they both name.
    """

    def test_two_projects_are_given_two_databases(
        self, tmp_path: Path, environment: str
    ) -> None:
        store = provision(tmp_path / "home")
        build = _BROKER_ENVIRONMENTS[environment]
        first = build(store, tmp_path / "first")["BROKER_SQLITE_PATH"]
        second = build(store, tmp_path / "second")["BROKER_SQLITE_PATH"]

        assert first == str(store.db_path(tmp_path / "first"))
        assert second == str(store.db_path(tmp_path / "second"))
        assert first != second
        assert Path(first).parent == store.project_dir(tmp_path / "first")

    def test_the_grant_space_is_not_named_apart(
        self, tmp_path: Path, environment: str
    ) -> None:
        """Unnamed, the base keeps grants in the database `BROKER_SQLITE_PATH`
        names. Naming a second file here would be the way grants came to be
        shared between projects while admissions were not."""
        store = provision(tmp_path / "home")
        env = _BROKER_ENVIRONMENTS[environment](store, tmp_path / "proj")
        assert "BROKER_SQLITE_GRANTS_PATH" not in env

    def test_the_database_of_the_earlier_layout_is_named_nowhere(
        self, tmp_path: Path, environment: str
    ) -> None:
        store = provision(tmp_path / "home")
        env = _BROKER_ENVIRONMENTS[environment](store, tmp_path / "proj")
        assert store.shared_db_path == store.home / "tegh.db"
        assert not [name for name, value in env.items() if str(store.shared_db_path) in value]

    def test_none_is_built_for_a_project_of_the_earlier_layout(
        self, tmp_path: Path, environment: str
    ) -> None:
        """Building one is what would let the base create this project's
        database, empty, and serve from it. Refused before anything is written:
        the credential map both environments otherwise create is not there."""
        store = provision(tmp_path / "home")
        project = tmp_path / "proj"
        _as_wrapped(store, project, marker=None)

        with pytest.raises(TeghStoreError, match="earlier version of tegh"):
            _BROKER_ENVIRONMENTS[environment](store, project)
        assert sorted(p.name for p in store.project_dir(project).iterdir()) == ["manifest.yaml"]

    def test_none_is_built_for_a_project_whose_marker_cannot_be_read(
        self, tmp_path: Path, environment: str
    ) -> None:
        """A marker that cannot be read says nothing about the layout, and
        nothing is served on that. Refused before anything is written, as for
        the earlier layout."""
        store = provision(tmp_path / "home")
        project = tmp_path / "proj"
        _as_wrapped(store, project, marker=None)
        _an_unreadable_marker(store, project)

        with pytest.raises(TeghStoreError, match="could not be read"):
            _BROKER_ENVIRONMENTS[environment](store, project)
        assert sorted(p.name for p in store.project_dir(project).iterdir()) == [
            "layout", "manifest.yaml",
        ]

    def test_one_is_built_once_the_marker_is_written(
        self, tmp_path: Path, environment: str
    ) -> None:
        store = provision(tmp_path / "home")
        project = tmp_path / "proj"
        _as_wrapped(store, project, marker=None)
        store.mark_layout(project)
        assert _BROKER_ENVIRONMENTS[environment](store, project)["BROKER_SQLITE_PATH"]


class TestLayoutRefusal:
    """Which projects are refused for the layout they were wrapped under, and how."""

    def test_a_project_that_was_never_wrapped_is_not_this_refusal(self, tmp_path: Path) -> None:
        store = provision(tmp_path / "home")
        assert store.layout_refusal(tmp_path / "proj") is None

    def test_a_project_of_this_layout_is_not_refused(self, tmp_path: Path) -> None:
        store = provision(tmp_path / "home")
        _as_wrapped(store, tmp_path / "proj", marker="2\n")
        assert store.layout_refusal(tmp_path / "proj") is None

    def test_an_old_database_in_the_home_refuses_nothing_by_being_there(
        self, tmp_path: Path
    ) -> None:
        store = provision(tmp_path / "home")
        store.shared_db_path.write_bytes(b"left by an earlier tegh")
        _as_wrapped(store, tmp_path / "proj", marker="2\n")
        assert store.layout_refusal(tmp_path / "never-wrapped") is None
        assert store.layout_refusal(tmp_path / "proj") is None

    @pytest.mark.parametrize(
        ("marker", "backup", "says", "does_not_say"),
        [
            pytest.param(
                None, True, ("earlier version of tegh", "to put your own servers back"),
                "left no backup", id="earlier-layout-with-a-backup",
            ),
            pytest.param(
                None, False, ("earlier version of tegh", "left no backup"),
                "to put your own servers back", id="earlier-layout-never-rewritten",
            ),
            pytest.param(
                "3\n", True, ("store layout as '3'", "different version of tegh"),
                "earlier version", id="a-layout-this-tegh-does-not-know",
            ),
            pytest.param(
                "", False, ("store layout as ''",), "earlier version", id="an-empty-marker",
            ),
        ],
    )
    def test_the_refusal_names_both_commands(
        self, tmp_path: Path, marker, backup, says, does_not_say
    ) -> None:
        store = provision(tmp_path / "home")
        project = tmp_path / "a project"  # the space is what the quoting is for
        _as_wrapped(store, project, marker=marker, backup=backup)

        refusal = store.layout_refusal(project)

        assert refusal is not None
        for phrase in says:
            assert phrase in refusal, refusal
        assert does_not_say not in refusal, refusal
        where = f"--project '{project}' --home {store.home}"
        assert f"`tegh unwrap {where}`" in refusal or "`tegh unwrap` has nothing" in refusal
        assert f"`tegh wrap claude {where}`" in refusal, refusal
        with pytest.raises(TeghStoreError) as raised:
            store.require_layout(project)
        assert str(raised.value) == refusal

    @pytest.mark.parametrize("backup", [True, False], ids=["with-a-backup", "never-rewritten"])
    def test_a_marker_that_cannot_be_read_is_refused_and_not_guessed_at(
        self, tmp_path: Path, backup: bool
    ) -> None:
        """Neither "this layout" nor "the earlier one": tegh cannot tell, and says that.

        Answering None here would serve the project, and answering as for a
        missing marker would tell its owner an earlier tegh wrapped it. The
        refusal names the file that could not be read and why.
        """
        store = provision(tmp_path / "home")
        project = tmp_path / "proj"
        _as_wrapped(store, project, marker=None, backup=backup)
        _an_unreadable_marker(store, project)

        for ask in (store.layout_refusal, store.require_layout):
            with pytest.raises(TeghStoreError) as raised:
                ask(project)
            said = str(raised.value)
            assert f"{store.layout_path(project)} could not be read" in said, said
            assert "cannot tell which store layout" in said, said
            assert "earlier version of tegh" not in said, said

#: What a shell can export that neither environment names. The base reads every
#: `BROKER_*` and signing name here; the last three are not the base's, and are
#: here because an inherit-everything start passed them too.
_ONLY_THE_SHELL_NAMES = (
    "BROKER_SQLITE_GRANTS_PATH",  # the case in #5: a second grants database
    "BROKER_SQLITE_GRANTS_READONLY",
    "BROKER_CEREMONY_IDENTITY",  # would select a different identity arm
    "BROKER_AUDIT_BUCKET",
    "BROKER_ENVELOPE_LOAD",
    # The base refuses when both key sources are set — correctly, since picking
    # one silently would attribute records to an unintended key.
    "ISSUER_SIGNING_KEY_SECRET_ARN",
    "ISSUER_VERIFY_KEYS_FILE",
    "AWS_PROFILE",
    "PYTHONPATH",
    "TEGH_HOME",
)

#: Every name both environments take from the shell: a copy of
#: `BROKER_INHERITED_ENV_VARS` by value, on purpose. A name deleted from the list
#: fails its own case below in both environments, and a name added to it fails
#: the comparison until it is added here too, which is the deliberate edit.
_INHERITED_FROM_THE_SHELL = (
    "HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER",
    "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)

_FROM_THE_SHELL = "from-the-shell"
_AS_THE_MACHINE_HAS_IT = "/as/the/machine/has/it"


@pytest.fixture(params=sorted(_BROKER_ENVIRONMENTS))
def broker_environment(request: Any, tmp_path: Path):
    """Build one of the two environments, from whatever `os.environ` holds then."""
    store = provision(tmp_path / "home")
    return lambda: _BROKER_ENVIRONMENTS[request.param](store, tmp_path / "proj")


class TestNothingArrivesFromTheShell:
    """Both environments start from a list of names, never from `os.environ` (#5)."""

    @pytest.mark.parametrize("name", _ONLY_THE_SHELL_NAMES)
    def test_a_variable_tegh_does_not_name_is_dropped(
        self, broker_environment: Any, monkeypatch: Any, name: str
    ) -> None:
        monkeypatch.setenv(name, _FROM_THE_SHELL)
        assert name not in broker_environment()

    def test_a_variable_tegh_sets_keeps_the_value_tegh_set(
        self, broker_environment: Any, monkeypatch: Any
    ) -> None:
        """The other half of "named, not inherited": for a name tegh does set,
        the shell's value loses. Every name is poisoned at once, so a name added
        to either environment later is covered without being listed here."""
        for name in BROKER_INHERITED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        named = broker_environment()
        assert named, "with nothing to inherit, what is left is what tegh sets"
        for name in named:
            monkeypatch.setenv(name, _FROM_THE_SHELL)

        assert broker_environment() == named

    def test_no_base_configuration_is_on_the_inherited_list(self) -> None:
        """The list is machine settings. A `BROKER_*` or signing name on it would
        hand that value back to the shell, which is the defect, one line long."""
        assert not [
            name
            for name in BROKER_INHERITED_ENV_VARS
            if name.startswith(("BROKER_", "ISSUER_", "EVALUATOR_", "AWS_", "PYTHON"))
        ]

    def test_the_inherited_list_is_the_one_pinned_here(self) -> None:
        """Adding a name widens what a shell can hand the broker, and deleting
        one breaks a machine that needed it. Either is an edit to this file too.
        Compared as the names on one side only, so a failure reads as the name."""
        assert set(BROKER_INHERITED_ENV_VARS) ^ set(_INHERITED_FROM_THE_SHELL) == set()

    @pytest.mark.parametrize("name", _INHERITED_FROM_THE_SHELL)
    def test_what_the_machine_needs_still_arrives(
        self, broker_environment: Any, monkeypatch: Any, name: str
    ) -> None:
        """Dropping too much fails further away than passing too much: without
        `PATH` a wrapped server's command is not found, `USER` is where a ceremony
        record's operator name is read from, and without the proxy a remote
        server is unreachable. Every name, in both environments, because the one
        left out of a sample is the one that goes missing unnoticed."""
        monkeypatch.setenv(name, _AS_THE_MACHINE_HAS_IT)
        assert broker_environment()[name] == _AS_THE_MACHINE_HAS_IT


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
