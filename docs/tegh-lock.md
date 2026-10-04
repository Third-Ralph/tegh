# `tegh.lock` — the committed admission record (cut B)

**Status:** contract, frozen 2026-07-25. This is the one tegh artifact that becomes public API:
it is committed to the user's repository, read by teammates, and diffed in code review. A format
freezes on first publish, so this document defines the shape *including the fields v1 does not
yet use* — widening later is the trap this doc exists to avoid.

Conformance clauses are **TL1–TL11**. The drift primitive, the tool-definition model, and the
admission ceremony are NOT re-invented here: `broker/MCP-HOST.md` remains normative for
admission, and `compute_tool_def_hash` remains the single drift primitive.

## What it is, and what it is emphatically not

`tegh.lock` is a **projection**: a portable, signed, human-reviewable record of what a human
admitted, exported into the project tree so it can be committed, reviewed, and carried to another
machine.

It is **not** the runtime admission authority.

That distinction is the whole security argument, and getting it backwards opens the hole the
admission ceremony exists to close. Two-key admission (MCP-HOST.md) works because its two keys
have *different writers*: the image-baked manifest declares the `(server_id, tool_name)` namespace
and its ToolOp classification, and a separately-identified ceremony writes the registry row that
activates it. Neither key is writable by the agent.

`tegh.lock` sits in the project directory — the directory the wrapped agent has ordinary write
access to. Claude Code's protected-path list, which forces a human prompt on writes to
`.mcp.json` and `.claude.json`, is **hard-coded vendor-side; tegh cannot add a file to it**
[verified 2026-07-25: `docs/references/harnesses/claude-code.md` §5, sourced to the vendor's
permission-modes page]. An agent editing `tegh.lock` therefore raises no prompt at all. If the
broker read admissions from that file, an honest-but-injected agent would admit its own tools
with a single `Edit` call, collapsing both keys at once.

So, locally:

| Role | Cloud | tegh (local) |
|---|---|---|
| Key #1 — namespace + ToolOp declaration | image-baked `AgentManifest` | tegh's own store directory, **outside the project tree** |
| Key #2 — activation row | DynamoDB registry row, ceremony-written | sqlite registry row, ceremony-written |
| Committed record | (none) | **`tegh.lock`** — this document |

Posture-1 honesty applies and is stated in `tegh posture` (shipped 2026-07-26): a same-user attacker
(or a same-user agent process) can read and write tegh's store directory too. Moving authority out
of the project tree defeats the *ordinary* injected-agent edit, not a determined same-user
adversary. Posture 2 (`tegh contain`) is what makes that a real boundary. Claiming otherwise is the
overclaim the posture ladder exists to prevent — the ladder itself is `docs/posture-ladder.md`.

**TL1.** A broker MUST NOT read `tegh.lock` when deciding, admitting, or connecting. The lock is
written by the ceremony and read by humans and by `tegh verify`/`tegh sync` — never by the
per-call path.

## Integrity

**TL2 — stored-bytes basis, as a signature sidecar.** The lock is serialized exactly once.
That byte string is what is written to disk, what the signature is computed over, and what is
verified — verbatim, **before** parsing. No consumer may re-serialize the parsed model and sign or
verify that: an integrity basis must indict tampering, never schema evolution.

The signature therefore lives in a **sidecar**, `tegh.lock.sig`, whose basis is the exact bytes of
`tegh.lock` on disk. A signature field *inside* the lock would be circular, and the usual escape —
embedding the canonical payload as a JSON string inside a wrapper object — would make the lock
unreadable in a code-review diff, defeating TL11 for the sake of TL2. The sidecar satisfies both:
the signed basis is literally the file, and the file stays a pretty-printed, diffable JSON
document. Both files are committed together.

**TL2a — the writer is deterministic.** Identical content MUST serialize to identical bytes:
servers sorted by `server_id`, admitted tools sorted by `tool_name`, object keys sorted, fixed
indentation, trailing newline. A re-export that changed nothing must produce an empty `git diff`,
or reviewers learn to skim the one artifact whose whole value is that it gets read.

