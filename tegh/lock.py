"""`tegh.lock` — the committed admission record and its one serialization.

`docs/tegh-lock.md` (frozen 2026-07-25) is normative; conformance clauses are
TL1–TL11 and are cited by number throughout. This module owns the typed shape
and the deterministic writer. It does NOT admit, discover, sign, or verify
anything — those seams live outside the schema layer, exactly as
`schemas/mcp_registry.py` keeps the registry row separate from the ceremony
that writes it.

**What the lock is.** A *projection*: a portable, signed, human-reviewable
record of what a human admitted, exported into the project tree so it can be
committed, reviewed in a diff, and carried to another machine.

**What it emphatically is not: the runtime admission authority (TL1).** The
lock sits in the project directory — a directory the wrapped agent has ordinary
write access to, and which the harness's protected-path list cannot be extended
to cover. If the broker read admissions from this file, an injected agent would
admit its own tools with a single edit, collapsing both keys of two-key
admission at once. Local authority stays in tegh's own store directory (key #1)
and its ceremony-written registry rows (key #2); this file is written by the
ceremony and read by humans, `tegh verify`, and `tegh sync` — never by the
per-call path.

Nothing in this module can enforce TL1 — a schema cannot stop a caller from
reading it. It is stated here because the day someone wires `parse_lock_bytes`
into the connect path, this docstring is what should stop them.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from safe_agents.broker.schemas import McpToolDef, ToolOp, compute_tool_def_hash

# The lock format version. It is written into every lock and frozen on first
# publish: this artifact is committed to a user's repository and read by their
# teammates, so the shape is public API from the moment it ships. Widening
# later is the trap `docs/tegh-lock.md` exists to avoid — which is why fields
# v1 does not yet use (the origin attestation of TL7, the scope of TL9) are
# present now rather than added under a version bump.
LOCK_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# Closed catalogs
# ---------------------------------------------------------------------------


class AttestationKind(str, Enum):
    """How many distinct credentials stood behind an admission (TL6).

    A CLOSED catalog: an attestation kind is a claim about the integrity of the
    ceremony that produced an entry, so it may take only values this format
    defines. `SOLO_ATTESTED` is the N=1 local ceremony — one identity
    both proposing and ratifying; `MAKER_CHECKER` is the two-distinct-
    credentials ceremony (GAL §8: two credentials, one unmintable by the
    proposer — an org control the platform evidences, never enforces).

    A solo attestation is recorded honestly as solo. The lock never
    manufactures a second identity to look better than it is: an importing
    machine (TL7) weighs the origin attestation as evidence, and evidence that
    inflates itself is worse than no evidence.
    """

    SOLO_ATTESTED = "solo-attested"
    MAKER_CHECKER = "maker-checker"


class Harness(str, Enum):
    """Which coding harness a locked server was wrapped from (TL9/TL9a).

    THE closed catalog of this format — and the one the schema validates.
    `scope`, deliberately, is not: scope vocabularies are harness-specific
    (Claude Code's five load-side scopes plus managed are not Cursor's,
    OpenClaw's, or Hermes's), so enumerating scopes in the format would either
    privilege one harness or pile every harness's terms into a shared namespace
    where two different "project" scopes are indistinguishable. So the closure
    moves one layer out, to the adapter, which is the only layer that can judge
    whether a scope name is valid for a given harness (TL9a).

    **This catalog may GROW WITHOUT A FORMAT BUMP.** A lock naming a harness
    this tegh has no adapter for is a **capability** failure — "this lock needs
    an adapter you don't have" — not a format failure. The document is still
    perfectly well-formed, every clause still means what it meant, and a newer
    tegh reads it unchanged. That distinction is what keeps adding the fifth
    adapter from being a breaking change to an artifact already committed in
    users' repositories, and it is why an unknown harness must not be reported
    in the vocabulary of `_format_version_is_understood` (which fails on a
    document this version cannot safely READ).
    """

    CLAUDE_CODE = "claude-code"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


class LockAttestation(BaseModel):
    """Who admitted an entry, when, and under which identity kind (TL6).

    Attestation is DATA, not authority. On the machine that wrote it, it is a
    record of a ceremony that already happened; on any other machine it is
    *evidence about the lock's origin* and nothing more — importing a foreign
    lock is a ratification, not a copy (TL7). The importing machine runs its own
    admission and mints its own attestation, which is maker≠checker across
    machines and falls out of the format for free. That is why the origin
    attestation is carried even though v1 ships single-player.

    `admitted_at` is an ISO-8601 tz-aware UTC string, matching
    `RegisteredTool.admitted_at`: the model STORES a timestamp, it never mints
    one, so no wall-clock read enters the schema layer.
    """

    model_config = ConfigDict(extra="forbid")

    kind: AttestationKind
    admitted_by: str  # the admitting identity string (maker, in a two-key ceremony)
    admitted_at: str  # ISO-8601, tz-aware UTC — stored, never minted here
    ratified_by: Optional[str] = None  # the checker identity, maker-checker only

    @model_validator(mode="after")
    def _ratifier_matches_kind(self) -> "LockAttestation":
        """`ratified_by` is REQUIRED for maker-checker and REFUSED for solo.

        Both directions are refusals, and both matter. A `maker-checker` kind
        with no named checker is an unsupported claim of a two-credential
        ceremony — the exact overstatement TL6 forbids. A `solo-attested` kind
        that nonetheless names a checker is incoherent: either the kind or the
        field is wrong, and guessing which one silently would let a reviewer
        read a stronger provenance than the ceremony actually had.
        """
        if self.kind is AttestationKind.MAKER_CHECKER and self.ratified_by is None:
            raise ValueError(
                "attestation kind is 'maker-checker' but no 'ratified_by' identity "
                "is named; a two-credential ceremony must record the checker — an "
                "unnamed checker is a claim the lock cannot support"
            )
        if self.kind is AttestationKind.SOLO_ATTESTED and self.ratified_by is not None:
            raise ValueError(
                "attestation kind is 'solo-attested' but 'ratified_by' names "
                f"{self.ratified_by!r}; a solo ceremony has no checker — record it "
                "honestly as solo, or declare kind 'maker-checker'"
            )
        return self


class LockedTool(BaseModel):
    """One admitted tool, pinned verbatim (TL4/TL5/TL6).

    The entry carries the FULL ratified `McpToolDef` as admitted, not a hash
    alone. The reason is operational, not aesthetic: verify-against-lock must
    render the M5 diff, and a hash can only say *that* something changed, never
    *what*. The description delta is the model-facing injection vector and the
    thing a human must actually read (TL11 renders it verbatim and first); a
    hash-only lock would reduce the most important review in the product to the
    word "mismatch". Pinning the definition is also what lets a second machine
    — which may never have seen the live server — render a poisoned
    description in full.

    `tool_op` rides INSIDE the bytes the signature covers (TL5). An attacker
    who could downgrade a write tool to `effect: read` in an unsigned region
    would defeat the gate without touching a single definition hash, so the
    classification is not metadata about the entry — it is part of it.

    The `ToolOp` is what a human CONFIRMED. A server's advertised `annotations`
    (`readOnlyHint` and friends) may PROPOSE it, but annotations are untrusted
    input — a swapped server can claim `readOnlyHint: true` for a tool that
    deletes. Two things make the proposal safe to offer: a human ratifies it,
    and annotations ride the signed set, so a later hint flip breaks
    `def_hash` and brings the tool back for re-vet.
    """

    model_config = ConfigDict(extra="forbid")

    tool_def: McpToolDef
    def_hash: str
    tool_op: ToolOp
    attestation: LockAttestation

    @model_validator(mode="after")
    def _hash_matches_pinned_definition(self) -> "LockedTool":
        """The pinned hash must be the hash OF the pinned definition.

        `compute_tool_def_hash` is THE drift primitive — the lock re-uses it
        rather than defining a second one, so "the lock agrees with the
        registry" is structural rather than a pair of implementations that
        happen to match today.

        An entry whose pinned hash disagrees with its pinned definition is
        incoherent: verification could not say which half is authoritative, and
        either answer is wrong (trust the hash and the human-readable half is a
        lie; trust the definition and the drift binding is meaningless). Fail at
        parse, before any consumer has to choose.
        """
        expected = compute_tool_def_hash(self.tool_def)
        if self.def_hash != expected:
            raise ValueError(
                f"lock entry for {self.tool_def.server_id}/{self.tool_def.tool_name} "
                f"pins def_hash {self.def_hash!r} but its pinned definition hashes to "
                f"{expected!r}; the two halves of the entry disagree"
            )
        return self

    @model_validator(mode="after")
    def _classification_matches_coordinate(self) -> "LockedTool":
        """`tool_op` must classify THIS tool: `tool=server_id`, `op=tool_name`.

        The MCP coordinate convention (MCP-HOST.md) maps an MCP tool onto the
        broker's `(tool, op)` pair as `tool=server_id`, `op=tool_name`, which is
        what makes response-taint and the per-op budget counters work with no
        MCP-specific arm. A mismatched pair would carry one tool's definition
        under another tool's classification — a downgrade with no drift to show
        for it.
        """
        if self.tool_op.tool != self.tool_def.server_id:
            raise ValueError(
                f"lock entry pins tool_op.tool {self.tool_op.tool!r} for a definition "
                f"from server {self.tool_def.server_id!r}; the MCP coordinate "
                "convention is tool=server_id"
            )
        if self.tool_op.op != self.tool_def.tool_name:
            raise ValueError(
                f"lock entry pins tool_op.op {self.tool_op.op!r} for tool "
                f"{self.tool_def.tool_name!r}; the MCP coordinate convention is "
                "op=tool_name"
            )
        return self


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


class LockedServer(BaseModel):
    """One discovered server: where it came from, how to reach it, what was admitted.

    TL9 — the entry records the `server_id`, the `harness` that was wrapped, the
    config scope the winning definition was read from, the transport, and the
    NON-SECRET connection configuration (`command` + `args` + `cwd`, or `url`).
    The transport validators mirror `McpServerDecl` deliberately: the lock is a
    projection of an admission, so a server shape the manifest could never
    declare must not be representable here either.

    TL10 — `env_names` and `header_names` record the NAMES of the environment
    variables and headers a server requires, and no values. Those names are
    already public (they sit in `.mcp.json` today) and the lock needs them to
    know what must be injected. It deliberately carries no convention describing
    where a relocated secret now lives: publishing a secret-location format in a
    frozen public artifact would freeze it before the naming question is
    settled. That resolution lives store-side, where it can still change.

    TL8 — `admitted` MAY be empty, and an empty list is a first-class,
    meaningful state. Admission is lazy: `tegh wrap` discovers and snapshots,
    then offers a per-server batched review, and a human may admit none. "This
    server was discovered and nothing was admitted" and "this server was never
    seen" have OPPOSITE meanings on the next `tegh status`, so collapsing them
    would turn a deliberate refusal into an apparent gap.
    """

    model_config = ConfigDict(extra="forbid")

    server_id: str
    harness: Harness
    #: TL9/TL9a — the config scope the winning definition was read from, in the
    #: WRAPPED HARNESS's own vocabulary. See `_scope_is_not_blank` for why this
    #: is a string rather than an enum, and where the closure went.
    scope: str
    transport: Literal["stdio", "streamable-http"]

    # -- stdio connection config (non-secret) --
    command: Optional[str] = None
    args: list[str] = []
    cwd: Optional[str] = None

    # -- streamable-http connection config (non-secret) --
    url: Optional[str] = None

    # TL10: NAMES ONLY. Never values, and never a location convention.
    env_names: list[str] = []
    header_names: list[str] = []

    discovered_at: str  # ISO-8601, tz-aware UTC — stored, never minted here

    # TL8: an empty list is "discovered, nothing admitted" — not "not present".
    admitted: list[LockedTool] = []

    @field_validator("harness", mode="before")
    @classmethod
    def _harness_is_one_this_tegh_can_adapt(cls, value: Any) -> Any:
        """Refuse an unknown harness with a CAPABILITY message, not a type error.

        A bare enum rejection produces the generic pydantic "input should be
        'claude-code'", which tells a user nothing about what to do — and, worse,
        reads like the lock is malformed. It is not: per TL9a the catalog grows
        without a format bump, so an unrecognized harness means only that this
        installation lacks that adapter. The document is well-formed and a newer
        tegh reads it unchanged. Saying so is the whole point of the message.
        """
        if isinstance(value, Harness):
            return value
        known = ", ".join(sorted(member.value for member in Harness))
        if not isinstance(value, str) or value not in {m.value for m in Harness}:
            raise ValueError(
                f"lock names harness {value!r}, which this tegh has no adapter for "
                f"(adapters available here: {known}); the lock itself is well-formed "
                "— this is a capability gap, not a format error, so upgrade tegh (or "
                "install that harness's adapter) rather than editing the lock"
            )
        return value

    @field_validator("scope")
    @classmethod
    def _scope_is_not_blank(cls, value: str) -> str:
        """`scope` is a string here — but never an empty one.

        The closure did NOT disappear; it MOVED (TL9a). `harness` is the closed
        catalog the schema validates, and the scope vocabulary belongs to the
        ADAPTER — the only layer that can judge whether "project" or "workspace"
        or "managed" is a real scope for a *given* harness. A shared enum would
        have to hold every harness's terms at once, and two harnesses' "project"
        scopes would then be indistinguishable in a format frozen as public API.

        What survives here is the floor: a scope must actually name something.
        Scope is load-bearing twice over, which is why it is recorded rather
        than inferred:

        1. **Unwrap must restore to the file it took from.** A server lifted out
           of the user scope and put back into the project scope is a silent
           config edit the user never asked for.
        2. **Scope shadowing is a drift class no hash can see.** The same
           `server_id` appearing at a HIGHER-precedence scope shadows an
           admitted entry while every tool definition — and therefore every
           `def_hash` — stays byte-identical. Without the recorded scope, that
           substitution is invisible to verification.

        A blank scope silently forfeits both, so it is refused rather than
        stored as an empty claim about where the definition came from.

        Surrounding whitespace is refused for the same reason, one step
        removed: `" project"` is not a scope any adapter's closed set contains,
        so it would be stored by the format and then matched by nobody — an
        entry that reads correct to a human and resolves to nothing. It is
        REFUSED rather than stripped, per the validate-never-normalize rule:
        silently rewriting a caller's value hides the bug that
        produced it, and in a signed artifact a normalizing writer is also a
        writer whose output no longer matches what it was handed.
        """
        if not value.strip():
            raise ValueError(
                "locked server records an empty 'scope'; the scope a definition was "
                "read from is load-bearing (unwrap must restore to the file it took "
                "from, and a higher-precedence scope shadows without changing any "
                "hash), so it must name a real scope of the wrapped harness"
            )
        if value != value.strip():
            raise ValueError(
                f"locked server records scope {value!r} with surrounding whitespace; "
                "no adapter's scope vocabulary contains a padded name, so this would "
                "be stored by the format and recognized by nothing — pass the exact "
                "scope name (it is refused, not trimmed: validate, never normalize)"
            )
        return value

    @field_validator("admitted")
    @classmethod
    def _sorted_admitted(cls, value: list[LockedTool]) -> list[LockedTool]:
        """Normalize admitted order at construction (TL2a).

        Sorting here rather than in the writer means EVERY construction path —
        the ceremony, a parse, a test fixture — yields the same order, so the
        deterministic-bytes property cannot be lost by a caller that builds a
        lock some other way. Mirrors `McpServerSnapshot._sorted_entries`.
        """
        return sorted(value, key=lambda entry: entry.tool_def.tool_name)

    @field_validator("admitted")
    @classmethod
    def _admitted_tool_names_are_unique(cls, value: list[LockedTool]) -> list[LockedTool]:
        """One tool may be admitted at most once per server.

        Mirrors `McpServerDecl._tool_names_are_unique`. Sorting makes a duplicate
        harmless for determinism, which is exactly why it must be refused
        explicitly rather than left to the sort: two entries for the same
        coordinate can pin DIFFERENT definitions, hashes, or classifications,
        and verification would have no rule for which one the admission was
        against. A lock that can be read two ways is not a lock.
        """
        seen: set[str] = set()
        for entry in value:
            name = entry.tool_def.tool_name
            if name in seen:
                raise ValueError(
                    f"tool {name!r} is admitted more than once; each tool is "
                    "admitted at most once per server — two entries for one "
                    "coordinate can pin different definitions or classifications"
                )
            seen.add(name)
        return value

    @model_validator(mode="after")
    def _spawn_accessories_require_command(self) -> "LockedServer":
        """`args`/`cwd` are stdio-only accessories — refuse them without `command`.

        Mirrors `McpServerDecl._spawn_accessories_require_command`. A server
        recording `args` but no `command` is dead config at best and a masked
        authoring slip at worst — the flags a reviewer reads as "this is how the
        server is launched" would in fact launch nothing. Fail at parse, not at
        the first attempt to reach the server.
        """
        if self.command is None and (self.args or self.cwd is not None):
            raise ValueError(
                f"locked server {self.server_id!r} records stdio accessories "
                "(args/cwd) without 'command'; these describe how a spawned "
                "server is launched and are meaningless without it"
            )
        return self

    @model_validator(mode="after")
    def _transport_requires_matching_config(self) -> "LockedServer":
        """`command` and `url` are mutually exclusive; transport must match.

        The same rule `McpServerDecl` enforces: `transport="streamable-http"`
        requires `url` and forbids `command`; `transport="stdio"` forbids `url`.
        A locked server that is somehow both would leave verification with two
        ways to reach a server and no rule for which one an admission was
        against.
        """
        if self.command is not None and self.url is not None:
            raise ValueError(
                f"locked server {self.server_id!r} records both 'command' and 'url'; "
                "a server is exactly one of stdio ('command') or streamable-http "
                "('url'), never both"
            )
        if self.transport == "streamable-http":
            if self.url is None:
                raise ValueError(
                    f"locked server {self.server_id!r} has transport "
                    "'streamable-http' but records no 'url'; a remote server must "
                    "record its endpoint"
                )
            if self.command is not None:
                raise ValueError(
                    f"locked server {self.server_id!r} has transport "
                    "'streamable-http' but records 'command'; a remote server is not "
                    "spawned, so 'command' is a stdio-only field"
                )
        if self.transport == "stdio" and self.url is not None:
            raise ValueError(
                f"locked server {self.server_id!r} has transport 'stdio' but records "
                "'url'; a spawned server has no remote endpoint — use "
                "transport='streamable-http' for a remote server"
            )
        return self

    @model_validator(mode="after")
    def _admitted_tools_belong_to_this_server(self) -> "LockedServer":
        """Every admitted definition must carry THIS server's `server_id`.

        The nesting already implies it, so a disagreement means one of the two
        is wrong — and a tool filed under the wrong server would be verified
        against the wrong live endpoint, which is drift detection pointed at
        the wrong target.
        """
        strays = sorted(
            {
                entry.tool_def.server_id
                for entry in self.admitted
                if entry.tool_def.server_id != self.server_id
            }
        )
        if strays:
            raise ValueError(
                f"locked server {self.server_id!r} lists admitted tools whose "
                f"definitions name a different server: {strays}; an entry is filed "
                "under the server it was discovered from"
            )
        return self


# ---------------------------------------------------------------------------
# The lock document
# ---------------------------------------------------------------------------


class TeghLock(BaseModel):
    """The whole committed record — the shape of `tegh.lock` on disk.

    There is deliberately NO signature field. The signature lives in a sidecar
    (`tegh.lock.sig`, `LockSignature` below) whose basis is the exact bytes of
    this document on disk (TL2). A signature INSIDE the lock would be circular,
    and the usual escape — embedding the canonical payload as a JSON string
    inside a wrapper — would make the lock unreadable in a code-review diff,
    defeating TL11 for the sake of TL2. The sidecar satisfies both: the signed
    basis is literally the file, and the file stays a pretty-printed, diffable
    JSON document. Both files are committed together.

    `generated_at` is an ISO-8601 tz-aware UTC string — stored, never minted
    here.
    """

    model_config = ConfigDict(extra="forbid")

    format_version: int
    generated_at: str  # ISO-8601, tz-aware UTC
    # Required, with no default: a lock states its server set explicitly, even
    # when that set is empty. `servers: []` is a written claim ("nothing was
    # discovered"); an omitted key would be an authoring slip reading as the
    # same thing, which is the TL8 collapse one level up.
    servers: list[LockedServer]

    @field_validator("format_version")
    @classmethod
    def _format_version_is_understood(cls, value: int) -> int:
        """Refuse a lock this version does not fully understand.

        `extra="forbid"` refuses a document with unknown FIELDS; without this,
        a document announcing an unknown FORMAT would still parse as long as it
        happened to use no new keys — silently half-understood, which is the one
        outcome the forbid posture exists to prevent. A newer lock may have
        semantics this version cannot see (a field whose absence means something
        different, a clause that changed what an existing field binds), so
        acting on it is a guess.

        Failing closed here is cheap and legible: the fix is to upgrade tegh,
        and the alternative is a tool quietly enforcing an older reading of a
        newer artifact.
        """
        if value != LOCK_FORMAT_VERSION:
            raise ValueError(
                f"lock declares format_version {value}, but this tegh understands "
                f"version {LOCK_FORMAT_VERSION}"
                + (
                    " — the lock was written by a newer tegh; upgrade rather than "
                    "acting on a partial reading of it"
                    if value > LOCK_FORMAT_VERSION
                    else " — unrecognized lock format"
                )
            )
        return value

    @field_validator("servers")
    @classmethod
    def _sorted_servers(cls, value: list[LockedServer]) -> list[LockedServer]:
        """Normalize server order at construction (TL2a) — see `_sorted_admitted`."""
        return sorted(value, key=lambda server: server.server_id)


class LockSignature(BaseModel):
    """The `tegh.lock.sig` sidecar — a signature OVER the lock file's bytes (TL2).

    The basis is the exact byte string `canonical_lock_bytes` produced and the
    writer put on disk, verified VERBATIM and BEFORE parsing. No consumer may
    re-serialize a parsed `TeghLock` and sign or verify that: an integrity basis
    must indict tampering, never schema evolution. A parse-then-
    re-serialize basis silently re-derives itself from whatever the model class
    looks like today, so any additive field turns every historical lock into a
    reported tamper — and a control that cries tamper on legitimate change
    teaches reviewers to dismiss the alarm that matters.

    Signing uses the local issuer key, which is what makes a lock PORTABLE: a
    second machine verifies with a public key, never a shared HMAC secret.
    `key_id` names which public key that is; `value` is the base64 signature.

    A lock may exist on disk with no sidecar — a lock written before local
    issuer keys are configured must not be a parse failure — but the ceremony
    that WRITES a lock refuses to write one unsigned unless explicitly forced
    (TL3, the `ratify --allow-unsigned` posture). Optional at schema, required
    at ceremony.

    This module models the typed shape and its serialization only. Producing
    and checking the signature bytes is a later slice; no crypto lives here.
    """

    model_config = ConfigDict(extra="forbid")

    key_id: str
    algorithm: Literal["ed25519"]
    value: str  # base64-encoded signature over the lock file's exact bytes


# ---------------------------------------------------------------------------
# The ONE writer — deterministic, pretty-printed on purpose (TL2/TL2a/TL11)
# ---------------------------------------------------------------------------


def _canonical_document_bytes(payload: dict) -> bytes:
    """Serialize a tegh committed document to its deterministic on-disk bytes.

    Shared by the lock and its sidecar so the two files cannot drift into
    different serialization rules. Deliberately UNLIKE the base's compact
    `separators=(",", ":")` canonical form: those payloads are hashed and never
    read by a human, whereas these are committed, diffed, and reviewed. So:

    - `sort_keys=True` + fixed `indent=2` + trailing newline — identical content
      MUST produce identical bytes. A re-export that changed nothing must
      produce an empty `git diff`, or reviewers learn to skim the one artifact
      whose entire value is that it gets read.
    - `ensure_ascii=True` — a non-ASCII byte in a description (the injection
      surface) renders as an escape a reviewer can see, and the file stays
      byte-stable across every editor and terminal encoding it passes through.
    """
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("utf-8")


def canonical_lock_bytes(lock: TeghLock) -> bytes:
    """The ONE serialization of a lock — written to disk AND signed (TL2).

    Serialize exactly once: this byte string is what the writer puts in
    `tegh.lock`, what the sidecar signature is computed over, and what a
    verifier digests verbatim before parsing.

    Determinism is structural rather than conventional: servers are sorted by
    `server_id` and admitted tools by `tool_name` at CONSTRUCTION (see the
    field validators), so a caller cannot lose the property by building the
    model some other way; this function only fixes key order, indentation, and
    encoding.
    """
    return _canonical_document_bytes(lock.model_dump(mode="json"))


def canonical_signature_bytes(signature: LockSignature) -> bytes:
    """The ONE serialization of the `tegh.lock.sig` sidecar.

    Same deterministic, pretty-printed form as the lock itself: the sidecar is
    committed alongside it and shows up in the same review diff. Note the
    asymmetry — the sidecar's bytes are not themselves an integrity basis (they
    CARRY the signature over the lock's bytes), so nothing verifies against
    this serialization.
    """
    return _canonical_document_bytes(signature.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Parsing — verify FIRST, then parse (the caller's ordering, not ours)
# ---------------------------------------------------------------------------


def parse_lock_bytes(raw: bytes) -> TeghLock:
    """Parse lock bytes into the typed model. Validation only — no verification.

    **Verify-then-parse ordering is the CALLER's responsibility** (TL2): a
    consumer that has a sidecar must check the signature over these exact bytes
    BEFORE calling this. The ordering is not enforceable here — this function
    cannot know whether a sidecar exists — so it is stated rather than
    implemented, and the signature-checking seam is where it must be honored.

    Unknown keys are REFUSED (`extra="forbid"` throughout), so a
    legacy-shaped or hand-widened document fails loudly instead of being
    silently half-read. That is the tamper-not-evolution posture applied at the format edge:
    a lock this version does not fully understand is not a lock it may act on.
    """
    return TeghLock.model_validate_json(raw)


def parse_signature_bytes(raw: bytes) -> LockSignature:
    """Parse sidecar bytes into the typed model. No crypto — shape only."""
    return LockSignature.model_validate_json(raw)
