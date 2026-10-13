"""upgrade.py: bring a project an earlier tegh wrapped up to this version, keeping its tape.

`tegh upgrade --project P [--home H] [--yes]`

A project wrapped by tegh 0.3.0 keeps a manifest in tegh's home that the
platform this tegh runs on refuses at load. Its envelope carries `allowlists`,
which safe-agents 0.76.0 retired and refuses by name, whatever its value
(`safe_agents/broker/schemas/envelope.py`, `RETIRED_ALLOWLISTS_MESSAGE`). It also
declares none of the reserved `harness-tool` coordinates the `PreToolUse` hook
asks about (`cli.HARNESS_TOOL_OPS`), and no `tainted_harness_hold`. So
`tegh gateway` and `tegh approve` refuse such a project before they touch a
store. `earlier_wrap_refusal` is the sentence they say instead of the
platform's, and it names this command.

Unwrapping and wrapping again does not repair it. The envelope hash is a
content hash over the whole envelope, so it moved between the two platform
versions, and every grant the earlier wrap issued is stamped under the old one
and quarantined under the new. `seed` never overwrites a grant, so a new wrap
skips every one of them and every call stays quarantined. The platform's
repair for exactly this is the `re-seed` ceremony, and this module runs it.

## Regenerated, never patched

The new manifest is built by `cli._synthesize_manifest`, the function a wrap
builds one with, from values read out of the stored manifest: the polarity, the
cap, each server's connection and `env`, the admitted ToolOps and which tools
the namespace names, the credential field names, the trusted read sources and
the hold list. Nothing is edited in place.

Then the two are compared with this upgrade's own changes set aside, and they
must be equal or the upgrade refuses and writes nothing. That is what makes
regeneration safe: a value the stored manifest holds and this tegh would not
write for it (a hand edit, or something an earlier tegh wrote that this one
does not know) is named in a refusal and never dropped quietly. A manifest this
tegh wrote is a fixed point: upgrading it changes no byte.

## The grants

`seed` issues the new classes, the sixteen `harness-tool` ones, and skips every
class that already has a grant. `re-seed` then reads every granted class: a
grant under an earlier envelope hash is rebuilt at its SAME level with an
issuer-signed `reattestation` record in one atomic write, and one already
under the hash in force is skipped. A grant whose stored HMAC does not verify
is refused as an incident and never re-attested. Both are the platform's
(`safe_agents/broker/grants/commands.py`), run as a wrap runs `seed`.

## The hook

A wrap by 0.3.0 left one hook entry, under `PostToolUse`. When the wrap backup
says so and that entry is still in the settings file, the upgrade adds the
`PreToolUse` entry beside it, running the same command, and records it in the
wrap backup first, so `tegh unwrap` takes it out again. A wrap that installed
no hook gets none here.

## Order

1. The stored manifest is copied to `manifest.yaml.before-upgrade` beside it,
   unless a copy is already there. Nothing removes it.
2. A marker, `upgrade-pending`, says an upgrade has begun.
3. The manifest is replaced whole (`cli._write_manifest`).
4. `seed`, for a manifest this upgrade rewrites, then `re-seed`, always.
5. The hook: the wrap backup's record, then the settings entry.
6. The marker is removed.

A stop anywhere after step 2 leaves the marker, and the next `tegh upgrade`
finds the manifest already current and runs steps 4 to 6 again, which skip what
is done. With no rewrite and no marker, `re-seed` still runs, after the same
question: the envelope hash moves with the platform as well as with the
manifest, so a pin move can quarantine every grant of a manifest that is
already current. When it re-attests nothing and there is no hook to add, the
upgrade says "nothing to upgrade", and nothing was written. Before the ceremony has run, the new `harness-tool` classes have no
grants and the old ones are quarantined, so a call refused in that window is
refused toward less authority. The audit tape is never opened.
"""

from __future__ import annotations

import copy
import datetime
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml
from pydantic import ValidationError

from safe_agents.broker.schemas import ToolOp
from safe_agents.broker.schemas.harness_tool import HARNESS_TOOL

from tegh import hooksite, interpose
from tegh.interpose import WrapBackup
from tegh.launch import python_module_argv
from tegh.store import TeghStore, TeghStoreError
from tegh.unwrap import Prompt, cannot_read, confirm

