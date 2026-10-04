"""Which literal values in a harness config are credentials — a human's call.

`interpose.py` refuses to displace a server whose config holds a literal
(non-`${VAR}`) value in `env` or `headers`, because moving that server's
definition into tegh's backup would copy the credential rather than relocate it.
The reasoning is sound and the gate is right. The problem is that **tegh cannot
tell a credential from a path**, and on a real machine most literal values are
paths: across six wrapped-candidate projects on the machine this was found on,
every literal value was a `MEMORY_FILE_PATH`. Under a gate that assumes the
worst, none of those projects can be wrapped at all.

## The decision is a human's, and it is asked the way classifications are

Not a name heuristic (`*_KEY`, `*_TOKEN`, `*_SECRET`). That is the obvious cheap
fix and it fails in the one direction that must never fail: a real credential in
an innocuously-named variable would be silently copied. A heuristic that is
usually right is worse than no heuristic here, because its failures are exactly
the cases that matter and it produces them silently.

Not by printing the values, either. The values are the thing being protected,
and a terminal and its scrollback are not where they belong. What renders is the
field NAME plus a small number of labelled FACTS — the same discipline
`classify.py` applies to ToolOp proposals, where a transport fact and a server's
untrusted claim are deliberately never shown in one voice.

## The classification is what AUTHORIZES a read

`discovery.py` never copies a value into memory at all (TL10): a
:class:`~tegh.discovery.DiscoveredServer` carries `env_names`, never
`env`. This module keeps that property by inverting it rather than weakening it
— a value is read only for a field a human has already classified as
configuration. Nothing in tegh reads a value tegh has not been told is safe to
read, and a field nobody classified is refused, not carried.

The two outcomes map onto a seam the base already has, and the mapping is the
whole point:

- **CONFIG** → :attr:`McpServerDecl.env`, documented as "the STATIC half of the
  child environment ... credential material never appears here".
- **CREDENTIAL** → :attr:`ConnectorAuth.env_map`, the spawn-time resolved half.
  This **relocates**: the value moves into tegh's per-project secret
  store and OUT of the harness config, and the manifest gains an `env_map` that
  delivers it at child spawn. A credential in a non-deliverable block
  (`headers`) still refuses — that is `header_map`, the remote leg, and it is a
  deliberate follow-on rather than something to half-build here.

## What is persisted, and what deliberately is not

Only CONFIG decisions are written, and each is bound to a SHA-256 of the value
it was made about. The binding is not ceremony for its own sake: a decision
keyed on a field NAME alone is a name heuristic that the operator trained by
hand, and it would silently clear a field whose value later became a credential.
A changed value is a new question.

No CREDENTIAL decision is recorded — not even its hash — and relocation did not change
that. It might look as though relocation now needs one, so that a second wrap
does not re-ask; it does not, and the reason is worth stating because the
opposite is a plausible-sounding mistake. A relocated credential is **gone from
the harness config**, so the next wrap's inventory does not find a literal at
that coordinate and there is nothing to ask about. Persisting the decision would
buy nothing and would leave a SHA-256 of a possibly low-entropy secret on disk.
Hashes of CONFIG values are unsalted because those values are not secret and are
about to be written into the manifest in plaintext regardless.

The record is **not signed**. It lives in the tegh home beside key #1, so an
actor who could forge it could already declare a tool namespace; a signature
would add a ritual, not a boundary. `tegh posture` is where that limit is stated.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional, Sequence

#: A value that is wholly a `${VAR}` / `${VAR:-default}` reference carries no
#: secret — the harness expands it from the environment at load
#: (`docs/references/harnesses/claude-code.md` §1 "Env var expansion"). Anything
#: else in an `env` or `headers` value is treated as literal material.
REFERENCE_ONLY = re.compile(r"^\s*\$\{[^}]+\}\s*$")

#: Config blocks whose values are credential-bearing when literal. CLOSED: a
#: block added to a harness config is a block tegh has not been told how to
#: judge, and judging it by analogy is how a new credential surface gets missed.
SECRET_BEARING_BLOCKS: tuple[str, ...] = ("env", "headers")

#: Only `env` reaches the spawned child. `headers` has no static counterpart on
#: `McpServerDecl` — a remote decl carries `url` and its credentials ride
#: `connector_auth.header_map` — so a header classified as configuration clears
#: the gate and is still REPORTED as undelivered rather than silently dropped.
DELIVERABLE_BLOCKS: tuple[str, ...] = ("env",)

_RECORD_VERSION = 1


class ValueClass(str, Enum):
    """What one literal config value is. CLOSED, and there is no third answer.

    "Probably a path" is not an option on purpose: an uncertain answer would
    have to resolve to one of these two anyway, and letting the operator defer
    would just relocate the decision to whoever reads the wrap output later.
    """

    #: Relocated out of the harness config; refuses the wrap in a block that
    #: cannot be delivered (`headers`).
    CREDENTIAL = "credential"
    #: Carried through to the server tegh spawns.
    CONFIG = "config"


@dataclass(frozen=True)
class LiteralField:
    """One literal value in a harness config, described WITHOUT quoting it.

    Every attribute here is derived from the value; none of them is the value.
    That is deliberate — this object is what rendering, prompting and the
    persisted record all consume, so keeping the value out of it means no later
    caller can print it by accident.
    """

    scope: str
    server_id: str
    block: Literal["env", "headers"]
    name: str
    value_sha256: str

    #: Facts for the reviewer, each rendered as a FACT and none of them
    #: deciding anything.
    length: int
    names_existing_path: bool

    @property
    def coordinate(self) -> str:
        """Scope-qualified, and the qualification is load-bearing.

        Claude Code's local and user scopes are two different blocks in the SAME
        file (`~/.claude.json`), and scopes shadow rather than merge — so one
        server name can exist twice with two different values, either of which
        may be a credential. An unqualified coordinate would let a decision made
        about one silently clear the other.
        """
        return f"{self.scope}:{self.server_id}.{self.block}.{self.name}"

    @property
    def is_deliverable(self) -> bool:
        """Can a value cleared as CONFIG actually reach the spawned server?"""
        return self.block in DELIVERABLE_BLOCKS


@dataclass(frozen=True)
class Decision:
    """One recorded CONFIG classification, bound to the value it was made about."""

    coordinate: str
    value_sha256: str
    decided_at: str
    decided_by: str


@dataclass
class DecisionRecord:
    """Every CONFIG classification this project's operator has made.

    Lookup is by coordinate AND hash together (:meth:`clears`); there is no
    accessor that matches on the coordinate alone, so a caller cannot
    accidentally honour a decision made about a different value.
    """

    decisions: dict[str, Decision]

    @classmethod
    def empty(cls) -> "DecisionRecord":
        return cls(decisions={})

    def clears(self, field: LiteralField) -> bool:
        """Has this exact value at this exact coordinate been cleared?"""
        decision = self.decisions.get(field.coordinate)
        return decision is not None and decision.value_sha256 == field.value_sha256

    def was_decided_about_another_value(self, field: LiteralField) -> bool:
        """Is there a decision here, but about a value this field no longer holds?

        Rendered to the operator as "this changed since you classified it",
        which is a materially different prompt from a first-time question and
        deserves to read like one.
        """
        decision = self.decisions.get(field.coordinate)
        return decision is not None and decision.value_sha256 != field.value_sha256

    def record(self, field: LiteralField, *, decided_at: str, decided_by: str) -> None:
        self.decisions[field.coordinate] = Decision(
            coordinate=field.coordinate,
            value_sha256=field.value_sha256,
            decided_at=decided_at,
            decided_by=decided_by,
        )

    def forget(self, coordinate: str) -> None:
        self.decisions.pop(coordinate, None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": _RECORD_VERSION,
                "decisions": [
                    {
                        "coordinate": d.coordinate,
                        "classification": ValueClass.CONFIG.value,
                        "value_sha256": d.value_sha256,
                        "decided_at": d.decided_at,
                        "decided_by": d.decided_by,
                    }
                    for d in sorted(self.decisions.values(), key=lambda d: d.coordinate)
                ],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "DecisionRecord":
        """Parse a record, refusing one this version does not understand.

        An unreadable or future-versioned record resolves to EMPTY rather than
        to an error, and empty means every field is asked again. Failing toward
        re-asking is the only safe direction: the alternative reading of a file
        tegh cannot parse is that some field was cleared, and acting on that
        guess is how a credential gets copied.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls.empty()
        if not isinstance(data, Mapping) or data.get("version") != _RECORD_VERSION:
            return cls.empty()
        entries = data.get("decisions")
        if not isinstance(entries, list):
            return cls.empty()

        decisions: dict[str, Decision] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            # A record is CONFIG decisions only. Anything else in the file is a
            # shape this version did not write; ignoring it re-asks the field.
            if entry.get("classification") != ValueClass.CONFIG.value:
                continue
            coordinate = entry.get("coordinate")
            digest = entry.get("value_sha256")
            if not isinstance(coordinate, str) or not isinstance(digest, str):
                continue
            decisions[coordinate] = Decision(
                coordinate=coordinate,
                value_sha256=digest,
                decided_at=str(entry.get("decided_at", "")),
                decided_by=str(entry.get("decided_by", "")),
            )
        return cls(decisions=decisions)


