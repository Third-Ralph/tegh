"""interpose.py — the rewrite half of `tegh wrap`, harness-agnostic.

`discovery.py` reads every scope a harness resolves MCP servers from and writes
nothing. This module is its dual: it REPLACES the servers a harness would load
with a single pointer at tegh's gateway, and restores what it displaced.

The adapter supplies WHERE (a list of :class:`ConfigSite`); everything about how
a config file is read, edited, backed up and restored lives here, so a second
harness contributes coordinates rather than another rewriter.

## Four properties, each a refusal rather than a hope

**1. tegh never reformats a config it does not fully understand.** Every write is
parse → modify → re-serialize, which would silently reformat a file whose style
the serializer cannot reproduce. So before ANY edit, :func:`_render` re-serializes
the file *unmodified* and compares it byte-for-byte with what was on disk; a
mismatch REFUSES (:class:`ConfigFormatUnreproducible`). Claude Code's
`~/.claude.json` round-trips exactly under `indent=2, ensure_ascii=False`
[verified 2026-07-27 against a 223KB real file, 117 project entries], but that is
a fact about today's writer, not a guarantee — and the failure it prevents is
tegh rewriting 220KB of someone's settings to change four lines.

**2. Only the `mcpServers` block is touched.** A site names a JSON pointer to
exactly one dict, and nothing outside it is read into the diff or written back.
This matters because `~/.claude.json` is a LIVE state file: alongside MCP config
it carries `lastSessionId`, `lastCost`, `lastTotalInputTokens`,
`lastGracefulShutdown` — per-session telemetry the harness writes as sessions
run. Restoring a whole-file snapshot would roll back state that has nothing to do
with the wrap. So the backup holds the BLOCK, never the file.

That also corrects the reference doc, which infers "nothing else races to mutate
these files at runtime" and marks it [Assessment, not a documented guarantee]
(`docs/references/harnesses/claude-code.md` §7). The field names alone disprove
the strong reading: something does write this file, and it is the harness.

**3. An UNCLASSIFIED inline secret REFUSES the wrap.** Removing a server whose
config holds a literal credential would move that credential into tegh's backup —
adding a copy, not a boundary, which is precisely what the wrap contract forbids
[ruling: maintainer, 2026-07-25]. A `${VAR}` reference is not a secret and passes.

A value a human CLASSIFIES as a credential is **relocated** instead of
refused: it moves into tegh's per-project secret store, is delivered at spawn via
`connector_auth.env_map`, and is replaced in this backup by a reference
(:func:`redact_relocated`) so exactly one copy survives. The refusal above is now
about the UNCLASSIFIED case and about `headers`, whose remote dual tegh does not
yet emit.

tegh cannot tell a credential from a path, though, and on a real machine most
literal values are paths. So the gate consults the classifications a human
made in `configvalues.py`, and refuses everything else. The check stays HERE, at
the write site, rather than being replaced by the prompt: a caller that forgets
to classify gets a refusal, not a silent copy. The prompt is ergonomics; this is
the control.

**4. What cannot be rewritten is reported, never assumed away.** Plugin-provided
servers, claude.ai connectors and an enterprise `managed-mcp.json` have no
writable local `mcpServers` block. A wrap that silently ignored them would claim
an exclusivity it does not have — the gateway would be one server among several.
They come back as :attr:`InterposePlan.unwritable` for the caller to refuse or
record.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from tegh.configvalues import SECRET_BEARING_BLOCKS, literal_fields
from tegh.discovery import ConfigScope
from tegh.launch import gateway_home_in

if TYPE_CHECKING:  # pragma: no cover - typing only; `hooksite` imports this module
    from tegh.hooksite import HookBackup, HookInstall

#: The serialization tegh writes back. Not a style preference — it is the one
#: form observed to reproduce a real `~/.claude.json` byte-for-byte, and
#: `_render` refuses rather than writing when it does not.
_JSON_KWARGS: dict[str, Any] = {"indent": 2, "ensure_ascii": False}


class InterposeError(RuntimeError):
    """A wrap or unwrap refused. Every subclass fails toward changing nothing."""


class ConfigFormatUnreproducible(InterposeError):
    """tegh cannot re-serialize this config without reformatting it."""


class InlineSecretRefused(InterposeError):
    """A server being displaced carries a literal credential in its config."""


class BackupMissing(InterposeError):
    """Unwrap was asked to restore from a backup that is absent or unreadable."""


class BackupUnreadable(InterposeError):
    """The wrap backup is not JSON, or not the shape tegh writes."""


class RestoreUnverified(InterposeError):
    """A restore was written and the site does not hold it when read back."""


class RestoreCollision(InterposeError):
    """A server added since the wrap has the name of one the unwrap restores."""


class ConfigChangedSincePlan(InterposeError):
    """A site holds different added entries than the plan a person agreed to."""


class CredentialNotStored(InterposeError):
    """How a `resolve` callback says the store has no value for a reference."""


class CredentialUnavailable(InterposeError):
    """A relocated credential is in neither the store nor the site it left.

    Carries coordinates for the caller's message, and never a value.
    """

    def __init__(self, site_label: str, server_id: str, block: str, field_name: str):
        super().__init__(
            f"credential {server_id} {block}.{field_name} is neither in tegh's "
            f"store nor in {site_label}"
        )
        self.site_label = site_label
        self.server_id = server_id
        self.block = block
        self.field_name = field_name


@dataclass(frozen=True)
class ConfigSite:
    """One writable location holding an `mcpServers` dict.

    `pointer` is the chain of keys from the document root down to the block —
    e.g. `("projects", "/abs/project/path", "mcpServers")` for Claude Code's
    local scope, or `("mcpServers",)` for a project `.mcp.json`. Held as data so
    the adapter states coordinates and this module owns traversal.
    """

    scope: ConfigScope
    path: Path
    pointer: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.scope.value}:{self.path}"


@dataclass(frozen=True)
class UnwritableSource:
    """A server source with no local `mcpServers` block tegh can rewrite."""

    scope: ConfigScope
    detail: str


@dataclass
class InterposePlan:
    """What a wrap would change, computed before anything is written."""

    displaced: dict[str, list[str]] = field(default_factory=dict)
    gateway_site: Optional[ConfigSite] = None
    gateway_name: str = "tegh"
    gateway_entry: dict[str, Any] = field(default_factory=dict)
    sites: list[ConfigSite] = field(default_factory=list)
    unwritable: list[UnwritableSource] = field(default_factory=list)
    #: The harness hook the wrap adds beside the gateway entry (`hooksite.py`),
    #: or None for a wrap that adds none (`--no-hooks`, or an adapter with no
    #: hook). Written after the `mcpServers` blocks, recorded in the backup, and
    #: taken out by the unwrap like the gateway entry.
    hook: Optional["HookInstall"] = None

    @property
    def displaced_count(self) -> int:
        return sum(len(names) for names in self.displaced.values())


#: Marks a relocated credential in the BACKUP, in place of its value.
#: The backup records where the value went; it never records the value.
SECRET_REFERENCE_KEY = "__tegh_relocated_secret__"


def redact_relocated(
    block: Mapping[str, Any], relocated: Mapping[str, str], *, scope: str
) -> dict[str, Any]:
    """Replace each relocated credential's literal with a reference to its leaf.

    This is what makes relocation a MOVE rather than a copy. Interposition
    already empties the harness config's `mcpServers` block, so after a wrap the
    only surviving plaintext would be the one in this backup — and a backup
    holding the credential is precisely the "second plaintext copy, not a
    boundary" that `configvalues.render_refusal` used to refuse the wrap over.
    Leaving it there would have answered the wrap contract's relocation half
    with a third storage location instead of a boundary.

    `relocated` maps coordinate (`<scope>:<server>.<block>.<name>`) to the bare
    LEAF the value now lives under. `unwrap` resolves the reference back out of
    the secret store, so a round-trip is still byte-identical.
    """
    redacted: dict[str, Any] = {}
    for server_id, entry in block.items():
        if not isinstance(entry, Mapping):
            redacted[server_id] = entry
            continue
        updated = dict(entry)
        for candidate in literal_fields(str(server_id), entry, scope=scope):
            leaf = relocated.get(candidate.coordinate)
            if leaf is None:
                continue
            values = dict(updated[candidate.block])
            values[candidate.name] = {
                SECRET_REFERENCE_KEY: {"leaf": leaf, "field": candidate.name}
            }
            updated[candidate.block] = values
        redacted[server_id] = updated
    return redacted


def resolve_references(
    block: Mapping[str, Any], resolve: "Callable[[str, str], str]"
) -> dict[str, Any]:
    """Put relocated credentials back into a block, for `unwrap`.

    `resolve(leaf, field)` returns the stored value or raises. A reference that
    cannot be resolved must NOT degrade into a placeholder written to the user's
    config: that would silently corrupt a working server definition into one
    holding a literal marker string. The exception propagates and `unwrap`
    refuses with nothing written.
    """
    return _resolve_block(block, resolve, current=None, label="")[0]


#: `(server_id, block, field)`: where in a site a credential belongs.
Coordinate = tuple[str, str, str]


def _string_at(current: Optional[Mapping[str, Any]], coordinate: Coordinate) -> Optional[str]:
    """The non-empty string a site holds at one coordinate, or None."""
    node: Any = current
    for key in coordinate:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _resolve_block(
    block: Mapping[str, Any],
    resolve: "Callable[[str, str], str]",
    *,
    current: Optional[Mapping[str, Any]],
    label: str,
) -> tuple[dict[str, Any], list[Coordinate]]:
    """`resolve_references`, plus the references the SITE already satisfies.

    An unwrap removes a credential from the store only after the site holds
    it, and removes the backup after that. A process that dies between the two
    leaves a backup whose reference no longer resolves, beside a config that
    already has the value. So when `resolve` raises `CredentialNotStored`, the
    site's block as it is now (`current`) is consulted: a non-empty string at
    the same coordinate means the put-back already happened, and that string
    is carried through untouched. It is never compared with anything and never
    printed; there is nothing left to compare it with.

    Returns the resolved block and the coordinates satisfied that way. A
    reference in neither place raises `CredentialUnavailable`, naming the site
    by `label`. With `current=None` nothing is consulted and the miss
    propagates as `resolve` raised it.
    """
    restored: dict[str, Any] = {}
    in_place: list[Coordinate] = []
    for server_id, entry in block.items():
        if not isinstance(entry, Mapping):
            restored[server_id] = entry
            continue
        updated = dict(entry)
        for name in SECRET_BEARING_BLOCKS:
            values = updated.get(name)
            if not isinstance(values, Mapping):
                continue
            rebuilt = dict(values)
            for key, value in values.items():
                if isinstance(value, Mapping) and SECRET_REFERENCE_KEY in value:
                    reference = value[SECRET_REFERENCE_KEY]
                    try:
                        rebuilt[key] = resolve(reference["leaf"], reference["field"])
                    except CredentialNotStored:
                        if current is None:
                            raise
                        coordinate = (str(server_id), name, str(key))
                        held = _string_at(current, coordinate)
                        if held is None:
                            raise CredentialUnavailable(label, *coordinate) from None
                        rebuilt[key] = held
                        in_place.append(coordinate)
            updated[name] = rebuilt
        restored[server_id] = updated
    return restored, in_place


@dataclass(frozen=True)
class SiteBackup:
    """The pre-wrap `mcpServers` block for one site.

    The block, not the file. See property 2 in the module docstring: the file
    around it is live state the harness owns and keeps writing.
    """

    scope: str
    path: str
    pointer: list[str]
    block: dict[str, Any]
    block_existed: bool
    #: Whether the FILE was there before the wrap. A wrap creates the file its
    #: gateway entry goes into when there is none, and an unwrap removes a file
    #: the wrap created once nothing else is in it (`SiteRestore.removes_file`).
    #: A backup written before this was recorded is read as True, which leaves
    #: the file where it is.
    file_existed: bool = True


@dataclass(frozen=True)
class WrapBackup:
    """Everything unwrap needs to put the harness back as it was."""

    project: str
    harness: str
    wrapped_at: str
    sites: list[SiteBackup]
    #: The one entry the wrap ADDED, and the label of the site it went into.
    #: Recorded because an unwrap removes only that entry and keeps anything
    #: else that appeared since [ruling: maintainer, 2026-10-04], so it has to
    #: know which entry is the wrap's. Both are None in a backup written before
    #: this was recorded; see `_gateway_name_at` for how that one is read.
    gateway_name: Optional[str] = None
    gateway_site: Optional[str] = None
    #: The hook entry the wrap added to the harness's settings, with what
    #: existed around it (`hooksite.HookBackup`). None in a backup of a wrap
    #: that added no hook, which includes every backup written before tegh
    #: added one: such an unwrap has no hook to take out.
    hook: Optional["HookBackup"] = None

    def to_json(self) -> str:
        gateway = (
            {"gateway": {"name": self.gateway_name, "site": self.gateway_site}}
            if self.gateway_name is not None
            else {}
        )
        hook = {"hook": self.hook.to_dict()} if self.hook is not None else {}
        return json.dumps(
            {
                "project": self.project,
                "harness": self.harness,
                "wrapped_at": self.wrapped_at,
                **gateway,
                **hook,
                "sites": [
                    {
                        "scope": s.scope,
                        "path": s.path,
                        "pointer": s.pointer,
                        "block": s.block,
                        "block_existed": s.block_existed,
                        "file_existed": s.file_existed,
                    }
                    for s in self.sites
                ],
            },
            **_JSON_KWARGS,
        )

    @classmethod
    def from_json(cls, raw: str) -> "WrapBackup":
        """Parse a backup, or raise `BackupUnreadable` saying what is wrong.

        Everything a restore later indexes is checked HERE, so a truncated or
        hand-edited backup is one refusal before anything is planned, and not a
        `KeyError` from the middle of a restore. The reason names a key or a
        scope and never a value.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackupUnreadable(
                f"it is not valid JSON ({exc.msg}, line {exc.lineno})"
            ) from exc
        from tegh.hooksite import HookBackup  # noqa: PLC0415 - cycle

        try:
            gateway = data.get("gateway") or {}
            hook = data.get("hook")
            backup = cls(
                project=str(data["project"]),
                harness=str(data["harness"]),
                wrapped_at=str(data["wrapped_at"]),
                sites=[
                    SiteBackup(
                        scope=_known_scope(s["scope"]),
                        path=str(s["path"]),
                        pointer=[str(key) for key in s["pointer"]],
                        block=dict(s["block"]),
                        block_existed=bool(s["block_existed"]),
                        file_existed=bool(s.get("file_existed", True)),
                    )
                    for s in data["sites"]
                ],
                gateway_name=gateway.get("name"),
                gateway_site=gateway.get("site"),
                hook=HookBackup.from_dict(hook) if hook is not None else None,
            )
            # Walks every credential reference, which is what indexes `leaf`
            # and `field` later.
            relocated_references(backup)
        except KeyError as exc:
            raise BackupUnreadable(f"it has no {exc.args[0]!r} key") from exc
        except (TypeError, AttributeError, ValueError) as exc:
            raise BackupUnreadable(
                "it does not have the shape tegh writes (a value has the wrong type)"
            ) from exc
        return backup