#: The envelope key safe-agents 0.76.0 retired and refuses by name. Its
#: PRESENCE is the test, whatever its value, as it is for the platform.
RETIRED_ENVELOPE_KEY = "allowlists"

#: The suffix of the copy of the manifest as it was before the upgrade.
BEFORE_UPGRADE_SUFFIX = ".before-upgrade"

#: The marker an upgrade leaves while its ceremony has not finished.
PENDING_NAME = "upgrade-pending"

#: Keys a stored manifest must have for this tegh to regenerate it.
REQUIRED_KEYS = ("principal", "envelope", "tool_ops", "grant_classes", "mcp_servers")

_GRANTS_CEREMONY = "safe_agents.broker.grants.commands"

EXIT_OK = 0
EXIT_NOT_UPGRADED = 1  # declined, or a ceremony step failed after the manifest was written
EXIT_REFUSED = 2  # nothing written: no home, no manifest, or one that cannot be regenerated

QUESTION = "Proceed? [Y/n] "


class UpgradeRefused(Exception):
    """The stored manifest cannot be upgraded as it stands. Nothing was written."""


# ---------------------------------------------------------------------------
# The detector, for the commands that load a manifest through the platform
# ---------------------------------------------------------------------------


def carries_retired_key(manifest_path: Path) -> bool:
    """Whether the manifest's envelope carries `allowlists`, with any value.

    A YAML read, without the platform. A file that cannot be read or parsed,
    or is not shaped as a manifest, is False: the platform's own refusal is the
    right one for it.
    """
    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    envelope = loaded.get("envelope") if isinstance(loaded, dict) else None
    return isinstance(envelope, dict) and RETIRED_ENVELOPE_KEY in envelope


def earlier_wrap_refusal(store: TeghStore, project: Path) -> Optional[str]:
    """Why a command cannot load this project's manifest, naming the upgrade, or None."""
    if not carries_retired_key(store.manifest_path(project)):
        return None
    return (
        f"{project} was wrapped by an earlier tegh. Its manifest "
        f"({store.manifest_path(project)}) carries `envelope.{RETIRED_ENVELOPE_KEY}`, "
        "which the platform this tegh runs on (safe-agents 0.76.0 and later) "
        f"refuses. Run `tegh upgrade {store.named_as(project)}` to bring it to "
        "this version; its audit tape is kept."
    )


# ---------------------------------------------------------------------------
# Reading the stored manifest back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredServer:
    """One server as a stored manifest's `mcp_servers` block declares it (`cli.ServerConnection`)."""

    server_id: str
    transport: str
    command: Optional[str] = None
    args: tuple[str, ...] = ()
    cwd: Optional[str] = None
    url: Optional[str] = None


