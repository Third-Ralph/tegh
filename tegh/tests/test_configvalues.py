"""Conformance for the literal-value classification (`tegh/configvalues.py`).

The module exists because tegh cannot tell a credential from a path, so a human
answers per coordinate. What these tests pin is what makes asking safe:

- **The value is never lifted.** Inventory, prompting, persistence and refusal
  all run on names and derived FACTS. `read_cleared_env` is the only function
  allowed to return a value, and only for a coordinate a human cleared — so
  every other surface asserting "no value here" is the property, not a detail.
- **A decision is bound to the value it was made about, at the scope it was
  made at.** Either half alone is a heuristic: the coordinate alone is a name
  rule the operator trained by hand, and an unscoped coordinate collides with
  the same server name in Claude Code's other block of the SAME file.
- **An unreadable record resolves to EMPTY.** Emptiness is what makes the next
  wrap ask again; any other reading of a file tegh cannot parse is a guess that
  something was already cleared.

Every literal here is deliberately un-secret-shaped (`a-literal-value`). Nothing
in the module keys on a value's SHAPE, so a realistic-looking key would buy no
coverage and would force an exemption from the repo's secret scanner.

Pure apart from `tmp_path`: no store, no network.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from tegh.configvalues import (
    DecisionRecord,
    LiteralField,
    ValueClass,
    _names_existing_path,
    cleared_coordinates,
    describe,
    inventory,
    literal_fields,
    load_record,
    read_cleared_env,
    render_refusal,
    render_undelivered_references,
    save_record,
    uncleared,
    undeliverable,
)

#: Not secret-shaped, on purpose (see the module docstring).
VALUE = "a-literal-value"
#: Pinned, because a record on disk must keep clearing the same value after an
#: upgrade. Moving the digest basis silently re-asks every answered field.
VALUE_SHA256 = "b9c67f430c2b6cb0528dbdbe4bce9acd3c0a41198adc72a48eb1d29468dfe8ef"


def _field(
    scope: str = "local",
    server_id: str = "notes",
    block: str = "env",
    name: str = "TOKEN",
    value: str = VALUE,
) -> LiteralField:
    """The one LiteralField in a single-value block — built through the parser,
    never by hand, so these tests exercise the derivation too."""
    found = literal_fields(server_id, {block: {name: value}}, scope=scope)
    assert len(found) == 1
    return found[0]


def _recorded(*fields: LiteralField) -> DecisionRecord:
    record = DecisionRecord.empty()
    for field in fields:
        record.record(field, decided_at="2026-07-27T00:00:00+00:00", decided_by="someone")
    return record


def _renderings(field: LiteralField) -> dict[str, str]:
    """Every surface a value could leak through, keyed by where it came from."""
    return {
        "repr": repr(field),
        "str": str(field),
        "asdict": repr(dataclasses.asdict(field)),
        "describe": describe(field),
        "describe_changed": describe(field, changed=True),
        "refusal": render_refusal([field]),
        "record_json": _recorded(field).to_json(),
    }


class TestTheValueIsNeverCarried:
    """TL10 applied to config: describe a value, never quote it.

    `discovery.py` holds this by never reading a value at all. This module has
    to read one to hash it, so the property has to be re-established on the
    objects it hands back — every attribute of a `LiteralField` is DERIVED from
    the value and none of them is the value, so nothing downstream can print
    what it was never given.
    """

    def test_no_attribute_of_a_literal_field_is_the_value(self) -> None:
        field = _field()
        assert VALUE not in repr(field)
        for name, attribute in dataclasses.asdict(field).items():
            assert attribute != VALUE, name
            assert VALUE not in str(attribute), name

    @pytest.mark.parametrize(
        "surface",
        ["repr", "str", "asdict", "describe", "describe_changed", "refusal", "record_json"],
    )
    def test_no_rendering_of_a_field_contains_the_value(self, surface: str) -> None:
        """Prompt, refusal and persisted record are all built from the same
        object, so proving the object is value-free proves all three — but each
        is asserted anyway, because each is a place an operator's scrollback or
        `~/.tegh` ends up holding."""
        assert VALUE not in _renderings(_field())[surface]

    def test_a_headers_value_is_equally_unquoted(self) -> None:
        """The block a real credential is most likely to sit in."""
        field = _field(block="headers", name="Authorization", value=f"Bearer {VALUE}")
        for surface, rendering in _renderings(field).items():
            assert VALUE not in rendering, surface

    def test_inventory_of_a_whole_block_quotes_nothing(self) -> None:
        block = {
            "notes": {"command": "x", "env": {"A": VALUE, "B": "another-literal"}},
            "api": {"type": "http", "url": "https://x", "headers": {"H": VALUE}},
        }
        rendered = repr(inventory(block, scope="local"))
        assert VALUE not in rendered and "another-literal" not in rendered


class TestTheValueBinding:
    """A decision is about a VALUE, and the digest is how that is checked."""

    def test_the_digest_is_pinned_so_an_existing_record_still_clears(self) -> None:
        assert _field().value_sha256 == VALUE_SHA256

    def test_the_same_value_at_two_coordinates_digests_the_same(self) -> None:
        """The digest describes the value alone; the coordinate is carried
        separately, and `clears` requires both to match."""
        assert _field(server_id="notes").value_sha256 == _field(server_id="other").value_sha256

    def test_a_different_value_digests_differently(self) -> None:
        assert _field(value="something-else").value_sha256 != VALUE_SHA256


class TestScopeIsPartOfTheCoordinate:
    """Claude Code's LOCAL and USER scopes are two blocks in the SAME file, and
    scopes shadow rather than merge — so one server name can exist twice with
    two different values, either of which may be a credential.

    This is the sharpest property in the module: an unqualified coordinate would
    let a decision about the local value silently clear the user-scope one, and
    the operator would never be asked about a value tegh then displaces.
    """

    def test_the_same_name_at_two_scopes_is_two_coordinates(self) -> None:
        local = _field(scope="local", value="/tmp/local.json")
        user = _field(scope="user", value=VALUE)
        assert local.coordinate != user.coordinate
        assert local.coordinate.startswith("local:") and user.coordinate.startswith("user:")

    def test_clearing_one_scope_does_not_clear_the_other(self) -> None:
        """Not even for an identical value: the two are different config that a
        wrap displaces separately, and the operator answered about one of them."""
        local = _field(scope="local")
        user = _field(scope="user")
        record = _recorded(local)

        assert record.clears(local)
        assert not record.clears(user)

    def test_the_other_scope_is_a_FIRST_question_not_a_changed_one(self) -> None:
        """A scope nobody has answered must read as new. Reporting "this
        changed" would tell the operator they had already seen it."""
        record = _recorded(_field(scope="local", value="/tmp/local.json"))
        user = _field(scope="user", value=VALUE)

        assert not record.was_decided_about_another_value(user)

    def test_the_authorized_read_is_scoped_too(self) -> None:
        """The gate and the read must agree on the coordinate, or a clearance
        granted at one scope would lift the other scope's value."""
        block = {"notes": {"env": {"TOKEN": VALUE}}}
        assert read_cleared_env(block, ["local:notes.env.TOKEN"], scope="local") == {
            "notes": {"TOKEN": VALUE}
        }
        assert read_cleared_env(block, ["local:notes.env.TOKEN"], scope="user") == {}

    def test_a_two_scope_inventory_leaves_every_field_distinctly_addressable(self) -> None:
        block = {"notes": {"env": {"TOKEN": VALUE}}}
        fields = inventory(block, scope="local") + inventory(block, scope="user")
        assert len({f.coordinate for f in fields}) == 2


