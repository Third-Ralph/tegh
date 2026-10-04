"""Propose a `ToolOp` classification from a tool's advertised annotations.

[ruling: maintainer, 2026-07-25] MCP servers advertise `annotations` — `readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`. tegh uses them to
*propose* a classification, renders the proposal, and binds only what a human
confirms. This module is the proposing half; `review.py` renders it and
`cli.py` collects the confirmation.

**Annotations are untrusted input.** A swapped or hostile server can claim
`readOnlyHint: true` for a tool that deletes, and every hint here arrives over
the same wire as the description tegh treats as injection surface. Two things
— and only two — make the proposal safe to offer at all:

1. **A human ratifies it.** An annotation never becomes a classification on its
   own authority. That is the whole reason this module returns a *proposal*
   carrying its reasoning rather than a `ToolOp` carrying none.
2. **Annotations ride the signed set.** A server that later flips a
   hint breaks `compute_tool_def_hash`, quarantines the tool at discovery, and
   brings it back for re-vet. The suggestion cannot be changed after the fact
   without a visible drift.

So the proposal is a labour-saver for the human, never a trust decision. What
makes that honest rather than merely stated is `Basis`: every proposed field
records WHERE its value came from, and the review renders that alongside the
value. A reviewer must be able to see at a glance that `effect: read` rests on
the server's own say-so while `external: true` rests on a fact tegh established
itself.

**Absence is not evidence of safety.** When a hint is missing the proposal
falls to the restrictive end (`effect: write`, `external: true`,
`reversible: false`) rather than to a convenient middle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Optional

from safe_agents.broker.schemas import McpToolDef, ToolOp

#: The four hints the MCP specification defines for `annotations`. Kept as an
#: explicit tuple rather than "whatever keys the server sent": an unknown key
#: is not a hint tegh understands, and silently reading one would be trusting
#: untrusted input to name its own semantics.
KNOWN_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


class Basis(str, Enum):
    """Where a proposed field's value came from. A CLOSED catalog.

    The three members are ordered by how much they may be trusted, and the
    distinction is the point of this module:

    - `TRANSPORT` — derived from tegh's OWN confirmed server declaration. A
      fact about the connection tegh will make, not a claim the server made.
      Nothing the server says can move it.
    - `ANNOTATION` — the server's own advertised hint. UNTRUSTED: it is a
      claim by the party being classified, about itself. Safe to *show*, never
      safe to bind without a human.
    - `RESTRICTIVE_DEFAULT` — no usable hint was advertised, so the field fell
      to its safe end. Distinct from `ANNOTATION` because "the server said
      nothing" and "the server asserted this" must not look alike to a
      reviewer: one is silence, the other is a claim that could be a lie.
    """

    TRANSPORT = "transport"
    ANNOTATION = "annotation"
    RESTRICTIVE_DEFAULT = "restrictive-default"
    #: A human reviewing this wrap SET this value, overriding what was proposed.
    #: The fourth member exists because the module's own promise — "a
    #: human ratifies it" — was only half-true: the review could accept or skip
    #: a proposal but never correct one, so a server advertising no annotations
    #: produced restrictive defaults nobody could talk down, and every call to
    #: it was held for approval forever. Ranked above ANNOTATION and
    #: RESTRICTIVE_DEFAULT: it is the one basis that is neither a guess nor a
    #: claim by the party being classified.
    OPERATOR = "operator"


@dataclass(frozen=True)
class ProposedField:
    """One field of a proposed `ToolOp`, with the reasoning that produced it."""

    value: Any
    basis: Basis
    #: Human-readable justification, rendered next to the value in the review.
    reason: str

    @property
    def is_untrusted(self) -> bool:
        """Does this value rest on something the classified server asserted?"""
        return self.basis is Basis.ANNOTATION

    @property
    def is_operator_set(self) -> bool:
        """Did a human override the proposal for this field?"""
        return self.basis is Basis.OPERATOR


@dataclass(frozen=True)
class ToolOpProposal:
    """A proposed classification plus, per field, why it was proposed.

    `tool_op` is a fully-formed `ToolOp` so the confirmation path can bind it
    unchanged when a human accepts it as-is — the common case should not
    require reassembling anything. The per-field records exist for the review
    rendering and are deliberately not folded into the `ToolOp`: the base's
    classification type carries a decision, not the argument for it, and
    widening it with tegh's review scratch would put a product concern into a
    broker schema.
    """

    tool_op: ToolOp
    effect: ProposedField
    external: ProposedField
    reversible: ProposedField
    egress_arg: ProposedField
    #: Advertised `annotations` keys tegh does not know. Surfaced rather than
    #: ignored: an unrecognized hint may carry meaning this version cannot act
    #: on, and silence about it reads as "nothing else was advertised".
    unknown_hints: list[str]

    @property
    def fields(self) -> list[tuple[str, ProposedField]]:
        return [
            ("effect", self.effect),
            ("external", self.external),
            ("reversible", self.reversible),
            ("egress_arg", self.egress_arg),
        ]

    @property
    def rests_on_untrusted_claim(self) -> bool:
        """Does ANY proposed field rest on the server's own assertion?

        The review leads with this: a proposal built entirely from transport
        facts and restrictive defaults is a different thing to confirm than one
        where the server talked tegh down from the safe end.
        """
        return any(field.is_untrusted for _, field in self.fields)


def _hint(annotations: Optional[dict], name: str) -> Optional[bool]:
    """Read one BOOLEAN hint. A non-boolean value is treated as absent.

    Deliberately strict: `"readOnlyHint": "true"` is not a boolean, and
    coercing it would let a server pick the permissive branch with a value the
    MCP specification does not define. Absent means the restrictive default
    applies, which is the correct handling of a malformed claim.
    """
    if not isinstance(annotations, dict):
        return None
    value = annotations.get(name)
    return value if isinstance(value, bool) else None


def propose_tool_op(
    tool_def: McpToolDef,
    *,
    transport: Literal["stdio", "streamable-http"],
) -> ToolOpProposal:
    """Propose a `ToolOp` for one discovered tool. Pure — no I/O, no store.

    The coordinate follows the MCP convention `tool=server_id`, `op=tool_name`
    (MCP-HOST.md), which is what makes response-taint and the per-op budget
    counters work with no MCP-specific arm — and is what `LockedTool` validates
    on parse.

    `transport` comes from tegh's own confirmed declaration rather than from
    anything the server said, which is why it can outrank a hint.
    """
    annotations = tool_def.annotations
    unknown = sorted(
        key for key in (annotations or {}) if key not in KNOWN_HINTS
    ) if isinstance(annotations, dict) else []

    # -- effect ------------------------------------------------------------
    read_only = _hint(annotations, "readOnlyHint")
    if read_only is True:
        effect = ProposedField(
            "read",
            Basis.ANNOTATION,
            "the server advertises readOnlyHint: true — its own claim that this "
            "tool changes nothing",
        )
    elif read_only is False:
        effect = ProposedField(
            "write", Basis.ANNOTATION, "the server advertises readOnlyHint: false"
        )
    else:
        effect = ProposedField(
            "write",
            Basis.RESTRICTIVE_DEFAULT,
            "no readOnlyHint advertised; a missing hint is not evidence of safety",
        )

    # -- external ----------------------------------------------------------
    # NEVER hint-derived, and never False. An MCP tool crosses a trust boundary
    # by construction: the call leaves this process for a server whose code we
    # do not control, and whose response is injection surface on the way back.
    # The base enforces exactly this — `AgentManifest` refuses a declared MCP
    # tool whose ToolOp is not `external=True` (two-key completeness), so
    # a hint-lowered `external: False` would produce a manifest the wrap cannot
    # even write. `openWorldHint` therefore describes whether the SERVER reaches
    # the wider internet, which is a different question from whether the AGENT
    # is crossing a boundary to reach the server; it is reported, never acted on.
    open_world = _hint(annotations, "openWorldHint")
    if transport == "streamable-http":
        reason = (
            "this is a REMOTE server — every call crosses to a third party by "
            "construction, regardless of what it advertises"
        )
    else:
        reason = (
            "an MCP tool always crosses a trust boundary: the call leaves this "
            "process for code we do not control"
        )
        if open_world is False:
            reason += (
                ", and the server's openWorldHint: false claim does not lower it "
                "(that hint is about the server's own reach, not this boundary)"
            )
    external = ProposedField(True, Basis.TRANSPORT, reason)

    # -- reversible --------------------------------------------------------
    # `ToolOp.reversible` is "not applicable" for a read, and the base's own
    # comment says so — proposing False there would assert a fact about a
    # question that was not asked.
    if effect.value == "read":
        # The basis is INHERITED from `effect`, deliberately: this field is
        # not-applicable only because the tool was classified a read, so when
        # that classification rests on the server's own claim, so does this
        # field's absence. Saying so is the difference between a reviewer
        # reading "n/a" as settled and reading it as contingent on a hint.
        reversible = ProposedField(
            None,
            effect.basis,
            "not applicable to a read"
            + (
                " — and 'read' here is the server's own claim, so this absence "
                "rests on it too"
                if effect.basis is Basis.ANNOTATION
                else ""
            ),
        )
    else:
        destructive = _hint(annotations, "destructiveHint")
        if destructive is False:
            reversible = ProposedField(
                True,
                Basis.ANNOTATION,
                "the server advertises destructiveHint: false — its own claim "
                "that a mistake here is recoverable",
            )
        elif destructive is True:
            reversible = ProposedField(
                False, Basis.ANNOTATION, "the server advertises destructiveHint: true"
            )
        else:
            reversible = ProposedField(
                False,
                Basis.RESTRICTIVE_DEFAULT,
                "no destructiveHint advertised; assume a mistake is neither "
                "undoable nor bounded",
            )

    # -- egress_arg --------------------------------------------------------
    # Never proposed. `egress_arg` names WHICH argument's bytes are metered
    # against the envelope's egress budget, and no annotation carries that. A
    # guess would be worse than a gap in both directions: name the wrong arg
    # and the real channel goes unmetered while a bound looks present; name any
    # arg on a tool that egresses nothing and the budget burns on a local call.
    # So it is left unset and the CONSEQUENCE is stated, rather than defaulted
    # quietly to the permissive end.
    egress_arg = ProposedField(
        None,
        Basis.RESTRICTIVE_DEFAULT,
        "not proposed — no annotation names which argument egresses. Egress "
        "metering stays OFF for this tool unless you name one"
        + (" (worth naming: this tool reaches an external party)" if external.value else ""),
    )

    return ToolOpProposal(
        tool_op=ToolOp(
            tool=tool_def.server_id,
            op=tool_def.tool_name,
            effect=effect.value,
            external=external.value,
            reversible=reversible.value,
            egress_arg=egress_arg.value,
        ),
        effect=effect,
        external=external,
        reversible=reversible,
        egress_arg=egress_arg,
        unknown_hints=unknown,
    )


# ---------------------------------------------------------------------------
# Correction — the ratification half
# ---------------------------------------------------------------------------

#: What a human may change about a proposal. A CLOSED set, and `external` is
#: deliberately absent: an MCP tool crosses a trust boundary by construction and
#: the base REFUSES a declared MCP tool whose ToolOp is not `external=True`
#: (two-key completeness). Offering it as editable would offer a value
#: that cannot be written — worse than not offering it, because the refusal
#: would arrive long after the human believed they had decided.
CORRECTABLE = ("effect", "reversible", "egress_arg")


class CorrectionRefused(ValueError):
    """A requested correction is not one the base would accept."""


def apply_corrections(
    proposal: ToolOpProposal,
    *,
    effect: Optional[Literal["read", "write"]] = None,
    reversible: Optional[bool] = None,
    egress_arg: Optional[str] = None,
    clear_egress_arg: bool = False,
) -> ToolOpProposal:
    """Return `proposal` with the named fields set by a human.

    Only fields explicitly passed are changed; everything else keeps its
    proposed value AND its basis, so a reviewer who corrects one field does not
    silently launder the rest into looking human-checked.

    `clear_egress_arg` exists because `None` already means "not passed" here,
    and an operator must be able to say "no egress arg" as a decision rather
    than only by leaving it alone. Overloading `None` for both would make the
    two indistinguishable — which is the same ambiguity `reversible` carries in
    the base and the reason a read's reversible is `None` rather than `False`.

    Correcting `effect` to `read` also clears `reversible`, because the base
    treats reversibility as not-applicable to a read; leaving a stale `False`
    behind would assert a fact about a question that was not asked.
    """
    fields = {
        "effect": proposal.effect,
        "reversible": proposal.reversible,
        "egress_arg": proposal.egress_arg,
    }

    if effect is not None:
        if effect not in ("read", "write"):
            raise CorrectionRefused(
                f"effect must be 'read' or 'write', not {effect!r}"
            )
        fields["effect"] = ProposedField(
            effect,
            Basis.OPERATOR,
            f"set to {effect!r} by the human reviewing this wrap, overriding the "
            f"proposed {proposal.effect.value!r} ({proposal.effect.basis.value})",
        )
        if effect == "read":
            fields["reversible"] = ProposedField(
                None,
                Basis.OPERATOR,
                "not applicable — this tool was classified a read by the reviewer",
            )

    if reversible is not None:
        if fields["effect"].value == "read":
            raise CorrectionRefused(
                "reversible is not applicable to a read — classify this tool a "
                "write first, or leave reversible alone"
            )
        fields["reversible"] = ProposedField(
            reversible,
            Basis.OPERATOR,
            f"set to {reversible!r} by the human reviewing this wrap, overriding "
            f"the proposed {proposal.reversible.value!r} "
            f"({proposal.reversible.basis.value})",
        )

    if clear_egress_arg:
        fields["egress_arg"] = ProposedField(
            None, Basis.OPERATOR, "the reviewer named no egress argument"
        )
    elif egress_arg is not None:
        fields["egress_arg"] = ProposedField(
            egress_arg,
            Basis.OPERATOR,
            f"the reviewer named {egress_arg!r} as the argument whose bytes are "
            "metered against the envelope's egress budget",
        )

    return ToolOpProposal(
        tool_op=ToolOp(
            tool=proposal.tool_op.tool,
            op=proposal.tool_op.op,
            effect=fields["effect"].value,
            # NOT correctable — see CORRECTABLE.
            external=proposal.external.value,
            reversible=fields["reversible"].value,
            egress_arg=fields["egress_arg"].value,
        ),
        effect=fields["effect"],
        external=proposal.external,
        reversible=fields["reversible"],
        egress_arg=fields["egress_arg"],
        unknown_hints=list(proposal.unknown_hints),
    )