def read_stored(path: Path) -> dict:
    """The stored manifest as YAML, without the platform, which refuses it.

    Raises `UpgradeRefused` for a file that does not parse, is not an object,
    or lacks a key regeneration needs.
    """
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UpgradeRefused(
            f"there is no manifest at {path}, so this project is not wrapped under "
            "this tegh home. Run `tegh wrap claude` to wrap it."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise UpgradeRefused(f"{path} could not be read ({exc}).") from None
    except yaml.YAMLError as exc:
        raise UpgradeRefused(f"{path} does not parse as YAML ({exc}).") from None
    if not isinstance(loaded, dict):
        raise UpgradeRefused(f"{path} is not a YAML mapping, so it is not a manifest.")
    missing = [key for key in REQUIRED_KEYS if key not in loaded]
    if missing:
        raise UpgradeRefused(
            f"{path} names no {', '.join(f'`{key}`' for key in missing)}, so it is not "
            "a manifest tegh wrote and this tegh cannot regenerate it."
        )
    return loaded


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise UpgradeRefused(f"`{where}` in the stored manifest is not a mapping.")
    return value


def _servers(stored: dict, ops: Sequence[ToolOp]) -> list[StoredServer]:
    """Each declared server, in the order its ToolOps appear, then the rest.

    `_synthesize_manifest` lists ToolOps server by server in the order it is
    given the servers, so this order gives the stored list back unchanged.
    """
    declared = _mapping(stored["mcp_servers"], "mcp_servers")
    order = list(dict.fromkeys([op.tool for op in ops if op.tool in declared]))
    order += [server_id for server_id in declared if server_id not in order]
    servers = []
    for server_id in order:
        block = _mapping(declared[server_id], f"mcp_servers.{server_id}")
        args = block.get("args") or ()
        servers.append(
            StoredServer(
                server_id=server_id,
                transport=str(block.get("transport", "")),
                command=block.get("command"),
                args=tuple(str(arg) for arg in args),
                cwd=block.get("cwd"),
                url=block.get("url"),
            )
        )
    return servers


def _consumer_ops(stored: dict) -> list[ToolOp]:
    """The stored ToolOps of the admitted MCP tools: every entry but the reserved coordinates."""
    entries = stored["tool_ops"]
    if not isinstance(entries, list):
        raise UpgradeRefused("`tool_ops` in the stored manifest is not a list.")
    ops = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("tool") == HARNESS_TOOL:
            continue
        try:
            ops.append(ToolOp.model_validate(entry))
        except ValidationError as exc:
            raise UpgradeRefused(
                f"`tool_ops[{index}]` in the stored manifest is not a ToolOp this "
                f"tegh can read: {exc.errors()[0]['msg']}."
            ) from None
    return ops


def regenerate(stored: dict, project: Path) -> dict:
    """This tegh's manifest for the project, built from the stored one's values.

    Built by the wrap's own function, never by editing the stored mapping. The
    caller compares the two (`unexplained_differences`).
    """
    # cli imports this module for `earlier_wrap_refusal`, so this import waits
    # for the call.
    from tegh.cli import TRUSTED_HARNESS_READS, _synthesize_manifest  # noqa: PLC0415

    envelope = _mapping(stored["envelope"], "envelope")
    caps = _mapping(envelope.get("caps"), "envelope.caps")
    if "polarity" not in envelope or "actions_per_utc_day" not in caps:
        raise UpgradeRefused(
            "the stored envelope names no `polarity` or no `caps.actions_per_utc_day`, "
            "and a wrap never leaves either to a default."
        )
    ops = _consumer_ops(stored)
    servers = _servers(stored, ops)
    declared = stored["mcp_servers"]
    tools_by_server: dict[str, list[ToolOp]] = {}
    for op in ops:
        tools_by_server.setdefault(op.tool, []).append(op)
    named = {
        (server.server_id, tool["tool_name"])
        for server in servers
        for tool in (declared[server.server_id].get("tools") or ())
        if isinstance(tool, dict) and "tool_name" in tool
    }
    env_by_server = {
        server.server_id: dict(declared[server.server_id]["env"])
        for server in servers
        if isinstance(declared[server.server_id].get("env"), dict)
    }
    auth = stored.get("connector_auth") or {}
    credentials_by_server = {
        server_id: list((block or {}).get("env_map") or ())
        for server_id, block in _mapping(auth, "connector_auth").items()
        if isinstance(block, dict)
    }
    trusted = envelope.get("trusted_read_sources", TRUSTED_HARNESS_READS)
    hold = envelope.get("tainted_harness_hold") or ()
    return _synthesize_manifest(
        servers,
        polarity=envelope["polarity"],
        project=project,
        daily_cap=caps["actions_per_utc_day"],
        tools_by_server=tools_by_server,
        env_by_server=env_by_server,
        credentials_by_server=credentials_by_server,
        named=named,
        hold_on_taint=list(hold),
        trusted_read_sources=list(trusted or ()),
    )


# ---------------------------------------------------------------------------
# What differs, and why
# ---------------------------------------------------------------------------


def _without_upgrade_changes(manifest: Mapping, *, stored_envelope: Mapping) -> dict:
    """`manifest` with the changes an upgrade makes set aside, for comparison.

    The retired key, the reserved coordinates, and an envelope key the stored
    manifest did not declare at all (the upgrade declares it as a wrap does).
    Every ToolOp is read through the schema, so a field the platform added
    since, at its default, does not count as a difference.
    """
    shaped = copy.deepcopy(dict(manifest))
    envelope = shaped.get("envelope")
    if isinstance(envelope, dict):
        envelope.pop(RETIRED_ENVELOPE_KEY, None)
        for key in ("tainted_harness_hold", "trusted_read_sources"):
            if key not in stored_envelope:
                envelope.pop(key, None)
    shaped["tool_ops"] = [
        ToolOp.model_validate(entry).model_dump(mode="json")
        for entry in shaped.get("tool_ops") or ()
        if not (isinstance(entry, dict) and entry.get("tool") == HARNESS_TOOL)
    ]
    shaped["grant_classes"] = [
        name
        for name in shaped.get("grant_classes") or ()
        if not str(name).startswith(f"{HARNESS_TOOL}.")
    ]
    return shaped


def unexplained_differences(stored: dict, regenerated: dict) -> list[str]:
    """The keys where the regenerated manifest differs for a reason the upgrade does not give.

    Names keys, never values. Empty when regeneration kept every value the
    stored manifest holds.
    """
    envelope = stored.get("envelope") or {}
    before = _without_upgrade_changes(stored, stored_envelope=envelope)
    after = _without_upgrade_changes(_as_written(regenerated), stored_envelope=envelope)
    differing = []
    for key in sorted(set(before) | set(after)):
        if key == "envelope" and isinstance(before.get(key), dict) and isinstance(
            after.get(key), dict
        ):
            differing += [
                f"envelope.{name}"
                for name in sorted(set(before[key]) | set(after[key]))
                if before[key].get(name) != after[key].get(name)
            ]
        elif before.get(key) != after.get(key):
            differing.append(key)
    return differing


def _as_written(manifest: Mapping) -> dict:
    """The mapping a YAML read of the file `_write_manifest` writes would give back."""
    return yaml.safe_load(yaml.safe_dump(dict(manifest), sort_keys=True))


def _harness_coordinates_missing(stored: dict) -> tuple[int, int]:
    """How many of the reserved coordinates the stored manifest does not declare and grant."""
    from tegh.cli import HARNESS_TOOL_OPS  # noqa: PLC0415 - see `regenerate`

    declared = [entry for entry in stored.get("tool_ops") or () if isinstance(entry, dict)]
    granted = set(stored.get("grant_classes") or ())
    missing = 0
    for op in HARNESS_TOOL_OPS:
        name = f"{op['tool']}.{op['op']}"
        if op not in declared or name not in granted:
            missing += 1
    return missing, len(HARNESS_TOOL_OPS)


def out_of_date(stored: dict, regenerated: dict) -> list[str]:
    """One line per way the stored manifest is behind this tegh's."""
    envelope = stored.get("envelope") or {}
    lines = []
    if RETIRED_ENVELOPE_KEY in envelope:
        lines.append(
            f"the envelope carries `{RETIRED_ENVELOPE_KEY}`, which safe-agents 0.76.0 "
            "retired and refuses at load; it is left out"
        )
    missing, total = _harness_coordinates_missing(stored)
    if missing:
        lines.append(
            f"{missing} of the {total} `{HARNESS_TOOL}` coordinates the PreToolUse "
            "hook asks about are not declared and granted, and the broker refuses a "
            "built-in call at a coordinate with no entry; all are declared and granted"
        )
    if "tainted_harness_hold" not in envelope:
        lines.append(
            "the envelope declares no `tainted_harness_hold`; it is declared, empty, "
            "as a wrap declares it"
        )
    if "trusted_read_sources" not in envelope:
        lines.append(
            "the envelope trusts no built-in read; it trusts a read inside the "
            "project, as a wrap does"
        )
    if not lines and _as_written(regenerated) != stored:
        lines.append("the manifest is not in the form this tegh writes for it")
    return lines


# ---------------------------------------------------------------------------
# The hook a 0.3.0 wrap left
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookUpgrade:
    """The `PreToolUse` entry the upgrade adds, and the backup that records it."""

    install: hooksite.HookInstall
    backup_path: Path
    #: The wrap backup with the entry recorded; None when it records it already
    #: (an earlier upgrade that stopped after writing it).
    backup: Optional[WrapBackup]


@dataclass(frozen=True)
class HookFinding:
    """What the upgrade does about the hooks: add one, or say why not."""

    upgrade: Optional[HookUpgrade] = None
    note: Optional[str] = None


def plan_hook_upgrade(store: TeghStore, project: Path) -> HookFinding:
    """Read the wrap backup and the settings file. Writes nothing.

    Two things are read separately, because an upgrade can stop between
    writing them: whether the backup records a `PreToolUse` entry, and whether
    the file holds one. Either missing is work to do. Raises
    `interpose.InterposeError` for a backup or settings file tegh cannot read
    back, before anything is written.
    """
    backup_path = store.backup_path(project)
    if not backup_path.exists():
        return HookFinding()
    backup = WrapBackup.from_json(backup_path.read_text(encoding="utf-8"))
    recorded = backup.hook
    if recorded is None:
        return HookFinding()
    path = Path(recorded.path)
    held = hooksite.events_holding(
        hooksite.HookSite(path=path, events=hooksite.HOOK_EVENTS), recorded.command
    )
    needs_entry = hooksite.PRE_TOOL_USE not in held
    needs_record = hooksite.PRE_TOOL_USE not in recorded.events
    if not (needs_entry or needs_record):
        return HookFinding()
    if hooksite.POST_TOOL_USE not in held:
        return HookFinding(
            note=f"hook      none added: tegh's PostToolUse entry is no longer in {path}, "
            "so the PreToolUse entry that would sit beside it is not added either"
        )
    install = hooksite.plan_hook(
        hooksite.HookSite(path=path, events=(hooksite.PRE_TOOL_USE,)), recorded.command
    )
    updated = None
    if needs_record:
        pre = hooksite.backup_of_hook(install).events[hooksite.PRE_TOOL_USE]
        updated = WrapBackup(
            project=backup.project,
            harness=backup.harness,
            wrapped_at=backup.wrapped_at,
            sites=backup.sites,
            gateway_name=backup.gateway_name,
            gateway_site=backup.gateway_site,
            hook=hooksite.HookBackup(
                path=recorded.path,
                command=recorded.command,
                file_existed=recorded.file_existed,
                hooks_existed=recorded.hooks_existed,
                dir_existed=recorded.dir_existed,
                events={hooksite.PRE_TOOL_USE: pre, **recorded.events},
            ),
        )
    return HookFinding(
        upgrade=HookUpgrade(install=install, backup_path=backup_path, backup=updated)
    )


def apply_hook_upgrade(upgrade: HookUpgrade) -> None:
    """Record the entry in the wrap backup, then add it.

    In that order: a stop between the two leaves a backup naming an entry the
    file does not hold, which an unwrap finds absent and passes over, and which
    the next `tegh upgrade` adds. The other order would leave an entry no
    backup names, which an unwrap keeps, and a `PreToolUse` entry left behind
    with no gateway refuses every built-in call.
    """
    if upgrade.backup is not None:
        interpose.write_backup(upgrade.backup_path, upgrade.backup)
    hooksite.write_hook(upgrade.install)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class UpgradePlan:
    project: Path
    manifest_path: Path
    copy_path: Path
    stored_text: str
    regenerated: dict
    findings: list[str]
    #: An earlier upgrade's ceremony did not finish (`PENDING_NAME`).
    pending: bool
    new_classes: list[str] = field(default_factory=list)
    existing_classes: list[str] = field(default_factory=list)
    hook: HookFinding = field(default_factory=HookFinding)

    @property
    def rewrites(self) -> bool:
        return bool(self.findings)

    @property
    def seeds(self) -> bool:
        """Whether `seed` runs: only for a manifest this upgrade rewrites, or rewrote."""
        return self.rewrites or self.pending


def pending_path(store: TeghStore, project: Path) -> Path:
    return store.project_dir(project) / PENDING_NAME


def copy_path_for(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + BEFORE_UPGRADE_SUFFIX)


def build_plan(store: TeghStore, project: Path) -> UpgradePlan:
    """Read everything the upgrade needs and decide what it does. Writes nothing."""
    refusal = store.layout_refusal(project)
    if refusal is not None:
        raise UpgradeRefused(refusal)
    manifest_path = store.manifest_path(project)
    stored = read_stored(manifest_path)
    from tegh.cli import _project_principal  # noqa: PLC0415 - see `regenerate`

    if stored["principal"] != _project_principal(project):
        raise UpgradeRefused(
            f"the manifest at {manifest_path} names a principal other than the one "
            f"tegh derives for {project}, and every grant is keyed on it. Nothing "
            "was written."
        )
    regenerated = regenerate(stored, project)
    lost = unexplained_differences(stored, regenerated)
    if lost:
        raise UpgradeRefused(
            f"the manifest at {manifest_path} holds values this tegh would not write "
            f"for it, at {', '.join(lost)}, so regenerating it would change them. "
            "Nothing was written. Take them out of the manifest, or set them back to "
            "what the wrap wrote, and run `tegh upgrade` again."
        )
    stored_classes = [str(name) for name in stored["grant_classes"] or ()]
    return UpgradePlan(
        project=project,
        manifest_path=manifest_path,
        copy_path=copy_path_for(manifest_path),
        stored_text=manifest_path.read_text(encoding="utf-8"),
        regenerated=regenerated,
        findings=out_of_date(stored, regenerated),
        pending=pending_path(store, project).exists(),
        new_classes=[
            name for name in regenerated["grant_classes"] if name not in stored_classes
        ],
        existing_classes=stored_classes,
        hook=plan_hook_upgrade(store, project),
    )


def render_plan(plan: UpgradePlan, store: TeghStore) -> str:
    verb = "change" if plan.seeds or plan.hook.upgrade is not None else "check"
    lines = [f"tegh upgrade will {verb}, for {plan.project}:", ""]
    if plan.rewrites:
        lines.append(f"  manifest  {plan.manifest_path}, regenerated, because:")
        lines += [f"              - {finding}" for finding in plan.findings]
        if plan.copy_path.exists():
            lines.append(f"  keep      {plan.copy_path}, the copy an earlier upgrade made")
        else:
            lines.append(f"  copy      the manifest as it is now, to {plan.copy_path}")
    elif plan.pending:
        lines.append(
            f"  manifest  {plan.manifest_path} is already this version's; an earlier "
            "`tegh upgrade` rewrote it and its ceremony did not finish"
        )
    else:
        lines.append(f"  manifest  {plan.manifest_path} is current; nothing to rewrite")
    if plan.seeds:
        lines.append(
            f"  seed      {len(plan.new_classes)} new grant class(es); a class that "
            "has a grant already is skipped"
        )
    lines.append(
        f"  re-seed   {len(plan.existing_classes)} existing grant(s), each "
        "re-attested at its own level under the envelope hash in force with an "
        "issuer-signed record if it is stamped under another, or skipped if not. "
        "The hash can move with the platform while the manifest stays the same"
    )
    if plan.hook.upgrade is not None:
        site = plan.hook.upgrade.install.site.path
        lines.append(
            f"  hook      add tegh's PreToolUse entry to {site}, running the command "
            "its PostToolUse entry runs, so the broker is asked before each built-in "
            "call; the wrap backup records it, so `tegh unwrap` takes it out"
        )
    elif plan.hook.note is not None:
        lines.append(f"  {plan.hook.note}")
    lines += [
        "",
        "It leaves untouched:",
        "",
        f"  the audit tape at {store.audit_path(plan.project)}, which goes on from "
        "where it is",
        "  every admission, and every grant's level",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Carrying it out
# ---------------------------------------------------------------------------


def _write_beside(path: Path, text: str, *, mode_of: Optional[Path] = None) -> None:
    """Write `text` to `path` whole: beside it, then renamed over it."""
    tmp = path.with_name(path.name + ".tegh-tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode_of is not None and mode_of.exists():
            tmp.chmod(mode_of.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ceremony(store: TeghStore, project: Path, command: str) -> subprocess.CompletedProcess:
    """One grant ceremony, under the environment `cli._seed_grants` runs `seed` under."""
    env = store.ceremony_env(role="maker", project=project)
    env["BROKER_MANIFEST"] = str(store.manifest_path(project))
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        python_module_argv(_GRANTS_CEREMONY, command),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _echo(completed: subprocess.CompletedProcess) -> None:
    """The ceremony's own lines, as it printed them, indented."""
    for stream in (completed.stdout, completed.stderr):
        for line in stream.splitlines():
            if line.strip():
                print(f"  {line}")


def _again(store: TeghStore, project: Path) -> str:
    return f"tegh upgrade {store.named_as(project)}"


#: The platform's closing `re-seed` line (`grants/commands.py::reseed_command`).
_RESEED_SUMMARY = re.compile(r"\[re-seed\] ratifiedBy=.*?: (\d+) re-attested, (\d+) skipped")


def _reseed_counts(completed: subprocess.CompletedProcess) -> Optional[tuple[int, int]]:
    """`(re-attested, skipped)` from the platform's summary line, or None if it has none."""
    found = _RESEED_SUMMARY.search(completed.stdout)
    return (int(found.group(1)), int(found.group(2))) if found else None


def apply_plan(store: TeghStore, plan: UpgradePlan) -> int:
    """Carry the plan out in the order the module docstring gives. Returns the exit status."""
    project = plan.project
    marker = pending_path(store, project)
    if plan.rewrites:
        if not plan.copy_path.exists():
            _write_beside(plan.copy_path, plan.stored_text, mode_of=plan.manifest_path)
        _write_beside(marker, datetime.datetime.now(datetime.UTC).isoformat() + "\n")
        from tegh.cli import _write_manifest  # noqa: PLC0415 - see `regenerate`

        _write_manifest(plan.manifest_path, plan.regenerated)
        print(f"  wrote     {plan.manifest_path}")
        print(f"  kept      the manifest as it was, at {plan.copy_path}")
    # `re-seed` runs on every upgrade, a current manifest's included: the
    # envelope hash moves with the platform as well as with the manifest, and a
    # grant stamped under another hash is quarantined whatever the manifest
    # says. When nothing needs it, the platform skips every grant and writes
    # nothing.
    counts = None
    for command in ("seed", "re-seed") if plan.seeds else ("re-seed",):
        completed = _ceremony(store, project, command)
        _echo(completed)
        if completed.returncode != 0:
            written = (
                f"The manifest at {plan.manifest_path} is this version's, and the "
                f"one it replaced is at {plan.copy_path}. "
                if plan.seeds
                else ""
            )
            print(
                f"\nUPGRADE NOT FINISHED: `{command}` failed (exit "
                f"{completed.returncode}). {written}Run `{_again(store, project)}` "
                "again once the cause above is dealt with: it runs the ceremony "
                "again, which skips what is done.",
                file=sys.stderr,
            )
            return EXIT_NOT_UPGRADED
        if command == "re-seed":
            counts = _reseed_counts(completed)
    if plan.hook.upgrade is not None:
        site = plan.hook.upgrade.install.site.path
        try:
            apply_hook_upgrade(plan.hook.upgrade)
        except (interpose.InterposeError, OSError) as exc:
            print(
                f"\nUPGRADE NOT FINISHED: tegh's PreToolUse entry could not be added to "
                f"{site}: {exc} The manifest and the grants are this version's. Run "
                f"`{_again(store, project)}` again once the file can be written.",
                file=sys.stderr,
            )
            return EXIT_NOT_UPGRADED
        print(f"  added     tegh's PreToolUse entry to {site}")
        print(f"  recorded  it in the wrap backup at {plan.hook.upgrade.backup_path}")
    marker.unlink(missing_ok=True)
    if not plan.seeds:
        if counts is not None and counts[0] == 0 and plan.hook.upgrade is None:
            print(f"\nnothing to upgrade: {project} is already wrapped as this tegh wraps it.")
            return EXIT_OK
        print(
            "\nmanifest current; "
            + (
                f"{counts[0]} grants re-attested, {counts[1]} already under the "
                "in-force hash"
                if counts is not None
                else "the platform's re-seed summary line was not found, so the "
                "lines above are the whole account of the grants"
            )
        )
    print(
        f"\nupgraded {project}. Its audit tape is at {store.audit_path(project)}; "
        f"check it with `tegh audit --verify {store.named_as(project)}`."
    )
    if plan.rewrites:
        print(
            "Start the coding agent in this project again: a session that was running "
            "started its gateway on the earlier manifest, which the platform refused."
        )
    return EXIT_OK


def run(store: TeghStore, project: Path, *, yes: bool, prompt: Optional[Prompt] = None) -> int:
    """`tegh upgrade`: plan, show, ask, carry out. Returns the exit status."""
    if not store.is_provisioned:
        print(f"REFUSED: no tegh home at {store.home}. Run `tegh init`.", file=sys.stderr)
        return EXIT_REFUSED
    try:
        plan = build_plan(store, project)
    except (UpgradeRefused, TeghStoreError, interpose.InterposeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"REFUSED: {cannot_read(exc, needed_by='upgrade')}", file=sys.stderr)
        return EXIT_REFUSED
    print(render_plan(plan, store))
    if not yes and not confirm(QUESTION, prompt=prompt):
        return EXIT_NOT_UPGRADED
    return apply_plan(store, plan)