class TestWhatCountsAsLiteral:
    """`${VAR}` holds a NAME; the harness expands it at load. Anything else in
    the value is material sitting in the config file."""

    @pytest.mark.parametrize(
        "value",
        [
            "${API_KEY}",
            "  ${API_KEY}  ",
            "${API_KEY:-default}",
            "  ${API_KEY:-default}  ",
            "\t${API_KEY}\n",
        ],
    )
    def test_a_whole_value_that_is_a_reference_is_not_literal(self, value: str) -> None:
        assert literal_fields("api", {"env": {"API_KEY": value}}, scope="local") == []

    @pytest.mark.parametrize(
        "value",
        [
            "Bearer ${API_KEY}",
            "${HOME}/notes.json",
            "${A}${B}",
            "prefix-${API_KEY}",
        ],
    )
    def test_a_value_merely_CONTAINING_a_reference_is_literal(self, value: str) -> None:
        """The surrounding text is material, and a value with two references is
        not a reference either — a permissive match here would wave through
        `Bearer <secret>` the moment it mentioned a variable."""
        assert [f.name for f in literal_fields("api", {"env": {"K": value}}, scope="local")] == ["K"]

    def test_an_empty_value_is_literal(self) -> None:
        """It is not a reference, so it is not exempt. Judging it harmless would
        be a shape heuristic, and this module has none."""
        assert [f.name for f in literal_fields("api", {"env": {"K": ""}}, scope="local")] == ["K"]