**TL3 — signature optional at rest, required at ceremony.** The sidecar FILE may be absent: a
lock written before local issuer keys are configured must parse, not explode. But the ceremony
that WRITES a lock refuses to write one unsigned unless explicitly forced. This follows an earlier
precedent [ruling: maintainer, 2026-07-25], and is the same posture as
`ratify --allow-unsigned`.

Note this clause is about the *absence of a file*, never an optional signature field inside the
lock — TL2 forbids that shape, and an earlier draft of this clause wrongly licensed it.

Signing uses the local issuer key (`ISSUER_SIGNING_KEY_FILE`, 0600-or-refuse). A signature is
what makes the lock portable: a second machine verifies with a public key, never a shared HMAC
secret.

*Reader behavior, not a format clause (so deliberately unnumbered):* a reader distinguishes three
outcomes, never two — verified, **unknown signer**, and failed. A lock signed by a key the reading
machine has no public half for is reported as unverifiable, never as a tamper: tegh is
single-player today with no key distribution, so a teammate's lock is the *expected* case, and
reporting it as tampering is the false alarm that trains people to dismiss the real one. tegh's CLI
gives the two distinct exit codes (4 and 3) for exactly this reason.

## Entries

**TL4 — pin the definition verbatim.** Each entry carries the full ratified `McpToolDef` as
admitted, plus its `def_hash` from `compute_tool_def_hash`. Not a hash alone.

The reason is operational, not aesthetic: **verify-against-lock must render the M5 diff, and a
hash can only say *that* something changed, never *what*.** The description delta is the
model-facing injection vector and the thing a human must actually read; a hash-only lock reduces
the most important review in the product to "mismatch". The pinned definition is what lets a
second machine — which may never have seen the live server — render a poisoned description
verbatim.

**TL5 — the classification rides inside the signed bytes.** Each entry carries the confirmed
`ToolOp` (`effect` / `external` / `reversible` / `egress_arg`, with `tool=server_id`,
`op=tool_name`). It must be covered by the lock's integrity binding: an attacker who could
downgrade a write tool to `effect: read` in an unsigned region would defeat the gate without
touching a single definition hash.

**TL6 — attestation is data.** Each entry carries who admitted it, when, and under which
identity kind — `solo-attested` (the N=1 local ceremony) or `maker-checker` (two distinct
credentials). A solo attestation is recorded honestly as solo; the lock never manufactures a
second identity to look better than it is.

**TL7 — importing a foreign lock is a ratification, not a copy.** When `tegh sync` ingests a lock
written on another machine, the attestation in that file is *evidence about its origin*, never
local authority. The importing machine runs its own admission and mints its own attestation. This
is maker≠checker across machines, and it falls out of the format for free — which is why the
origin attestation must be carried even though v1 ships single-player.

## Servers

**TL8 — a discovered server with zero admitted tools is a first-class state.** Admission is lazy
[ruling: maintainer, 2026-07-25]: `tegh wrap` discovers and snapshots, then offers a per-server batched
review, and a human may admit none. The lock MUST distinguish "this server was discovered and
nothing was admitted" from "this server was never seen" — the two have opposite meanings on the
next `tegh status`, and collapsing them turns a deliberate refusal into an apparent gap.

**TL9 — each server entry records which harness it came from, where within it, and how to reach
it:** its `server_id`, the `harness` that was wrapped, the config `scope` the winning definition
was read from, its transport, and its non-secret connection configuration (`command` + `args` +
`cwd`, or `url`). Scope is load-bearing twice over: unwrap must restore to the file it took from,
and a server *appearing at a higher-precedence scope* is a drift class that shadows an admitted
entry without changing any tool hash.

**TL9a — `harness` is a closed catalog; `scope` is closed only WITHIN a harness.**
[ruling: maintainer, 2026-07-25] Scope vocabularies are harness-specific — Claude Code's five load-side
scopes plus managed are not Cursor's, OpenClaw's, or Hermes's — so the format cannot enumerate
scopes without either privileging one harness or accumulating every harness's terms in a shared
namespace where two different "project" scopes are indistinguishable. Therefore `harness` is the
closed catalog the schema validates, and `scope` is a string the *adapter* validates against its
own closed set. The closure doesn't disappear; it moves to the layer that can actually judge it.