def _known_scope(scope: Any) -> str:
    try:
        return ConfigScope(scope).value
    except ValueError:
        raise BackupUnreadable(
            f"it names a scope this version of tegh does not know ({scope!r})"
        ) from None


# ---------------------------------------------------------------------------
# Reading and writing a site, with the format guard
# ---------------------------------------------------------------------------


def _render(document: Any) -> str:
    return json.dumps(document, **_JSON_KWARGS)


def _load_site(site: ConfigSite) -> tuple[Optional[Any], dict[str, Any], bool]:
    """Return `(document, block, block_existed)` for one site.

    An absent FILE is not an error — a project with no `.mcp.json` simply has no
    servers at that scope, and a wrap may still need to CREATE the block there.
    An unreadable or non-object file is an error: a scope that cannot be parsed
    cannot be rewritten, and pretending otherwise leaves its servers live.
    """
    if not site.path.exists():
        return None, {}, False

    try:
        raw = site.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # The position and nothing else of the error: its own text quotes the
        # byte, and a byte of this file can be a byte of a credential.
        raise InterposeError(
            f"{site.path} is not UTF-8 text (byte {exc.start} does not decode) — "
            "refusing to rewrite a config tegh cannot read: its servers would "
            "stay live while the wrap reported success."
        ) from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InterposeError(
            f"{site.path} is not valid JSON ({exc}) — refusing to rewrite a config "
            "tegh cannot parse: its servers would stay live while the wrap reported "
            "success."
        ) from exc

    # Property 1: prove the round-trip BEFORE any edit is contemplated.
    if _render(document) != raw:
        raise ConfigFormatUnreproducible(
            f"{site.path} does not round-trip through tegh's JSON serializer "
            "byte-for-byte, so writing it back would reformat the whole file to "
            "change a few lines. Refusing. (tegh writes `indent=2, "
            "ensure_ascii=False`; this file uses something else.)"
        )

    node: Any = document
    for key in site.pointer[:-1]:
        if not isinstance(node, Mapping) or key not in node:
            return document, {}, False
        node = node[key]
    if not isinstance(node, Mapping):
        return document, {}, False

    leaf = site.pointer[-1]
    block = node.get(leaf)
    if block is None:
        return document, {}, False
    if not isinstance(block, dict):
        raise InterposeError(
            f"{site.path}: {'.'.join(site.pointer)} is not an object — refusing to "
            "rewrite a config shaped in a way this adapter does not model."
        )
    return document, dict(block), True