class TestInventoryEnumeratesWhatAWrapDisplaces:
    """From the config block, not from discovery's effective server list.

    A wrap moves every definition in the block into tegh's backup, including
    ones discovery shadowed or skipped for a transport it cannot represent. Any
    of those may hold a literal, so enumerating the shorter list would displace
    a value nobody was asked about.
    """

    def test_a_transport_tegh_cannot_pin_is_still_inventoried(self) -> None:
        block = {"legacy": {"type": "sse", "url": "https://x", "headers": {"H": VALUE}}}
        assert [f.coordinate for f in inventory(block, scope="local")] == [
            "local:legacy.headers.H"
        ]

    def test_a_server_with_no_env_or_headers_contributes_nothing(self) -> None:
        assert inventory({"notes": {"command": "notes-server"}}, scope="local") == []

    def test_both_secret_bearing_blocks_on_one_server_are_found(self) -> None:
        block = {"api": {"env": {"E": VALUE}, "headers": {"H": VALUE}}}
        assert [f.coordinate for f in inventory(block, scope="local")] == [
            "local:api.env.E",
            "local:api.headers.H",
        ]

    def test_a_block_tegh_was_not_told_to_judge_is_not_inventoried(self) -> None:
        """`SECRET_BEARING_BLOCKS` is closed. `args` can hold a credential too,
        and treating it as one by analogy is a judgement this module does not
        make — the refusal it feeds would then rest on an unstated rule."""
        block = {"api": {"args": ["--token", VALUE], "command": VALUE}}
        assert inventory(block, scope="local") == []

    def test_render_order_is_stable_regardless_of_config_order(self) -> None:
        """The operator reads this list and answers it item by item; an order
        that moves between runs makes a re-run unreviewable."""
        block = {
            "zeta": {"env": {"B": VALUE, "A": VALUE}},
            "alpha": {"headers": {"H": VALUE}, "env": {"E": VALUE}},
        }
        assert [f.coordinate for f in inventory(block, scope="local")] == [
            "local:alpha.env.E",
            "local:alpha.headers.H",
            "local:zeta.env.A",
            "local:zeta.env.B",
        ]

    @pytest.mark.parametrize(
        "entry",
        [
            {"env": {"N": 42, "F": 1.5, "T": True, "NESTED": {"a": "b"}, "NONE": None, "L": []}},
            {"env": "not-a-mapping"},
            {"env": None},
            {"headers": ["not", "a", "mapping"]},
            {},
        ],
        ids=["non-string-values", "env-is-a-string", "env-is-null", "headers-is-a-list", "empty"],
    )
    def test_a_shape_this_module_cannot_read_is_skipped_not_raised(
        self, entry: dict[str, Any]
    ) -> None:
        """A harness config is user-editable JSON, so an odd shape is a normal
        Tuesday. Raising here would abort the wrap over a field that holds no
        literal at all — and a traceback is not the refusal this module owes."""
        assert literal_fields("api", entry, scope="local") == []

    def test_a_server_entry_that_is_not_a_mapping_is_skipped(self) -> None:
        block = {"broken": "not-a-mapping", "notes": {"env": {"K": VALUE}}}
        assert inventory(block, scope="local") == [_field(name="K")]