The catalog may GROW WITHOUT A FORMAT BUMP. A lock naming a harness this tegh has no adapter for
is a **capability** failure ("this lock needs an adapter you don't have"), not a format failure —
the document is still perfectly well-formed and a newer tegh reads it unchanged. That distinction
is what keeps adding the fifth adapter from being a breaking change to an artifact already
committed in users' repositories.

**TL10 — no secrets, and no secret-location convention.** The lock records the *names* of
environment variables and headers a server requires — those names are already public, they sit in
`.mcp.json` today, and the lock needs them to know what must be injected. It records **no
values**, and deliberately **no convention describing where a relocated secret now lives**. Secret
relocation is part of the wrap contract, but publishing its naming format in a frozen public
artifact would freeze it — a public format freezes the band-aid. The resolution lives
store-side, where it can still change.

This clause is UNCHANGED by the secret-naming ruling (2026-07-27) and by relocation shipping
(v0.63.0). The ruling fixed what a secret is NAMED — a bare leaf, with the deploying topology
supplying the scope (`docs/config-provenance.md`) — and TL10 is about where the lock says it
LIVES, which is still nowhere. Locally the per-project directory under the tegh home plays the
topology's part; a reader of `tegh.lock` cannot tell that from the artifact, and should not be
able to.

## Rendering

**TL11 — diffs follow the M5 finding-flood discipline.** A `description` delta is a *steering*
change, is the injection vector, and is rendered **verbatim and in full**. An
`input_schema` delta is a *contract* change and is machine-summarized (added / removed / retyped /
newly-required). Cosmetic churn is a count. A bulk acknowledgment flag may cover the latter two;
every description delta additionally requires explicit per-tool acknowledgment, refused before any
write is burned. This mirrors `bulk-ratify` exactly — the countermeasure is against reviewer
complacency, not against invisibility.

**TL11a — a newly-REQUIRED field on a REMOTE server is a disclosure escalation, and joins the
top tier.** [ruling: maintainer, 2026-07-25 — amends TL11's rendering, no format change] On a `url`
server the input schema is not merely model-facing surface: it is an **exfiltration-channel
specification**, enumerating what the vendor receives on every call. A field the agent
must now send to a third party is categorically more serious than the same field appearing on a
locally-spawned child, so it is rendered verbatim and in full, it names the destination URL, and
it requires its own per-tool acknowledgment that the bulk flag cannot supply.

The amendment is deliberately **narrow — only newly required, only on a remote server**. Promoting
every remote `input_schema` delta would put retyped optionals in the unmissable tier and train
reviewers to bulk-dismiss it, which is exactly the finding-flood failure the tiering exists to
prevent. Whether a field is brand new or was already advertised as optional is folded into the
escalation line rather than repeated in the contract summary below it: one field, one mention.

Within the top tier, disclosure renders **above** steering. Both require their own acknowledgment,
so the order is a legibility choice and not a security one — which is why TL11's original "first"
now qualifies the *tier* rather than the description specifically.

No schema change was needed for any of this: `LockedServer` already records `transport`, so this
is a rendering decision throughout.

## ToolOp classification: proposed from annotations, confirmed by a human

[ruling: maintainer, 2026-07-25]

MCP servers advertise `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`). tegh uses them to *propose* a classification, renders the proposal, and binds
only what the human confirms.

Annotations are untrusted input — a swapped server can claim `readOnlyHint: true` for a tool that
deletes. Two things make the proposal safe to offer:

1. **A human ratifies it.** The annotation never becomes a classification on its own authority.
2. **Annotations ride the signed set.** A server that later flips a hint breaks
   `def_hash`, quarantines the tool at discovery, and brings it back for re-vet. The suggestion
   cannot be changed after the fact without a visible drift.

When a hint is absent, the proposal falls to the restrictive end (`effect: write`,
`external: true`, `reversible: false`) — a missing annotation is not evidence of safety.

---

## Sequencing note

This contract was frozen before `tegh wrap`'s discovery half was built, deliberately: the format
decides what discovery must capture. In particular TL8 (zero-admitted servers), TL9 (scope of
origin, non-secret connection config), and TL10 (credential *names*) are each a discovery
requirement that would have been missed by building discovery first and fitting a format to it.