def _write_block(site: ConfigSite, block: dict[str, Any], *, prune_empty: bool) -> None:
    """Replace one site's `mcpServers` block, leaving every other key untouched.

    Written via a temporary file in the same directory and an atomic rename, so a
    crash mid-write cannot leave a user with a truncated `~/.claude.json`.
    """
    document, _, _ = _load_site(site)
    if document is None:
        if not block:
            return
        document = {}

    _set_block(document, site, block, prune_empty=prune_empty)
    site.path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(site.path, _render(document))


def _set_block(
    document: Any, site: ConfigSite, block: dict[str, Any], *, prune_empty: bool
) -> None:
    """Put `block` at the site's pointer in a parsed document, in memory."""
    node: Any = document
    for key in site.pointer[:-1]:
        if not isinstance(node, dict):
            raise InterposeError(f"{site.path}: cannot descend through {key!r}")
        node = node.setdefault(key, {})
    if not isinstance(node, dict):
        raise InterposeError(f"{site.path}: {'.'.join(site.pointer)} has no object parent")

    leaf = site.pointer[-1]
    if not block and prune_empty:
        node.pop(leaf, None)
    else:
        node[leaf] = block


def _prune_empty_parents(document: Any, pointer: Sequence[str]) -> bool:
    """Drop the objects above a block that are empty. Returns whether any went.

    Only for a file the wrap created: there, every object on the way down to
    the gateway entry was made by the wrap, so one that is empty again holds
    nothing of anybody's. Stops at the first object that holds something.
    """
    chain: list[Any] = [document]
    for key in pointer[:-1]:
        node = chain[-1]
        if not isinstance(node, dict) or key not in node:
            break
        chain.append(node[key])
    pruned = False
    for depth in range(len(chain) - 1, 0, -1):
        if chain[depth] != {}:
            break
        del chain[depth - 1][pointer[depth - 1]]
        pruned = True
    return pruned


