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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from tegh.configvalues import SECRET_BEARING_BLOCKS, literal_fields
from tegh.discovery import ConfigScope

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


class RestoreUnverified(InterposeError):
    """A restore was written and the site does not hold it when read back."""


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
    restored: dict[str, Any] = {}
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
                    rebuilt[key] = resolve(reference["leaf"], reference["field"])
            updated[name] = rebuilt
        restored[server_id] = updated
    return restored


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


@dataclass(frozen=True)
class WrapBackup:
    """Everything unwrap needs to put the harness back as it was."""

    project: str
    harness: str
    wrapped_at: str
    sites: list[SiteBackup]

    def to_json(self) -> str:
        return json.dumps(
            {
                "project": self.project,
                "harness": self.harness,
                "wrapped_at": self.wrapped_at,
                "sites": [
                    {
                        "scope": s.scope,
                        "path": s.path,
                        "pointer": s.pointer,
                        "block": s.block,
                        "block_existed": s.block_existed,
                    }
                    for s in self.sites
                ],
            },
            **_JSON_KWARGS,
        )

    @classmethod
    def from_json(cls, raw: str) -> "WrapBackup":
        data = json.loads(raw)
        return cls(
            project=data["project"],
            harness=data["harness"],
            wrapped_at=data["wrapped_at"],
            sites=[
                SiteBackup(
                    scope=s["scope"],
                    path=s["path"],
                    pointer=list(s["pointer"]),
                    block=s["block"],
                    block_existed=s["block_existed"],
                )
                for s in data["sites"]
            ],
        )


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

    raw = site.path.read_text(encoding="utf-8")
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

    site.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = site.path.with_name(site.path.name + ".tegh-tmp")
    tmp.write_text(_render(document), encoding="utf-8")
    tmp.replace(site.path)


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
) -> InterposePlan:
    """Compute what a wrap would displace, reading every site and writing none."""
    plan = InterposePlan(
        gateway_site=gateway_site,
        gateway_name=gateway_name,
        gateway_entry=dict(gateway_entry),
        sites=list(sites),
        unwritable=list(unwritable),
    )
    for site in sites:
        _, block, _ = _load_site(site)
        if not block:
            continue
        assert_no_inline_secrets(site, block, cleared=cleared_config)
        names = sorted(name for name in block if name != gateway_name)
        if names:
            plan.displaced[site.label] = names
    return plan


def apply_interposition(
    plan: InterposePlan,
    *,
    wrapped_at: str,
    project: Path,
    harness: str,
    relocated: Optional[Mapping[str, str]] = None,
) -> WrapBackup:
    """Displace every real server and install the gateway. Returns the backup.

    The backup is built from a read of every site BEFORE the first write, so a
    failure part-way through leaves a complete record of the original state
    rather than a half-one.

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
        _, block, existed = _load_site(site)
        originals.append(
            SiteBackup(
                scope=site.scope.value,
                path=str(site.path),
                pointer=list(site.pointer),
                block=redact_relocated(block, moved, scope=site.scope.value)
                if moved
                else block,
                block_existed=existed,
            )
        )

    for site in plan.sites:
        # The gateway's own site keeps exactly one entry; every other site is
        # emptied. Pruning an emptied key rather than leaving `{}` is what makes
        # the unwrap of a site that never had a block byte-identical.
        if site == plan.gateway_site:
            _write_block(site, {plan.gateway_name: dict(plan.gateway_entry)}, prune_empty=False)
        else:
            _write_block(site, {}, prune_empty=True)

    return WrapBackup(
        project=str(project),
        harness=harness,
        wrapped_at=wrapped_at,
        sites=originals,
    )


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


@dataclass(frozen=True)
class SiteRestore:
    """What one site must hold after an unwrap, and whether it already does.

    `block` is the RESOLVED pre-wrap block, so it can hold a credential. It is
    kept out of the repr for that reason: this object ends up in assertion
    messages and tracebacks.
    """

    site: ConfigSite
    block: dict[str, Any] = field(repr=False)
    block_existed: bool
    #: False when the site already holds what the unwrap would write. Every
    #: wrap records all of a harness's sites, and most of them never had a
    #: block and never got one, so "in the backup" does not mean "restored".
    changes: bool

    @property
    def servers(self) -> list[str]:
        return sorted(self.block) if self.block_existed else []


def _site_holds(site: ConfigSite, block: Mapping[str, Any], existed: bool) -> bool:
    _, current, exists_now = _load_site(site)
    if not existed:
        return not exists_now
    return exists_now and current == block


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
    """
    prepared: list[SiteRestore] = []
    for site_backup in backup.sites:
        site = ConfigSite(
            scope=ConfigScope(site_backup.scope),
            path=Path(site_backup.path),
            pointer=tuple(site_backup.pointer),
        )
        block = dict(site_backup.block)
        if resolve is not None:
            block = resolve_references(block, resolve)
        existed = site_backup.block_existed
        prepared.append(
            SiteRestore(
                site=site,
                block=block,
                block_existed=existed,
                changes=not _site_holds(site, block, existed),
            )
        )
    return prepared


def apply_restore(steps: Sequence[SiteRestore]) -> None:
    """Write every site that differs, then read EVERY site back and compare.

    A site whose block did not exist before the wrap has its key REMOVED rather
    than written as `{}` — the difference between "no servers at this scope" and
    "an empty servers object", which is what byte-identity means for a file that
    had neither.

    The read-back is what a caller holding the only other copy of a credential
    waits for before it deletes that copy: a write that returned is not a value
    on disk. The comparison is in memory and the mismatch names the site, never
    the contents.
    """
    for step in steps:
        if not step.changes:
            continue
        if not step.block_existed:
            _write_block(step.site, {}, prune_empty=True)
        else:
            _write_block(step.site, step.block, prune_empty=False)
    for step in steps:
        if not _site_holds(step.site, step.block, step.block_existed):
            raise RestoreUnverified(
                f"{step.site.label} does not hold the pre-wrap block after the "
                "restore was written (read back and compared). Something else "
                "is writing this file, or the write did not land."
            )


def restore(
    backup: WrapBackup, *, resolve: Optional[Callable[[str, str], str]] = None
) -> list[str]:
    """Put every site's `mcpServers` block back exactly as it was.

    Returns the labels of the sites it CHANGED, which is not every site in the
    backup: see :class:`SiteRestore`.
    """
    steps = plan_restore(backup, resolve=resolve)
    apply_restore(steps)
    return [step.site.label for step in steps if step.changes]
