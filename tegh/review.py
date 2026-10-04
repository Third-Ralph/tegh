"""Rendering for the two reviews a human actually reads (TL11).

Two jobs, one discipline:

- **First admission** (`tegh wrap`): there is no baseline, so nothing is a
  *delta*. What a human must read before binding a tool is its description in
  full (the model-facing injection vector), its contract, and the proposed
  classification with the provenance of each proposed field.
- **Verify against the lock** (`tegh status` / `tegh diff`): the lock's pinned
  definition is the baseline and the live server is the new side. This is where
  TL11's tiers apply.

**The countermeasure is against reviewer complacency, not against
invisibility.** Everything here is already visible in the raw JSON; the reason
the tiers exist is that thirty-nine cosmetic changes must not drown the one
that steers the model. So the tier a delta lands in decides two things
together: where it renders, and whether a bulk acknowledgment can cover it.
Rendering something loudly while still letting `--yes` sweep it past is the
failure mode this module exists to avoid.

## The tiers

`DISCLOSURE` and `STEERING` are the top tier: rendered verbatim, in full,
first, and each requiring its OWN per-tool acknowledgment that `--yes` cannot
supply. `CONTRACT` is machine-summarized and bulk-acknowledgeable. Unchanged
tools are a count.

`DISCLOSURE` is the disclosure amendment [ruling: maintainer, 2026-07-25]. TL11 as first
frozen put every `input_schema` delta in the summarized tier, which is right
for a stdio server spawned from config tegh confirmed — but wrong for a remote
one. On a `url` server the input schema is not merely model-facing surface, it
is an **exfiltration-channel specification**: it enumerates what the vendor
receives on every call. A newly-REQUIRED field there is a disclosure
escalation — the agent must now send that value to a third party — and it is
categorically more serious than the same field appearing on a locally-spawned
child. The amendment is deliberately narrow: only *newly required*, only on a
*remote* server. Promoting every remote schema delta would put retyped
optionals in the unmissable tier and train reviewers to bulk-dismiss it, which
is precisely the M5 finding-flood failure the tiering exists to prevent.

Within the top tier `DISCLOSURE` renders above `STEERING`. Both gate
identically (each needs its own acknowledgment), so the order is a legibility
choice, not a security one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

from safe_agents.broker.schemas import (
    McpToolDef,
    SchemaDelta,
    compute_tool_def_hash,
    diff_input_schema,
)

from tegh.classify import Basis, ToolOpProposal

#: Advertised metadata fields that are signed and rendered verbatim when
#: they move. `input_schema`/`output_schema` are absent: they are CONTRACT-class
#: and go through the delta summarizer. Mirrors `broker/mcp/render.py`'s list —
#: kept explicit rather than derived from the model so a schema change to
#: `McpToolDef` is a conscious edit here too.
VERBATIM_METADATA_FIELDS = ("title", "icons", "annotations", "meta", "execution")

_INDENT = "  "


class DeltaTier(str, Enum):
    """How loudly a change renders, and what may acknowledge it. CLOSED.

    The two are one decision: a tier that rendered loudly but could be swept by
    `--yes` would be theatre, and a tier that blocked on cosmetic churn would
    train the reviewer to reach for `--yes` reflexively.
    """

    #: The disclosure amendment: a newly-required field on a REMOTE server. Per-tool ack.
    DISCLOSURE = "disclosure"
    #: A `description` change — the model-facing injection vector. Per-tool ack.
    STEERING = "steering"
    #: Schema/metadata changes. Machine-summarized, bulk-acknowledgeable.
    CONTRACT = "contract"


@dataclass
class ToolDelta:
    """What moved between an admitted definition and the live one.

    Computed, never stored: drift is evaluated at read time (the same posture
    `broker/mcp/registry.py` takes — a quarantine is never written).
    """

    tool_name: str
    admitted: McpToolDef
    live: McpToolDef
    transport: Literal["stdio", "streamable-http"]
    schema_delta: SchemaDelta
    metadata_changes: list[tuple[str, object, object]] = field(default_factory=list)

    @property
    def description_changed(self) -> bool:
        return self.admitted.description != self.live.description

    @property
    def is_remote(self) -> bool:
        return self.transport == "streamable-http"

    @property
    def disclosure_fields(self) -> list[str]:
        """Newly-required fields that are a disclosure escalation.

        Empty for a stdio server by construction: the amendment turns on the
        transport, because that is what decides whether a required argument
        leaves the machine.
        """
        return list(self.schema_delta.newly_required) if self.is_remote else []

    @property
    def tiers(self) -> list[DeltaTier]:
        """Every tier this delta lands in, loudest first."""
        found: list[DeltaTier] = []
        if self.disclosure_fields:
            found.append(DeltaTier.DISCLOSURE)
        if self.description_changed:
            found.append(DeltaTier.STEERING)
        if not self.schema_delta.is_empty or self.metadata_changes:
            found.append(DeltaTier.CONTRACT)
        return found

    @property
    def has_drift(self) -> bool:
        return compute_tool_def_hash(self.admitted) != compute_tool_def_hash(self.live)

    @property
    def requires_per_tool_ack(self) -> bool:
        """Is this delta beyond what a bulk `--yes` may cover?"""
        return bool(self.disclosure_fields) or self.description_changed


def compute_tool_delta(
    admitted: McpToolDef,
    live: McpToolDef,
    *,
    transport: Literal["stdio", "streamable-http"],
) -> ToolDelta:
    """Diff one admitted definition against its live counterpart. Pure."""
    metadata_changes: list[tuple[str, object, object]] = []
    for name in ("output_schema", *VERBATIM_METADATA_FIELDS):
        old = getattr(admitted, name)
        new = getattr(live, name)
        if old != new:
            metadata_changes.append((name, old, new))

    return ToolDelta(
        tool_name=live.tool_name,
        admitted=admitted,
        live=live,
        transport=transport,
        schema_delta=diff_input_schema(admitted.input_schema, live.input_schema),
        metadata_changes=metadata_changes,
    )


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------


def _verbatim_block(label: str, text: str) -> list[str]:
    """Render `text` VERBATIM and IN FULL — never truncated, summarized, or
    whitespace-collapsed. Summarizing the injection vector would defeat the
    entire point of showing it.

    An EMPTY description renders as an explicit marker rather than as nothing:
    a tool advertising no description at all is a fact about it, and rendering
    that as a blank gap would read as a formatting artifact.
    """
    body = text.splitlines() if text else ["(empty — the server advertises no text here)"]
    return [f"{_INDENT}--- {label} ---", *body]


def render_description_verbatim(description: str) -> str:
    """One description, in full — for a FIRST admission, which has no baseline."""
    return "\n".join(
        [
            f"{_INDENT}DESCRIPTION (verbatim, in full — this text steers the model):",
            *_verbatim_block("description", description),
        ]
    )


def render_description_delta(old: str, new: str) -> str:
    """Both sides verbatim. TL11's steering tier."""
    return "\n".join(
        [
            f"{_INDENT}!! DESCRIPTION CHANGED (STEERING) — the model-facing injection",
            f"{_INDENT}   vector. Read both sides; this needs its own acknowledgment.",
            *_verbatim_block("ADMITTED (verbatim)", old),
            *_verbatim_block("LIVE (verbatim)", new),
        ]
    )