#: The mode of a config tegh has to CREATE. A file it replaces keeps its own.
_NEW_CONFIG_MODE = 0o600


def _replace_file(path: Path, text: str) -> None:
    """Replace `path` atomically, with the permission bits it already had.

    A harness config can hold a credential, before a wrap and again after an
    unwrap, and its owner may have made it 0600 for that reason. The temporary
    file is therefore CREATED with the existing file's mode, and never by
    `write_text` under the process umask, which would turn a 0600 config into
    a 0644 one at exactly the moment a credential is written back into it. The
    same shape as the credential map in `store.py`: the mode is set at
    creation, so there is no window in which the bytes are readable more
    widely than the file they replace.

    A temporary file that fails to become the config is removed, since on an
    unwrap it holds the resolved credential.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = _NEW_CONFIG_MODE
    tmp = path.with_name(path.name + ".tegh-tmp")
    # A temporary file of OUR name is left only by a tegh process that died
    # between creating it and renaming it. O_EXCL below would refuse over it
    # forever, so it is removed first; nothing else writes that name.
    tmp.unlink(missing_ok=True)
    handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            # The umask can only have narrowed the mode asked for above, so
            # this widens the temporary file back to the original's bits and
            # never past them.
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# The inline-secret gate
# ---------------------------------------------------------------------------


def read_block(site: ConfigSite) -> dict[str, Any]:
    """One site's `mcpServers` block, for a caller that must inspect it early.

    Exposed so `tegh wrap` can inventory literal values and prove the format
    round-trips BEFORE it burns an admission ceremony. Both refusals
    used to arrive after the whole wrap had run, which is the posture
    `bulk-ratify` deliberately does not take: refuse before the write, not
    after.
    """
    _, block, _ = _load_site(site)
    return block


def assert_no_inline_secrets(
    site: ConfigSite,
    block: Mapping[str, Any],
    *,
    cleared: Optional[Mapping[str, str]] = None,
) -> None:
    """Refuse the wrap over any literal value not classified as configuration.

    Displacing a server whose config holds an unclassified literal would copy
    that value into tegh's backup — "wrapping without relocation is redecorating
    the config, not securing it" [ruling: maintainer, 2026-07-25], and a second
    plaintext copy is strictly worse than one. A value a human classified —
    either as configuration or as a credential the wrap relocates — is approved by
    DIGEST below; everything else refuses.

    `cleared` maps each coordinate (`<scope>:<server>.<block>.<name>`) a human
    classified as configuration to the SHA-256 of the value they classified.
    The default is EMPTY, so a caller that skips the classification step
    refuses exactly as before: the permissive direction has to be asked for,
    never inherited.

    **The digest is compared here, not merely carried.** This function reads the
    site FRESH, and it runs at the end of a wrap — after snapshot, review,
    admission, the lock write and grant seeding. The value it is about to copy
    into tegh's backup is therefore not necessarily the value anyone looked at:
    `~/.claude.json` is live state this repo has OBSERVED changing mid-session,
    and the wrapped agent has a shell. Matching on the coordinate alone would
    make the clearance a name heuristic with extra steps — exactly what the
    classification step refused to be — with the whole wrap as its window. So a value that moved
    under the classification is treated as unclassified, which it is.
    """
    approved = dict(cleared or {})
    unclassified: dict[str, list[str]] = {}
    changed: dict[str, list[str]] = {}
    for name, entry in block.items():
        if not isinstance(entry, Mapping):
            continue
        for candidate in literal_fields(str(name), entry, scope=site.scope.value):
            expected = approved.get(candidate.coordinate)
            if expected == candidate.value_sha256:
                continue
            bucket = changed if expected is not None else unclassified
            bucket.setdefault(str(name), []).append(
                f"{candidate.block}.{candidate.name}"
            )

    if not unclassified and not changed:
        return

    def detail(offenders: dict[str, list[str]]) -> str:
        return "; ".join(
            f"{name} ({', '.join(sorted(fields))})"
            for name, fields in sorted(offenders.items())
        )

    parts = []
    if unclassified:
        parts.append(f"unclassified literal values in {detail(unclassified)}")
    if changed:
        parts.append(
            f"values that CHANGED during this wrap in {detail(changed)} — what is "
            "there now is not what was classified"
        )
    raise InlineSecretRefused(
        f"{site.label} holds " + ", and ".join(parts) + ". tegh cannot tell a "
        "credential from a path, and wrapping one nobody vouched for would move a "
        "possible credential into tegh's backup — a second plaintext copy, not a "
        "boundary, which is exactly what the wrap contract forbids. Run `tegh "
        "wrap` again and classify each value, or replace the literal with a "
        "${VAR} reference."
    )


# ---------------------------------------------------------------------------
# Plan / apply / restore
# ---------------------------------------------------------------------------


def plan_interposition(
    *,
    sites: Sequence[ConfigSite],
    gateway_site: ConfigSite,
    gateway_entry: Mapping[str, Any],
    gateway_name: str = "tegh",
    unwritable: Sequence[UnwritableSource] = (),
    cleared_config: Optional[Mapping[str, str]] = None,
    hook: Optional["HookInstall"] = None,
) -> InterposePlan:
    """Compute what a wrap would displace, reading every site and writing none."""
    plan = InterposePlan(
        gateway_site=gateway_site,
        gateway_name=gateway_name,
        gateway_entry=dict(gateway_entry),
        sites=list(sites),
        unwritable=list(unwritable),
        hook=hook,
    )
    for site in sites:
        _, block, _ = _load_site(site)
        if not block:
            continue
        assert_no_inline_secrets(site, block, cleared=cleared_config)
        # Every entry, whatever it is called. A server of the user's own named
        # like the gateway entry is displaced and restored with the rest, so it
        # is listed with the rest. This project's gateway is never among them:
        # a wrap refuses a project that already runs it before it plans.
        plan.displaced[site.label] = sorted(block)
    return plan


def backup_of(
    plan: InterposePlan,
    *,
    wrapped_at: str,
    project: Path,
    harness: str,
    relocated: Optional[Mapping[str, str]] = None,
) -> WrapBackup:
    """What a wrap is about to displace, read from every site and writing none.

    The first half of an interposition, on its own so that a caller can put
    the backup ON DISK before the first site is written (`tegh wrap` does). A
    wrap that dies between the two then leaves a backup and an untouched
    config, which `tegh unwrap` clears; the other order leaves a rewritten
    config and no record of what it held.

    `relocated` maps a credential coordinate to the LEAF it now lives under;
    each such value is replaced in the backup by a reference, so the
    credential ends this operation in exactly one place. The default is empty,
    which is byte-for-byte the backup as it was before relocation existed.
    """
    if plan.gateway_site is None:
        raise InterposeError("no gateway site — nothing to interpose into")

    moved = dict(relocated or {})
    originals: list[SiteBackup] = []
    for site in plan.sites:
        document, block, existed = _load_site(site)
        originals.append(
            SiteBackup(
                scope=site.scope.value,
                path=str(site.path),
                pointer=list(site.pointer),
                block=redact_relocated(block, moved, scope=site.scope.value)
                if moved
                else block,
                block_existed=existed,
                file_existed=document is not None,
            )
        )
    hook = None
    if plan.hook is not None:
        from tegh.hooksite import backup_of_hook  # noqa: PLC0415 - cycle

        hook = backup_of_hook(plan.hook)
    return WrapBackup(
        project=str(project),
        harness=harness,
        wrapped_at=wrapped_at,
        sites=originals,
        gateway_name=plan.gateway_name,
        gateway_site=plan.gateway_site.label,
        hook=hook,
    )


def write_backup(path: Path, backup: WrapBackup) -> None:
    """Put a backup on disk whole, or not at all.

    The backup is what a wrap that dies before its config is rewritten is
    recovered by: the next `tegh wrap` sees the file and sends the person to
    `tegh unwrap`, which reads it. Written in place, a wrap killed between the
    file's creation and its content would leave an empty or cut-short one, and
    then the wrap refuses for the backup and the unwrap refuses because it
    does not parse. So it is written beside the real one and renamed over it,
    as a config is (`_replace_file`): a killed wrap leaves no backup, or a
    complete one. A wrap only ever creates this file, so it is owner-only from
    its first byte, which suits a record of every server definition a config
    held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(path, backup.to_json())


