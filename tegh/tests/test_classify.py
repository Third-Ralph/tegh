"""ToolOp proposals from advertised annotations (`tegh/classify.py`).

The proposal is a labour-saver, not a trust decision — a human ratifies it and
a later hint flip breaks `def_hash` and forces a re-vet. What these
tests pin is the part that makes offering it honest:

- **Absence falls to the restrictive end.** A missing hint is not evidence of
  safety, and every default here is the safe one.
- **Provenance is recorded per field.** A value resting on the server's own
  claim must be distinguishable from one resting on a transport fact — the
  review renders the difference, and it can only do that if the proposal
  carries it.
- **The transport outranks any hint.** A remote server is a third party
  whatever it advertises about itself.

Pure: no I/O, no store, no network.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from safe_agents.broker.schemas import McpToolDef
from tegh.classify import (
    CORRECTABLE,
    Basis,
    CorrectionRefused,
    apply_corrections,
    propose_tool_op,
)


def _def(annotations: Optional[dict] = None, **over: Any) -> McpToolDef:
    fields: dict[str, Any] = {
        "server_id": "fs",
        "tool_name": "read_file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "description": "Read a file.",
        "annotations": annotations,
    }
    fields.update(over)
    return McpToolDef(**fields)


class TestRestrictiveDefaults:
    """A missing annotation is not evidence of safety."""

    def test_no_annotations_at_all_lands_on_the_safe_end(self) -> None:
        proposal = propose_tool_op(_def(None), transport="stdio")
        assert proposal.tool_op.effect == "write"
        assert proposal.tool_op.external is True
        assert proposal.tool_op.reversible is False
        # `external` is a structural FACT rather than a default — it is the one
        # field no annotation may move, in either direction.
        assert [
            name
            for name, f in proposal.fields
            if f.basis is not Basis.RESTRICTIVE_DEFAULT and name != "external"
        ] == []
        assert proposal.external.basis is Basis.TRANSPORT
        assert not proposal.rests_on_untrusted_claim

    def test_empty_annotations_object_is_the_same_as_none(self) -> None:
        assert propose_tool_op(_def({}), transport="stdio").tool_op == propose_tool_op(
            _def(None), transport="stdio"
        ).tool_op

    @pytest.mark.parametrize("bogus", ["true", 1, None, [], {"nested": True}])
    def test_a_non_boolean_hint_is_treated_as_absent(self, bogus: Any) -> None:
        """A server must not reach the permissive branch with a value the MCP
        specification does not define — coercing `"true"` would let it."""
        proposal = propose_tool_op(_def({"readOnlyHint": bogus}), transport="stdio")
        assert proposal.tool_op.effect == "write"
        assert proposal.effect.basis is Basis.RESTRICTIVE_DEFAULT


class TestAnnotationsArePermissiveOnlyViaTheirOwnClaim:
    def test_read_only_hint_proposes_read_and_is_marked_untrusted(self) -> None:
        proposal = propose_tool_op(_def({"readOnlyHint": True}), transport="stdio")
        assert proposal.tool_op.effect == "read"
        assert proposal.effect.basis is Basis.ANNOTATION
        assert proposal.effect.is_untrusted
        assert proposal.rests_on_untrusted_claim

    def test_a_read_carries_no_reversible_claim(self) -> None:
        """`ToolOp.reversible` is not applicable to a read — proposing False
        would answer a question nobody asked."""
        proposal = propose_tool_op(_def({"readOnlyHint": True}), transport="stdio")
        assert proposal.tool_op.reversible is None

    def test_destructive_hint_false_proposes_reversible(self) -> None:
        proposal = propose_tool_op(
            _def({"readOnlyHint": False, "destructiveHint": False}), transport="stdio"
        )
        assert proposal.tool_op.effect == "write"
        assert proposal.tool_op.reversible is True
        assert proposal.reversible.basis is Basis.ANNOTATION

    def test_open_world_false_cannot_lower_external(self) -> None:
        """An MCP tool crosses a trust boundary by construction, and the base
        refuses a declared MCP tool whose ToolOp is not external=True
        (two-key completeness) — so a hint-lowered False would produce a
        manifest the wrap could not even write."""
        proposal = propose_tool_op(_def({"openWorldHint": False}), transport="stdio")
        assert proposal.tool_op.external is True
        assert proposal.external.basis is Basis.TRANSPORT
        assert "does not lower it" in proposal.external.reason


class TestTransportOutranksAnnotations:
    """A remote server IS a third party receiving every call, whatever it says."""

    def test_remote_is_external_even_when_the_server_denies_it(self) -> None:
        proposal = propose_tool_op(
            _def({"openWorldHint": False}), transport="streamable-http"
        )
        assert proposal.tool_op.external is True
        assert proposal.external.basis is Basis.TRANSPORT
        assert not proposal.external.is_untrusted

    def test_remote_external_holds_with_no_annotations(self) -> None:
        proposal = propose_tool_op(_def(None), transport="streamable-http")
        assert proposal.tool_op.external is True
        assert proposal.external.basis is Basis.TRANSPORT


class TestEgressArgIsNeverGuessed:
    def test_not_proposed_and_the_consequence_is_stated(self) -> None:
        """Naming the wrong arg is worse than naming none: the real channel
        goes unmetered while a bound looks present."""
        proposal = propose_tool_op(_def({"openWorldHint": True}), transport="stdio")
        assert proposal.tool_op.egress_arg is None
        assert "OFF" in proposal.egress_arg.reason

    def test_an_external_tool_says_naming_one_is_worth_it(self) -> None:
        proposal = propose_tool_op(_def(None), transport="streamable-http")
        assert "external party" in proposal.egress_arg.reason


class TestCoordinateAndUnknownHints:
    def test_coordinate_follows_the_mcp_convention(self) -> None:
        """tool=server_id, op=tool_name — what `LockedTool` validates on parse."""
        proposal = propose_tool_op(
            _def(None, server_id="ctx7", tool_name="resolve"), transport="stdio"
        )
        assert (proposal.tool_op.tool, proposal.tool_op.op) == ("ctx7", "resolve")

    def test_unknown_annotation_keys_are_surfaced_not_ignored(self) -> None:
        proposal = propose_tool_op(
            _def({"readOnlyHint": True, "vendorHint": "x", "another": 1}),
            transport="stdio",
        )
        assert proposal.unknown_hints == ["another", "vendorHint"]

    def test_known_hints_are_not_reported_as_unknown(self) -> None:
        proposal = propose_tool_op(
            _def({"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
            transport="stdio",
        )
        assert proposal.unknown_hints == []


class TestCorrections:
    """The ratification half. A human must be able to DISAGREE.

    Before this, review could accept or skip a proposal but never correct one,
    so a server advertising no annotations produced restrictive defaults nobody
    could talk down and every call to it was held for approval forever.
    """

    def _proposal(self, **annotations):
        definition = McpToolDef(
            server_id="ledger",
            tool_name="get_entry",
            input_schema={"type": "object"},
            description="Read one entry.",
            annotations=annotations or None,
        )
        return propose_tool_op(definition, transport="stdio")

    def test_an_unannotated_tool_proposes_the_restrictive_end(self) -> None:
        """The premise. Correct on its own terms, and unusable without a fix."""
        proposal = self._proposal()
        assert proposal.tool_op.effect == "write"
        assert proposal.tool_op.reversible is False
        assert proposal.effect.basis is Basis.RESTRICTIVE_DEFAULT

    def test_correcting_effect_to_read_binds_the_correction(self) -> None:
        corrected = apply_corrections(self._proposal(), effect="read")
        assert corrected.tool_op.effect == "read"
        assert corrected.effect.basis is Basis.OPERATOR
        assert "overriding the proposed 'write'" in corrected.effect.reason

    def test_correcting_to_read_clears_reversible(self) -> None:
        """The base treats reversibility as not-applicable to a read; leaving a
        stale False behind would assert a fact about a question not asked."""
        corrected = apply_corrections(self._proposal(), effect="read")
        assert corrected.tool_op.reversible is None
        assert corrected.reversible.basis is Basis.OPERATOR

    def test_reversible_on_a_read_is_refused(self) -> None:
        corrected = apply_corrections(self._proposal(), effect="read")
        with pytest.raises(CorrectionRefused, match="not applicable to a read"):
            apply_corrections(corrected, reversible=True)

    def test_untouched_fields_keep_their_basis(self) -> None:
        """Correcting one field must not launder the others into looking
        human-checked — that would be the same one-voice failure the whole
        module exists to avoid."""
        corrected = apply_corrections(self._proposal(), effect="read")
        assert corrected.external.basis is Basis.TRANSPORT
        assert corrected.egress_arg.basis is Basis.RESTRICTIVE_DEFAULT

    def test_external_is_not_correctable(self) -> None:
        """An MCP tool crosses a trust boundary by construction, and the base
        REFUSES a declared MCP tool that is not external=True. Offering it would
        offer a value that cannot be written."""
        assert "external" not in CORRECTABLE
        corrected = apply_corrections(self._proposal(), effect="read")
        assert corrected.tool_op.external is True

    def test_an_operator_can_name_an_egress_arg_and_can_clear_one(self) -> None:
        named = apply_corrections(self._proposal(), egress_arg="query")
        assert named.tool_op.egress_arg == "query"
        assert named.egress_arg.basis is Basis.OPERATOR

        cleared = apply_corrections(named, clear_egress_arg=True)
        assert cleared.tool_op.egress_arg is None
        assert cleared.egress_arg.basis is Basis.OPERATOR

    def test_a_bad_effect_is_refused_rather_than_coerced(self) -> None:
        with pytest.raises(CorrectionRefused):
            apply_corrections(self._proposal(), effect="readonly")

    def test_a_correction_can_also_TIGHTEN_a_servers_claim(self) -> None:
        """Not only a loosening affordance. A server claiming readOnlyHint on
        something the reviewer knows writes is exactly the hostile case the
        module warns about, and the fix must run in that direction too."""
        claimed_read = self._proposal(readOnlyHint=True)
        assert claimed_read.tool_op.effect == "read"

        tightened = apply_corrections(claimed_read, effect="write", reversible=False)
        assert tightened.tool_op.effect == "write"
        assert tightened.tool_op.reversible is False
        assert tightened.effect.basis is Basis.OPERATOR