def render_disclosure_escalation(delta: ToolDelta, *, url: Optional[str]) -> str:
    """The disclosure tier: what the vendor now receives that it did not before.

    Whether the field is brand new or was already advertised as optional is
    folded into the escalation line rather than left to the contract summary
    below. Both facts are about the same field, and splitting them across two
    tiers would print the name twice — which teaches a reader that the loud
    tier is duplicated noise, the exact habit the tiering exists to prevent.
    """
    destination = url or "the remote server"
    added = set(delta.schema_delta.added_fields)
    lines = [
        f"{_INDENT}!! DISCLOSURE ESCALATION (REMOTE server) — this needs its own",
        f"{_INDENT}   acknowledgment. Every call now sends these to {destination}:",
    ]
    for name in delta.disclosure_fields:
        origin = (
            "a NEW field the server did not advertise before"
            if name in added
            else "previously advertised, but optional — the agent could omit it"
        )
        lines.append(f"{_INDENT}   ! {name!r} is NOW REQUIRED ({origin})")
    lines.append(
        f"{_INDENT}   An input schema on a remote server is not only model-facing"
    )
    lines.append(
        f"{_INDENT}   surface — it specifies what this vendor receives per call."
    )
    return "\n".join(lines)


def render_schema_summary(delta: SchemaDelta, *, suppress: tuple[str, ...] = ()) -> str:
    """CONTRACT tier — machine-summarized, never a raw-JSON dump.

    `suppress` omits fields already rendered in the disclosure tier — from the
    added list as well as the required one, since that tier states both facts
    about the field — so an escalated field appears ONCE, at the top.
    """
    lines: list[str] = []
    for name in delta.added_fields:
        if name not in suppress:
            lines.append(f"{_INDENT}   + added field: {name!r}")
    for name in delta.removed_fields:
        lines.append(f"{_INDENT}   - removed field: {name!r}")
    for name, old_type, new_type in delta.retyped_fields:
        lines.append(f"{_INDENT}   ~ retyped: {name!r} ({old_type} -> {new_type})")
    for name in delta.newly_required:
        if name not in suppress:
            lines.append(f"{_INDENT}   ! now required: {name!r}")
    for name in delta.no_longer_required:
        lines.append(f"{_INDENT}   ! no longer required: {name!r}")
    if not lines:
        return ""
    return "\n".join([f"{_INDENT}~ contract changes (input_schema):", *lines])