def write_interposition(plan: InterposePlan) -> None:
    """Displace every real server, install the gateway, then the hook. The second half.

    The hook goes last. It reports built-in tool calls to the gateway's mouth,
    so a hook installed beside a config that does not yet name the gateway
    would report to nothing; the other order leaves, at worst, a gateway with
    no hook, which observes nothing it did not observe before.
    """
    if plan.gateway_site is None:
        raise InterposeError("no gateway site — nothing to interpose into")
    for site in plan.sites:
        # The gateway's own site keeps exactly one entry; every other site is
        # emptied. Pruning an emptied key rather than leaving `{}` is what makes
        # the unwrap of a site that never had a block byte-identical.
        if site == plan.gateway_site:
            _write_block(site, {plan.gateway_name: dict(plan.gateway_entry)}, prune_empty=False)
        else:
            _write_block(site, {}, prune_empty=True)
    if plan.hook is not None:
        from tegh.hooksite import write_hook  # noqa: PLC0415 - cycle

        write_hook(plan.hook)


def apply_interposition(
    plan: InterposePlan,
    *,
    wrapped_at: str,
    project: Path,
    harness: str,
    relocated: Optional[Mapping[str, str]] = None,
) -> WrapBackup:
    """`backup_of` then `write_interposition`. Returns the backup.

    The backup is built from a read of every site BEFORE the first write, so a
    failure part-way through leaves a complete record of the original state
    rather than a half-one. It is the caller's to store.
    """
    backup = backup_of(
        plan, wrapped_at=wrapped_at, project=project, harness=harness, relocated=relocated
    )
    write_interposition(plan)
    return backup