class TestDecisionsAreBoundToBothCoordinateAndValue:
    """A clearance is about one value at one coordinate. Keyed on the coordinate
    alone it is a name heuristic the operator trained by hand — and it would
    silently carry a field whose value later became a credential."""

    def test_a_recorded_decision_clears_that_exact_field(self) -> None:
        field = _field()
        record = _recorded(field)
        assert record.clears(field)
        assert not record.was_decided_about_another_value(field)

    def test_a_changed_value_at_a_decided_coordinate_no_longer_clears(self) -> None:
        """The case a name-keyed record would answer wrongly and silently."""
        record = _recorded(_field())
        changed = _field(value="a-different-literal")

        assert not record.clears(changed)
        assert record.was_decided_about_another_value(changed)

    def test_a_never_seen_coordinate_is_a_first_question_not_a_changed_one(self) -> None:
        """The two prompts read differently to the operator, so conflating them
        would either cry wolf or bury a real change."""
        record = _recorded(_field())
        fresh = _field(server_id="other")

        assert not record.clears(fresh)
        assert not record.was_decided_about_another_value(fresh)

    def test_a_clearance_does_not_transfer_between_servers(self) -> None:
        """Same variable name on a second server is a second question."""
        record = _recorded(_field(server_id="notes"))
        assert not record.clears(_field(server_id="other"))

    def test_a_clearance_does_not_transfer_between_blocks(self) -> None:
        record = _recorded(_field(block="env", name="X"))
        assert not record.clears(_field(block="headers", name="X"))

    def test_forget_removes_a_decision_and_tolerates_an_absent_one(self) -> None:
        field = _field()
        record = _recorded(field)
        record.forget(field.coordinate)
        record.forget("local:never.env.SEEN")
        assert not record.clears(field)

    def test_uncleared_and_cleared_coordinates_partition_the_inventory(self) -> None:
        fields = inventory(
            {"notes": {"env": {"A": VALUE, "B": "another-literal"}}}, scope="local"
        )
        record = _recorded(fields[0])

        assert cleared_coordinates(fields, record) == ["local:notes.env.A"]
        assert [f.coordinate for f in uncleared(fields, record)] == ["local:notes.env.B"]

    def test_an_empty_record_leaves_everything_to_ask(self) -> None:
        """The default a caller that forgot to load a record gets: every field
        pending, none cleared."""
        fields = inventory({"notes": {"env": {"A": VALUE}}}, scope="local")
        assert uncleared(fields, DecisionRecord.empty()) == fields
        assert cleared_coordinates(fields, DecisionRecord.empty()) == []