def render_metadata_changes(changes: list[tuple[str, object, object]]) -> str:
    """Signed metadata moves, rendered verbatim — these are small values, and an
    annotation flip is exactly the drift the signed set was widened to catch."""
    if not changes:
        return ""
    lines = [f"{_INDENT}~ signed metadata changes:"]
    for name, old, new in changes:
        lines.append(f"{_INDENT}   {name}:")
        lines.append(f"{_INDENT}     admitted: {'(not advertised)' if old is None else old!r}")
        lines.append(f"{_INDENT}     live:     {'(not advertised)' if new is None else new!r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# First admission — the wrap-time review
# ---------------------------------------------------------------------------


def render_input_schema(schema: dict) -> str:
    """A compact field listing for a definition with no baseline to diff."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not properties:
        return f"{_INDENT}input_schema: (no properties advertised)"
    parts = []
    for name in sorted(properties):
        subschema = properties[name]
        kind = subschema.get("type", "<untyped>") if isinstance(subschema, dict) else "<untyped>"
        parts.append(f"{name} ({kind}{', REQUIRED' if name in required else ''})")
    return f"{_INDENT}input_schema: " + ", ".join(parts)


def render_proposal(proposal: ToolOpProposal) -> str:
    """The proposed classification, each field labelled with its provenance.

    The labelling is the honesty: a reviewer must be able to see that
    `effect: read` rests on the server's own say-so while `external: true`
    rests on a fact tegh established. Rendering the values alone would present
    an untrusted claim and a transport fact in the same voice.
    """
    marks = {
        Basis.TRANSPORT: "FACT     ",
        Basis.ANNOTATION: "UNTRUSTED",
        Basis.RESTRICTIVE_DEFAULT: "DEFAULT  ",
        # Rendered distinctly for the same reason the other three are: a value a
        # human chose is a different kind of thing from one a server claimed or
        # one that fell to a default, and showing them in one voice is exactly
        # what this function exists not to do.
        Basis.OPERATOR: "YOU SET  ",
    }
    corrected = any(field.is_operator_set for _, field in proposal.fields)
    heading = (
        "classification AS CORRECTED — CONFIRM before it binds:"
        if corrected
        else "proposed classification — CONFIRM before it binds:"
    )
    lines = [f"{_INDENT}{heading}"]
    for name, proposed in proposal.fields:
        shown = "(none)" if proposed.value is None else str(proposed.value).lower()
        lines.append(
            f"{_INDENT}   {name:<11}{shown:<8} [{marks[proposed.basis]}] {proposed.reason}"
        )
    if proposal.rests_on_untrusted_claim:
        lines.append(
            f"{_INDENT}   NB fields marked UNTRUSTED come from the server's own "
            "annotations —"
        )
        lines.append(
            f"{_INDENT}      a hostile server can claim readOnlyHint on a tool that "
            "deletes."
        )
    if proposal.unknown_hints:
        lines.append(
            f"{_INDENT}   unrecognized annotation keys (not acted on): "
            + ", ".join(proposal.unknown_hints)
        )
    return "\n".join(lines)


def render_first_admission(
    tool_def: McpToolDef,
    proposal: ToolOpProposal,
    *,
    url: Optional[str] = None,
) -> str:
    """One tool, as offered for admission at `tegh wrap`. No baseline exists.

    Description leads, for the same reason it leads in a diff: it is the thing
    a human is least likely to read carefully and most needs to. On a remote
    server the required-argument set is also called out — its FIRST admission
    is when a disclosure channel is opened, and the amendment's logic applies to opening
    one at least as much as to widening it.
    """
    lines = [f"{_INDENT}--- {tool_def.server_id}/{tool_def.tool_name} ---"]
    if tool_def.title:
        lines.append(f"{_INDENT}title: {tool_def.title}")
    lines.append(render_description_verbatim(tool_def.description))
    lines.append(render_input_schema(tool_def.input_schema))

    if url is not None:
        required = sorted(tool_def.input_schema.get("required") or [])
        lines.append(
            f"{_INDENT}REMOTE — admitting this opens a disclosure channel to {url}."
        )
        lines.append(
            f"{_INDENT}   sent on every call (required): "
            + (", ".join(required) if required else "(no required arguments)")
        )
    lines.append(render_proposal(proposal))
    lines.append(f"{_INDENT}def_hash: {compute_tool_def_hash(tool_def)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verify against the lock
# ---------------------------------------------------------------------------


def render_tool_delta(delta: ToolDelta, *, url: Optional[str] = None) -> str:
    """One drifted tool, tiered per TL11 + the disclosure amendment.

    Order is DISCLOSURE, then STEERING, then CONTRACT. The two top-tier
    sections each require their own acknowledgment, so their relative order is
    legibility only — a reviewer meets the escalation before the prose.
    """
    lines = [f"{_INDENT}--- {delta.live.server_id}/{delta.tool_name} — DRIFT ---"]
    if delta.disclosure_fields:
        lines.append(render_disclosure_escalation(delta, url=url))
    if delta.description_changed:
        lines.append(render_description_delta(delta.admitted.description, delta.live.description))
    contract = render_schema_summary(
        delta.schema_delta, suppress=tuple(delta.disclosure_fields)
    )
    if contract:
        lines.append(contract)
    metadata = render_metadata_changes(delta.metadata_changes)
    if metadata:
        lines.append(metadata)
    lines.append(f"{_INDENT}admitted def_hash: {compute_tool_def_hash(delta.admitted)}")
    lines.append(f"{_INDENT}live     def_hash: {compute_tool_def_hash(delta.live)}")
    return "\n".join(lines)


def render_ack_requirements(deltas: list[ToolDelta]) -> str:
    """Name exactly which acknowledgments a caller still owes.

    Stated as flags rather than a count so the message is directly actionable,
    and refused BEFORE any write is burned (`bulk-ratify`'s posture).
    """
    disclosure = [d.tool_name for d in deltas if d.disclosure_fields]
    steering = [d.tool_name for d in deltas if d.description_changed]
    if not disclosure and not steering:
        return ""
    lines = ["Per-tool acknowledgment required (--yes does NOT cover these):"]
    for name in sorted(disclosure):
        lines.append(f"  --acknowledge-disclosure-change {name}")
    for name in sorted(steering):
        lines.append(f"  --acknowledge-description-change {name}")
    return "\n".join(lines)