@dataclass(frozen=True)
class RelocatedReference:
    """One credential a wrap moved out of a site, as the backup records it.

    Coordinates only: where it came from, and the leaf and field it lives under
    in the secret store. The value is never part of this.
    """

    site_label: str
    server_id: str
    block: str
    field_name: str
    leaf: str


def relocated_references(backup: WrapBackup) -> list[RelocatedReference]:
    """Every credential reference a backup holds, in site order."""
    found: list[RelocatedReference] = []
    for site_backup in backup.sites:
        label = f"{site_backup.scope}:{site_backup.path}"
        for server_id, entry in site_backup.block.items():
            if not isinstance(entry, Mapping):
                continue
            for name in SECRET_BEARING_BLOCKS:
                values = entry.get(name)
                if not isinstance(values, Mapping):
                    continue
                for key, value in values.items():
                    if isinstance(value, Mapping) and SECRET_REFERENCE_KEY in value:
                        reference = value[SECRET_REFERENCE_KEY]
                        found.append(
                            RelocatedReference(
                                site_label=label,
                                server_id=str(server_id),
                                block=name,
                                field_name=str(reference["field"]),
                                leaf=str(reference["leaf"]),
                            )
                        )
    return found


#: The gateway entry's name in a backup that did not record one. It is the
#: only name a wrap has ever used (`InterposePlan.gateway_name`).
_UNRECORDED_GATEWAY_NAME = "tegh"


def _gateway_name_at(backup: WrapBackup, site: ConfigSite) -> Optional[str]:
    """The name the wrap gave the entry it added at `site`, or None if it added none.

    What the wrap CALLED its entry, for the plan to say, and not how the entry
    is found now: that is `_runs_gateway`, which reads the command. A backup
    that recorded its gateway answers exactly. One that did not answers
    `tegh` at every site, the only name a wrap has used.
    """
    if backup.gateway_name is None:
        return _UNRECORDED_GATEWAY_NAME
    return backup.gateway_name if backup.gateway_site == site.label else None


@dataclass(frozen=True)
class SiteRestore:
    """What one site must hold after an unwrap, and whether it already does.

    `block` is the RESOLVED pre-wrap block and `target` is what the site ends
    with, so both can hold a credential. They are kept out of the repr for that
    reason: this object ends up in assertion messages and tracebacks.
    """

    site: ConfigSite
    block: dict[str, Any] = field(repr=False)
    block_existed: bool
    #: False when the site already holds what the unwrap would write. Every
    #: wrap records all of a harness's sites, and most of them never had a
    #: block and never got one, so "in the backup" does not mean "restored".
    changes: bool
    #: Entries at the site now that are neither the wrap's gateway entry nor in
    #: the pre-wrap block: servers added since the wrap. The unwrap KEEPS them
    #: [ruling: maintainer, 2026-10-04]; it removes only what the wrap put there.
    kept: tuple[str, ...] = ()
    #: `block` plus the kept entries. With nothing kept it IS `block`, in the
    #: same order, which is what keeps the restore byte-for-byte.
    target: dict[str, Any] = field(repr=False, default_factory=dict)
    #: What the wrap called the entry it added here (`_gateway_name_at`).
    gateway_name: Optional[str] = None
    #: The entries at the site now that run this project's gateway, by name:
    #: what the unwrap takes out. Usually the one name above. Another name, or
    #: a name at a site the wrap added nothing to, is an entry renamed or moved
    #: since, and the plan has a line for it.
    gateway_found: tuple[str, ...] = ()
    #: Credential coordinates the site already held when the store did not.
    in_place: tuple[Coordinate, ...] = ()
    #: The wrap CREATED this site's file: it was not there before, and the
    #: gateway entry went into it. Never true for a file the wrap only read.
    file_created: bool = False
    #: The project the backup is for, which is how an entry that runs its
    #: gateway is told from a server of the user's (`_runs_gateway`).
    project: Optional[str] = None
    #: The unwrap removes the file: the wrap created it, and with the wrap's
    #: own entries taken out nothing is left in it. One server or one other
    #: key added since is enough to keep the file, holding just that. The same
    #: on every site of one file.
    removes_file: bool = False
    #: The unwrap takes out the objects the wrap made above this site's block,
    #: which are on the disk and empty once the block is out, in a file the
    #: wrap created and the unwrap keeps. Never true beside `removes_file`.
    prunes_parents: bool = False

    @property
    def servers(self) -> list[str]:
        return sorted(self.block) if self.block_existed else []

    @property
    def target_exists(self) -> bool:
        return self.block_existed or bool(self.kept)


def _runs_gateway(entry: Any, project: Optional[str]) -> bool:
    """Whether a config entry runs tegh's gateway for `project`.

    Read from what the entry RUNS, the same reading `tegh wrap` uses to refuse
    a second wrap, so the two cannot disagree about one entry. The name is not
    the test, in either direction. A gateway entry that was renamed, or moved
    to another scope, is still the wrap's, and an unwrap that kept it as "added
    since the wrap" would leave a project the next wrap refuses with no backup
    left to unwrap from. And an entry that is only CALLED what the wrap called
    its own, and runs something else, is a server of the user's.
    """
    if project is None or not isinstance(entry, Mapping):
        return False
    command, args = entry.get("command"), entry.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    return gateway_home_in([command, *map(str, args)], project=project) is not None