class TestRecordParsingFailsTowardReAsking:
    """Anything this version cannot fully vouch for resolves to EMPTY.

    Empty is not a degraded outcome here — it is the outcome that asks the human
    again. The alternative reading of a record tegh cannot parse is that
    something was cleared, and acting on that guess is how a credential gets
    copied into the backup. So every assertion below is on EMPTINESS, and none
    of them is on an exception.
    """

    def test_a_record_round_trips(self) -> None:
        record = _recorded(_field(name="B"), _field(scope="user", name="A"))
        again = DecisionRecord.from_json(record.to_json())
        assert again.decisions == record.decisions

    def test_a_round_tripped_record_still_clears_the_same_field(self) -> None:
        field = _field()
        assert DecisionRecord.from_json(_recorded(field).to_json()).clears(field)

    def test_the_written_file_is_ordered_so_a_re_run_diffs_cleanly(self) -> None:
        record = _recorded(_field(name="Z"), _field(name="A"))
        written = json.loads(record.to_json())
        assert [d["coordinate"] for d in written["decisions"]] == [
            "local:notes.env.A",
            "local:notes.env.Z",
        ]

    def test_only_config_classifications_are_written(self) -> None:
        """No CREDENTIAL decision is persisted, not even its digest: it refuses
        the wrap either way, so a record would save nothing on the next run and
        would leave a hash of a low-entropy secret on disk."""
        record = _recorded(_field())
        written = json.loads(record.to_json())
        assert {d["classification"] for d in written["decisions"]} == {ValueClass.CONFIG.value}
        assert ValueClass.CREDENTIAL.value not in record.to_json()

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            "",
            "null",
            "[]",
            '"a string"',
            '{"decisions": []}',
            '{"version": 2, "decisions": []}',
            '{"version": "1", "decisions": []}',
            '{"version": 1, "decisions": {"local:notes.env.K": {}}}',
            '{"version": 1, "decisions": "everything"}',
            '{"version": 1}',
        ],
        ids=[
            "malformed",
            "empty-file",
            "json-null",
            "top-level-list",
            "top-level-string",
            "no-version",
            "future-version",
            "version-is-a-string",
            "decisions-is-a-mapping",
            "decisions-is-a-string",
            "decisions-absent",
        ],
    )
    def test_a_record_this_version_cannot_read_is_empty(self, raw: str) -> None:
        assert DecisionRecord.from_json(raw).decisions == {}

    @pytest.mark.parametrize(
        "entry",
        [
            {"coordinate": "local:notes.env.K", "value_sha256": VALUE_SHA256},
            {
                "coordinate": "local:notes.env.K",
                "value_sha256": VALUE_SHA256,
                "classification": "credential",
            },
            {
                "coordinate": "local:notes.env.K",
                "value_sha256": VALUE_SHA256,
                "classification": "CONFIG",
            },
            {"classification": "config", "value_sha256": VALUE_SHA256},
            {"classification": "config", "coordinate": "local:notes.env.K"},
            {"classification": "config", "coordinate": None, "value_sha256": VALUE_SHA256},
            {"classification": "config", "coordinate": "local:notes.env.K", "value_sha256": 12345},
            "not-a-mapping",
            None,
        ],
        ids=[
            "no-classification",
            "credential-classification",
            "wrong-case-classification",
            "no-coordinate",
            "no-digest",
            "null-coordinate",
            "non-string-digest",
            "entry-is-a-string",
            "entry-is-null",
        ],
    )
    def test_an_entry_this_version_did_not_write_clears_nothing(self, entry: Any) -> None:
        """Skipped rather than half-trusted: an entry with no digest cannot be
        checked against a value, and honouring it on its coordinate alone is
        exactly the name-keyed clearance the record exists to avoid."""
        raw = json.dumps({"version": 1, "decisions": [entry]})
        assert DecisionRecord.from_json(raw).decisions == {}

    def test_a_good_entry_beside_a_bad_one_still_loads(self) -> None:
        """Skipping is per entry. One unreadable line re-asks its own field, not
        every field the operator already answered."""
        raw = json.dumps(
            {
                "version": 1,
                "decisions": [
                    {
                        "classification": "config",
                        "coordinate": "local:a.env.K",
                        "value_sha256": VALUE_SHA256,
                    },
                    {"classification": "credential", "coordinate": "local:b.env.K", "value_sha256": "x"},
                ],
            }
        )
        assert list(DecisionRecord.from_json(raw).decisions) == ["local:a.env.K"]

    def test_an_absent_file_is_no_decisions_rather_than_an_error(self, tmp_path: Path) -> None:
        """A project's first wrap has no record, and that is the normal path."""
        assert load_record(tmp_path / "never-written.json").decisions == {}

    def test_a_saved_record_reloads_and_still_clears(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "decisions.json"
        field = _field()
        save_record(path, _recorded(field))

        assert load_record(path).clears(field)

    def test_a_corrupted_file_on_disk_re_asks_rather_than_raising(self, tmp_path: Path) -> None:
        path = tmp_path / "decisions.json"
        path.write_text("{half written", encoding="utf-8")
        assert load_record(path).decisions == {}


class TestTheAuthorizedRead:
    """`read_cleared_env` is the only function in tegh that returns a config
    value, and a coordinate's presence in `cleared` is what authorizes it."""

    BLOCK = {
        "notes": {
            "command": "notes-server",
            "env": {"MEMORY_FILE_PATH": "/tmp/m.json", "TOKEN": VALUE},
        },
        "api": {
            "type": "http",
            "url": "https://x",
            "headers": {"Authorization": VALUE},
            "env": {"REGION": "us-east-1"},
        },
    }

    def test_nothing_cleared_reads_nothing(self) -> None:
        assert read_cleared_env(self.BLOCK, [], scope="local") == {}

    def test_an_uncleared_sibling_on_the_same_server_is_absent(self) -> None:
        """Per coordinate, never per server. A server can hold both a path and a
        credential, and clearing the path must not lift the other one."""
        carried = read_cleared_env(
            self.BLOCK, ["local:notes.env.MEMORY_FILE_PATH"], scope="local"
        )
        assert carried == {"notes": {"MEMORY_FILE_PATH": "/tmp/m.json"}}
        assert "TOKEN" not in carried["notes"]

    def test_a_cleared_header_is_not_read(self) -> None:
        """`headers` has no static counterpart on `McpServerDecl`, so there is
        nowhere for it to go; the caller reports that rather than the reader
        handing back a value nothing will deliver."""
        assert read_cleared_env(
            self.BLOCK, ["local:api.headers.Authorization"], scope="local"
        ) == {}

    def test_a_coordinate_naming_no_field_reads_nothing(self) -> None:
        assert read_cleared_env(
            self.BLOCK, ["local:notes.env.ABSENT", "local:ghost.env.K"], scope="local"
        ) == {}

    def test_two_servers_are_kept_apart(self) -> None:
        carried = read_cleared_env(
            self.BLOCK, ["local:notes.env.TOKEN", "local:api.env.REGION"], scope="local"
        )
        assert carried == {"notes": {"TOKEN": VALUE}, "api": {"REGION": "us-east-1"}}

    def test_a_reference_value_is_returned_verbatim_and_unexpanded(self) -> None:
        """tegh does not expand a `${VAR}` — that would pull a live secret out
        of this process's environment, which is what discovery refuses to do.
        The unexpanded string is what the child would get, and the caller
        reports it as undelivered rather than the reader resolving it."""
        block = {"api": {"env": {"K": "${API_KEY}"}}}
        assert read_cleared_env(block, ["local:api.env.K"], scope="local") == {
            "api": {"K": "${API_KEY}"}
        }

    @pytest.mark.parametrize(
        "block",
        [
            {"api": "not-a-mapping"},
            {"api": {"env": "not-a-mapping"}},
            {"api": {"env": {"K": 42}}},
            {"api": {"headers": {"K": VALUE}}},
        ],
        ids=["entry-not-a-mapping", "env-not-a-mapping", "value-not-a-string", "wrong-block"],
    )
    def test_an_unreadable_or_undeliverable_shape_yields_nothing(
        self, block: dict[str, Any]
    ) -> None:
        assert read_cleared_env(block, ["local:api.env.K"], scope="local") == {}


class TestUndeliverable:
    """A cleared header passed the human's gate and still cannot arrive."""

    FIELDS = inventory(
        {"api": {"env": {"REGION": "us-east-1"}, "headers": {"Authorization": VALUE}}},
        scope="local",
    )

    def test_a_cleared_header_is_reported_and_a_cleared_env_var_is_not(self) -> None:
        record = _recorded(*self.FIELDS)
        assert [f.coordinate for f in undeliverable(self.FIELDS, record)] == [
            "local:api.headers.Authorization"
        ]

    def test_an_uncleared_header_is_not_reported_as_undelivered(self) -> None:
        """It is a refusal, not a limitation — reporting it here would tell the
        operator the wrap went ahead without it."""
        assert undeliverable(self.FIELDS, DecisionRecord.empty()) == []

    def test_a_stale_clearance_does_not_make_a_header_undelivered(self) -> None:
        """`undeliverable` reports the consequence of a live clearance, and a
        decision about a former value is not one."""
        record = _recorded(*self.FIELDS)
        changed = inventory(
            {"api": {"headers": {"Authorization": "a-changed-literal"}}}, scope="local"
        )
        assert undeliverable(changed, record) == []

    def test_deliverability_is_a_property_of_the_block(self) -> None:
        assert _field(block="env").is_deliverable
        assert not _field(block="headers", name="H").is_deliverable


class TestPathFactIsGuarded:
    """`names_existing_path` is a FACT tegh checked, offered to a human who is
    deciding — so it must never be the thing that aborts the wrap. An arbitrary
    config value is not necessarily a legal path."""

    def test_a_real_file_reports_true(self, tmp_path: Path) -> None:
        target = tmp_path / "memory.json"
        target.write_text("{}", encoding="utf-8")
        assert _names_existing_path(str(target)) is True

    def test_a_home_relative_path_is_expanded(self, tmp_path: Path, monkeypatch) -> None:
        """`~/notes.json` is how these values are actually written, and an
        unexpanded one would report False on a path that plainly exists."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
        assert _names_existing_path("~/notes.json") is True

    @pytest.mark.parametrize(
        "value",
        [
            VALUE,
            " ",
            "a-literal\x00value",
            "x" * 5000,
            "/" + "y" * 5000,
        ],
        ids=["plain-string", "whitespace", "embedded-nul", "over-long", "over-long-component"],
    )
    def test_a_value_that_is_not_a_legal_path_reports_false(self, value: str) -> None:
        """False, never an exception: an embedded NUL and an over-long component
        raise from `Path.exists()` on some platforms and versions, and either
        escaping would abort a wrap over a value the operator was about to
        classify by hand anyway."""
        assert _names_existing_path(value) is False

    def test_an_empty_value_does_not_report_the_working_directory(self) -> None:
        """`Path("")` normalizes to `.`, which always exists — so without an
        explicit guard an empty value renders "names an existing path on this
        machine", a fact about the working directory rather than about the
        value.

        It is the panel's credibility that is at stake, not safety: an empty
        value is not credential material and is refused until a human answers
        it either way. But a reviewer who catches one false line stops reading
        the others, and every line here exists to be read at the moment a
        decision is made.
        """
        assert _names_existing_path("") is False
        assert "names nothing that exists" in describe(_field(value=""))

    def test_a_field_carries_the_fact_it_was_built_with(self, tmp_path: Path) -> None:
        target = tmp_path / "memory.json"
        target.write_text("{}", encoding="utf-8")
        assert _field(value=str(target)).names_existing_path
        assert not _field().names_existing_path


class TestRendering:
    """What the operator reads. Labelled facts, and never the value."""

    def test_a_prompt_names_the_coordinate_and_labels_its_facts(self, tmp_path: Path) -> None:
        target = tmp_path / "memory.json"
        target.write_text("{}", encoding="utf-8")
        rendered = describe(_field(name="MEMORY_FILE_PATH", value=str(target)))

        assert "local:notes.env.MEMORY_FILE_PATH" in rendered
        assert "[FACT]" in rendered
        assert "names an existing path" in rendered
        assert str(target) not in rendered

    def test_a_non_existent_path_states_what_the_fact_does_NOT_prove(self) -> None:
        """False here is nearly uninformative — a file the server creates on
        first use looks identical to a credential — and a reviewer who read it
        as evidence of a secret would misclassify every fresh install."""
        rendered = describe(_field(name="MEMORY_FILE_PATH", value="/nowhere/at/all.json"))
        assert "names nothing that exists" in rendered
        assert "creates on first use" in rendered

    def test_a_header_prompt_says_the_clearance_does_not_deliver(self) -> None:
        rendered = describe(_field(block="headers", name="Authorization"))
        assert "does NOT deliver" in rendered

    def test_an_env_prompt_carries_no_undelivered_warning(self) -> None:
        assert "does NOT deliver" not in describe(_field())

    def test_a_changed_value_reads_differently_from_a_first_question(self) -> None:
        assert "CHANGED" in describe(_field(), changed=True)
        assert "CHANGED" not in describe(_field())

    def test_a_refusal_names_every_coordinate_and_the_two_ways_out(self) -> None:
        """A refusal an operator cannot act on gets worked around; relocating a
        header credential is not built, so the message has to carry what IS possible today."""
        rendered = render_refusal([_field(name="A"), _field(server_id="api", name="B")])
        assert "local:notes.env.A" in rendered and "local:api.env.B" in rendered
        assert "${VAR}" in rendered
        assert "connector_auth.header_map" in rendered and "not built" in rendered

    def test_nothing_unexpanded_reports_nothing(self) -> None:
        assert render_undelivered_references({}) is None

    def test_unexpanded_references_are_named_per_server(self) -> None:
        """These pass the gate and still do not arrive; silence would leave the
        operator debugging a server that misbehaves for no visible reason."""
        rendered = render_undelivered_references(
            {"api": ["env.API_KEY", "env.OTHER"], "notes": ["headers.X"]}
        )
        assert rendered is not None
        assert "api.env.API_KEY" in rendered and "notes.headers.X" in rendered
        assert "NOT receive them" in rendered