def load_record(path: Path) -> DecisionRecord:
    """Read a project's decisions, treating an absent file as no decisions."""
    if not path.exists():
        return DecisionRecord.empty()
    return DecisionRecord.from_json(path.read_text(encoding="utf-8"))


def save_record(path: Path, record: DecisionRecord) -> None:
    """Write the record atomically, the way `interpose` writes a config.

    A torn write already resolves to "no decisions" on the next read, which is
    the safe direction — but "safe because the failure mode happens to fail
    right" is a worse property than "cannot half-write", and the rename costs
    two lines. `ensure_ascii` is left at its default here, unlike
    `interpose.py`, because that file must reproduce someone ELSE's bytes and
    this one only has to round-trip its own.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tegh-tmp")
    tmp.write_text(record.to_json(), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Inventory — names and derived facts, never values
# ---------------------------------------------------------------------------


def value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _names_existing_path(value: str) -> bool:
    """Does this value name something that exists on this machine?

    A FACT, and a weak one in only one direction: a path the server creates on
    first use does not exist yet and looks exactly like a credential here. So
    True is informative and False proves nothing, which is what the rendering
    says out loud. Guarded because an arbitrary config value is not necessarily
    a legal path — a NUL byte or an over-long component raises rather than
    returning False.

    The empty string is excluded explicitly, because `Path("")` is `Path(".")`
    and the current directory always exists: without this an empty config value
    would render "names an existing path on this machine", which is a FALSE
    fact shown to a reviewer at the moment they are deciding. A fact panel is
    only worth having if every line in it is true.
    """
    if not value.strip():
        return False
    try:
        return Path(value).expanduser().exists()
    except (OSError, ValueError):
        return False


def literal_fields(
    server_id: str, entry: Mapping[str, Any], *, scope: str
) -> list[LiteralField]:
    """Describe every literal `env`/`headers` value on one server entry."""
    found: list[LiteralField] = []
    for block in SECRET_BEARING_BLOCKS:
        values = entry.get(block)
        if not isinstance(values, Mapping):
            continue
        for name, value in values.items():
            if not isinstance(value, str) or REFERENCE_ONLY.match(value):
                continue
            found.append(
                LiteralField(
                    scope=scope,
                    server_id=server_id,
                    block=block,  # type: ignore[arg-type]
                    name=str(name),
                    value_sha256=value_digest(value),
                    length=len(value),
                    names_existing_path=_names_existing_path(value),
                )
            )
    return sorted(found, key=lambda f: (f.server_id, f.block, f.name))


def inventory(block: Mapping[str, Any], *, scope: str) -> list[LiteralField]:
    """Describe every literal value in one site's whole `mcpServers` block.

    Taken from the BLOCK rather than from discovery's effective server list,
    because the block is what a wrap displaces. A server discovery shadowed, or
    skipped for an unrepresentable transport, still sits in this config and
    still has its definition moved into tegh's backup — so its values are still
    tegh's problem, and enumerating from the shorter list would miss them.
    """
    found: list[LiteralField] = []
    for server_id, entry in block.items():
        if isinstance(entry, Mapping):
            found.extend(literal_fields(str(server_id), entry, scope=scope))
    return sorted(found, key=lambda f: (f.server_id, f.block, f.name))


# ---------------------------------------------------------------------------
# The authorized read
# ---------------------------------------------------------------------------


def read_cleared_env(
    block: Mapping[str, Any], cleared: Iterable[str], *, scope: str
) -> dict[str, dict[str, str]]:
    """Read the values of exactly the cleared, deliverable fields. Nothing else.

    Returns `{server_id: {NAME: value}}` for `McpServerDecl.env`. This is the
    ONLY function in tegh that lifts a harness-config value into a returned
    object, and it does so for a coordinate a human has classified as
    configuration — which is what the classification is for.

    A cleared field in a non-deliverable block (`headers`) is deliberately
    absent from the result: it passed the gate, but there is nowhere for it to
    go, and the caller reports that rather than dropping it quietly.
    """
    wanted = set(cleared)
    carried: dict[str, dict[str, str]] = {}
    for server_id, entry in block.items():
        if not isinstance(entry, Mapping):
            continue
        for deliverable in DELIVERABLE_BLOCKS:
            values = entry.get(deliverable)
            if not isinstance(values, Mapping):
                continue
            for name, value in values.items():
                coordinate = f"{scope}:{server_id}.{deliverable}.{name}"
                if coordinate in wanted and isinstance(value, str):
                    carried.setdefault(str(server_id), {})[str(name)] = value
    return carried


class CredentialMoved(RuntimeError):
    """A value classified as a credential is not the value now at that coordinate."""


def read_cleared_credentials(
    block: Mapping[str, Any], classified: Mapping[str, str], *, scope: str
) -> dict[str, dict[str, str]]:
    """Read the values of exactly the fields classified as CREDENTIALS.

    The second consumer of the same authorization :func:`read_cleared_env`
    exercises, and deliberately routed through this module rather than given its
    own reader: a value is lifted only for a coordinate a human has ruled on.

    **The digest is verified HERE, at the point of the read**, not upstream —
    a caution earned the hard way. `read_cleared_env` can afford to match
    on the coordinate alone because the interposition gate re-reads and
    re-compares before anything is written; this function has no such backstop,
    because what it returns is copied straight into tegh's secret store. A value
    that moved between classification and read is therefore refused outright
    rather than relocated, and refusing is safe: the wrap has written nothing at
    the point this runs.

    Returns `{server_id: {NAME: value}}`, restricted to DELIVERABLE blocks — a
    header cannot ride `env_map`, so a credential in one is not relocatable by
    this path (that is `header_map`, the remote leg, deliberately a follow-on).
    """
    wanted = dict(classified)
    carried: dict[str, dict[str, str]] = {}
    for server_id, entry in block.items():
        if not isinstance(entry, Mapping):
            continue
        for deliverable in DELIVERABLE_BLOCKS:
            values = entry.get(deliverable)
            if not isinstance(values, Mapping):
                continue
            for name, value in values.items():
                coordinate = f"{scope}:{server_id}.{deliverable}.{name}"
                expected = wanted.get(coordinate)
                if expected is None or not isinstance(value, str):
                    continue
                if value_digest(value) != expected:
                    raise CredentialMoved(
                        f"{coordinate} no longer holds the value it was classified "
                        "about — refusing to relocate it. Re-run `tegh wrap` and "
                        "classify it again."
                    )
                carried.setdefault(str(server_id), {})[str(name)] = value
    return carried


def uncleared(
    fields: Sequence[LiteralField], record: DecisionRecord
) -> list[LiteralField]:
    """The fields still needing a human answer, in render order."""
    return [field for field in fields if not record.clears(field)]


def cleared_coordinates(
    fields: Sequence[LiteralField], record: DecisionRecord
) -> list[str]:
    """Coordinates whose exact current value the operator classified as config."""
    return [field.coordinate for field in fields if record.clears(field)]


def cleared_digests(
    fields: Sequence[LiteralField], record: DecisionRecord
) -> dict[str, str]:
    """Cleared coordinates mapped to the digest of the value that was cleared.

    This, not :func:`cleared_coordinates`, is what the interposition gate takes.
    The gate reads the config again at the END of a wrap, and a bare list of
    names would let it approve whatever happens to be at that coordinate by
    then — the name heuristic this module exists to refuse, handed a window as
    long as the whole ceremony.
    """
    return {
        field.coordinate: field.value_sha256 for field in fields if record.clears(field)
    }


def undeliverable(
    fields: Sequence[LiteralField], record: DecisionRecord
) -> list[LiteralField]:
    """Cleared fields whose values cannot reach the server tegh spawns."""
    return [
        field
        for field in fields
        if record.clears(field) and not field.is_deliverable
    ]


def describe(field: LiteralField, *, changed: bool = False) -> str:
    """One field, as the operator reads it. NEVER the value.

    Facts are labelled the way `review.render_proposal` labels a proposal's
    provenance, and for the same reason: a reviewer must be able to see that
    "names an existing file" is something tegh checked, not something tegh
    concluded.
    """
    def fact(text: str) -> str:
        return f"     {text:<48} [FACT]"

    lines = [f"  {field.coordinate}", fact(f"{field.length} character(s)")]
    if field.names_existing_path:
        lines.append(fact("names an existing path on this machine"))
    else:
        lines.append(fact("names nothing that exists on this machine"))
        lines.append(
            "     (a path the server creates on first use looks like this too,\n"
            "      so this line tells you much less than the other would)"
        )
    if not field.is_deliverable:
        lines.append(
            "     NB a header has no static counterpart on the broker's server\n"
            "        declaration, so calling this configuration clears the wrap\n"
            "        but does NOT deliver it."
        )
    if changed:
        lines.append(
            "     !! this value has CHANGED since you last classified it"
        )
    return "\n".join(lines)


def render_refusal(credentials: Sequence[LiteralField]) -> str:
    """Why the wrap stopped over credentials it cannot relocate.

    Now that the stdio path relocates, this is reached only by a credential in a NON-deliverable block —
    a `headers` value on a remote server. The stdio path relocates instead, so
    the message names the one gap rather than the old blanket refusal.
    """
    named = ", ".join(field.coordinate for field in credentials)
    return (
        f"you classified {len(credentials)} value(s) as CREDENTIALS in a block "
        f"tegh\ncannot yet relocate: {named}.\n\n"
        "A header credential belongs in `connector_auth.header_map` (the remote "
        "leg of\nrelocation). Relocating it is not built (#4), and wrapping anyway would "
        "move the value\ninto tegh's backup — a second plaintext copy rather "
        "than a boundary, which is\nwhat the wrap contract forbids.\n\n"
        "Two things work today:\n"
        "  - replace the literal with a ${VAR} reference in your harness config "
        "and\n    export it in your shell (a real improvement to that config "
        "either way), or\n"
        "  - remove the server from this harness until the remote leg lands."
    )


def render_undelivered_references(
    unexpanded: Mapping[str, Sequence[str]]
) -> Optional[str]:
    """Report `${VAR}` fields, which pass the gate and still do not arrive.

    The broker performs no `${VAR}` expansion, and tegh will not expand them
    either — that would pull a live secret out of this process's environment
    into memory, which is exactly what discovery refuses to do (TL10). So a
    referenced variable is not delivered, and saying so is the difference
    between a known limitation and a server that quietly misbehaves.
    """
    if not unexpanded:
        return None
    lines = [
        "  !! these fields reference environment variables, and the wrapped "
        "server will",
        "     NOT receive them: tegh does not expand a ${VAR} (expanding it "
        "would pull a",
        "     live secret into tegh's memory, which discovery refuses to do).",
    ]
    for server_id in sorted(unexpanded):
        for field_path in sorted(unexpanded[server_id]):
            lines.append(f"       {server_id}.{field_path}")
    return "\n".join(lines)