def _merged(
    site: ConfigSite,
    block: dict[str, Any],
    existed: bool,
    gateway_name: Optional[str],
    in_place: Sequence[Coordinate] = (),
    *,
    file_created: bool = False,
    project: Optional[str] = None,
) -> SiteRestore:
    """Merge the pre-wrap block with what the site holds NOW. Reads, never writes.

    The wrap's own entry comes out: any entry, under any name and at any site,
    that runs this project's gateway (`_runs_gateway`). Nothing comes out for
    its name alone. `gateway_name` is carried for the plan to say.

    Raises `RestoreCollision` when the site holds an entry under the name of a
    pre-wrap server and the two differ: keeping one would silently drop the
    other, and choosing between two server definitions is not tegh's to do. An
    entry EQUAL to the pre-wrap one is no collision. That is the state a re-run
    finds after an unwrap that restored this site and stopped.
    """
    _, current, exists_now = _load_site(site)
    kept: dict[str, Any] = {}
    gateway_found: list[str] = []
    collisions: list[str] = []
    for name, entry in current.items():
        if _runs_gateway(entry, project):
            gateway_found.append(name)
            continue
        if name not in block:
            kept[name] = entry
        elif entry != block[name]:
            collisions.append(name)
    if collisions:
        names = ", ".join(sorted(collisions))
        raise RestoreCollision(
            f"{site.label} now holds a server named {names}, and the wrap "
            "displaced a different server of the same name from that scope, "
            "which an unwrap would put back over it. Nothing was changed. "
            "Rename or remove one of the two (the entry in that file now, or "
            "the displaced one in tegh's wrap backup), then run `tegh unwrap` "
            "again."
        )
    target = {**block, **kept}
    step = SiteRestore(
        site=site,
        block=block,
        block_existed=existed,
        changes=True,
        kept=tuple(kept),
        target=target,
        gateway_name=gateway_name,
        gateway_found=tuple(gateway_found),
        in_place=tuple(in_place),
        file_created=file_created,
        project=project,
    )
    holds = exists_now == step.target_exists and current == target
    return replace(step, changes=not holds)


def _created_files(steps: Sequence[SiteRestore]) -> list[list[SiteRestore]]:
    """The steps of each file the wrap created, grouped by file."""
    by_path: dict[Path, list[SiteRestore]] = {}
    for step in steps:
        if step.file_created:
            by_path.setdefault(step.site.path, []).append(step)
    return list(by_path.values())


def _reaches(document: Any, keys: Sequence[str]) -> bool:
    """Whether every object on the way down `keys` is in the document."""
    node = document
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _with_file_fate(steps: Sequence[SiteRestore]) -> list[SiteRestore]:
    """Mark what the unwrap does to a file the wrap created. Reads, never writes.

    The answer comes from the file as it is now, with every site's target put
    into a copy in memory: what would be left is what decides, so a server kept
    at either scope of the file, or any key something else has written beside
    them, keeps the file. A file that stays loses the objects the wrap made
    above its entry when they are on the disk and end up empty, and the site
    they are above is marked, so the plan has a line for every write.
    """
    #: Per step: (removes_file, prunes_parents).
    fate: dict[int, tuple[bool, bool]] = {}
    for group in _created_files(steps):
        document, _, _ = _load_site(group[0].site)
        if document is None:
            continue  # already gone: the state a re-run finds
        parents = [step.site.pointer[:-1] for step in group]
        there = [_reaches(document, keys) for keys in parents]
        for step in group:
            _set_block(document, step.site, step.target, prune_empty=not step.target_exists)
            _prune_empty_parents(document, step.site.pointer)
        for step, keys, was_there in zip(group, parents, there):
            fate[id(step)] = (
                (True, False)
                if document == {}
                else (False, was_there and not _reaches(document, keys))
            )
    marked: list[SiteRestore] = []
    for step in steps:
        removes_file, prunes_parents = fate.get(id(step), (False, False))
        marked.append(replace(step, removes_file=removes_file, prunes_parents=prunes_parents))
    return marked


def _settle_created_file(group: Sequence[SiteRestore]) -> None:
    """Do what the plan said about a file the wrap created, once its sites are written.

    Nothing, when the plan has no line for it. Otherwise the objects the wrap
    made on the way down to its entry go when they are empty again. The file is
    removed only if that empties it AND the plan said it would be removed;
    whatever else is left is written back. Read again here because the sites
    were just written, and because something written into the file since the
    recheck is not this function's to remove.
    """
    site = group[0].site
    if not any(step.removes_file or step.prunes_parents for step in group):
        return
    document, _, _ = _load_site(site)
    pruned = [_prune_empty_parents(document, step.site.pointer) for step in group]
    if document == {} and group[0].removes_file:
        site.path.unlink()
    elif any(pruned):
        _replace_file(site.path, _render(document))


def plan_restore(
    backup: WrapBackup, *, resolve: Optional[Callable[[str, str], str]] = None
) -> list[SiteRestore]:
    """Resolve every site and compare it with the disk, writing nothing.

    `resolve(leaf, field)` puts a relocated credential back into the
    config it came from. **Every site is resolved before any site is written**,
    so a credential missing from the store aborts the unwrap with the harness
    untouched rather than half-restored — the same fail-toward-changing-nothing
    direction the wrap takes. A backup with no references never calls it, which
    is why it stays optional.

    A `resolve` that raises `CredentialNotStored` is asking this function to
    look in the site instead (`_resolve_block`). A reference found in neither
    place raises `CredentialUnavailable`.
    """
    # A wrap creates one file at most: the one its gateway entry went into.
    created = {
        s.path
        for s in backup.sites
        if not s.file_existed and f"{s.scope}:{s.path}" == backup.gateway_site
    }
    prepared: list[SiteRestore] = []
    for site_backup in backup.sites:
        site = ConfigSite(
            scope=ConfigScope(site_backup.scope),
            path=Path(site_backup.path),
            pointer=tuple(site_backup.pointer),
        )
        block = dict(site_backup.block)
        in_place: list[Coordinate] = []
        if resolve is not None:
            block, in_place = _resolve_block(
                block, resolve, current=_load_site(site)[1], label=site.label
            )
        prepared.append(
            _merged(
                site,
                block,
                site_backup.block_existed,
                _gateway_name_at(backup, site),
                in_place,
                file_created=site_backup.path in created,
                project=backup.project,
            )
        )
    return _with_file_fate(prepared)


def recheck_restore(steps: Sequence[SiteRestore]) -> list[SiteRestore]:
    """Merge every site again, against the file as it is at apply time.

    The plan was computed before a person was asked, and the harness can write
    its config while the question waits. The merge is therefore redone here,
    so what is written keeps what is there NOW. If the entries kept differ from
    the ones the plan named, the person agreed to something else: this raises
    `ConfigChangedSincePlan` before any site is written. The same goes for a
    gateway entry the plan did not name, and for a file the wrap created, when
    what the plan showed happening to it is no longer what would happen.
    """
    fresh = _with_file_fate(
        [
            _merged(
                step.site, step.block, step.block_existed, step.gateway_name,
                step.in_place, file_created=step.file_created, project=step.project,
            )
            for step in steps
        ]
    )
    for planned, now in zip(steps, fresh):
        # A file the plan showed removed and that is gone already is where the
        # person agreed it would be: there is nothing to stop for.
        gone = planned.removes_file and not planned.site.path.exists()
        was = is_now = None
        if planned.removes_file != now.removes_file and not gone:
            was, is_now = (
                ("removing this file, which the wrap created", "holds something else")
                if planned.removes_file
                else ("keeping this file", "holds nothing but what the wrap put there")
            )
        elif planned.prunes_parents != now.prunes_parents:
            was, is_now = (
                (
                    "removing an empty entry the wrap created in this file",
                    "holds something in that entry, or no such entry",
                )
                if planned.prunes_parents
                else (
                    "leaving the rest of this file as it is",
                    "holds an empty entry the wrap created",
                )
            )
        if was is not None:
            # Raised before the kept-entry check below, which the same change
            # can also trip: this one is about a whole file.
            raise ConfigChangedSincePlan(
                f"{planned.site.path} changed while tegh unwrap was waiting: "
                f"the plan showed tegh {was}, and it now {is_now}. Nothing was "
                "changed. Run `tegh unwrap` again for a plan of what is there "
                "now."
            )
        unnamed = set(now.gateway_found) - set(planned.gateway_found)
        if unnamed:
            # One that has gone since is no reason to stop: the plan said it
            # would go. One that has appeared is a removal nobody was shown.
            raise ConfigChangedSincePlan(
                f"{planned.site.label} changed while tegh unwrap was waiting: "
                f"it now holds {_names(unnamed)}, running tegh's gateway for "
                "this project, which the plan did not show being removed. "
                "Nothing was changed. Run `tegh unwrap` again for a plan of "
                "what is there now."
            )
        if set(planned.kept) != set(now.kept):
            raise ConfigChangedSincePlan(
                f"{planned.site.label} changed while tegh unwrap was waiting: "
                f"the plan showed {_names(planned.kept)} as added since the "
                f"wrap, and the file now holds {_names(now.kept)}. Nothing was "
                "changed. Run `tegh unwrap` again for a plan of what is there "
                "now."
            )
    return fresh


def _names(kept: Sequence[str]) -> str:
    return ", ".join(sorted(kept)) if kept else "no entries"


def write_site(step: SiteRestore) -> None:
    """Write one site's target block.

    A site that ends with no block has its key REMOVED rather than written as
    `{}` — the difference between "no servers at this scope" and "an empty
    servers object", which is what byte-identity means for a file that had
    neither.
    """
    _write_block(step.site, step.target, prune_empty=not step.target_exists)


def site_restored(step: SiteRestore) -> bool:
    """Whether the site holds this step's target block, read off the disk.

    A site whose file the unwrap removes is not restored while the file is
    still there, whatever it holds.
    """
    if step.removes_file and step.site.path.exists():
        return False
    _, current, exists_now = _load_site(step.site)
    return exists_now == step.target_exists and current == step.target


def apply_restore(steps: Sequence[SiteRestore]) -> None:
    """Write every site that differs, then read EVERY site back and compare.

    The read-back is what a caller holding the only other copy of a credential
    waits for before it deletes that copy: a write that returned is not a value
    on disk. The comparison is in memory, against the merged block this call
    meant to write, and the mismatch names the site, never the contents.
    """
    fresh = recheck_restore(steps)
    for step in fresh:
        if step.changes:
            write_site(step)
    for group in _created_files(fresh):
        _settle_created_file(group)
    for step in fresh:
        if not site_restored(step):
            raise RestoreUnverified(
                f"{step.site.label} does not hold the restored block after the "
                "restore was written (read back and compared). Something else "
                "is writing this file, or the write did not land."
            )


def restore(
    backup: WrapBackup, *, resolve: Optional[Callable[[str, str], str]] = None
) -> list[str]:
    """Put back every pre-wrap server, and take out the entry the wrap added.

    With nothing added since the wrap that is each site's `mcpServers` block
    exactly as it was. Returns the labels of the sites it CHANGED, which is not
    every site in the backup: see :class:`SiteRestore`.
    """
    steps = plan_restore(backup, resolve=resolve)
    apply_restore(steps)
    return [step.site.label for step in steps if step.changes]
