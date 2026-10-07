# The agent's job in this project

<!-- Written by the tegh-define-job skill and confirmed by the person named below.
     This file is a record for people. The broker does not read it. -->

- **Project:** `<absolute path>`
- **Agent:** Claude Code
- **Stated by:** `<person>`, `<date>`
- **Posture:** 1 (a local wrapper, running as the same OS user as the agent)

## Purpose

> `<The person's own sentences, verbatim. Number them so the table can cite them.>`
>
> 1. ...
> 2. ...
> 3. ...

## Role

One row per tool the servers advertise. "Needed by" cites a sentence of the purpose by number, or
says "none". Where a tool's fit depended on how a sentence is read, the row says whose reading it
records.

| Server / tool | Needed by | Decision | Effect | Reversible | What a call gets |
|---|---|---|---|---|---|
| `memory/read_graph` | 1 | admit | read | n/a | executes |
| `memory/create_entities` | 2 | admit, held | write | false (set by reviewer) | held for approval |
| `memory/delete_entities` | none (3 forbids it) | skip | | | refused |

Daily cap per admitted tool: `<200, or the --daily-cap value>`

## Config values

Names only, never values. The purpose does not decide these: the person says what each value is.

| Field | Classified as |
|---|---|
| `local:vendor.env.LEDGER_API_KEY` | credential (moved to tegh's store) |
| `local:vendor.env.LEDGER_REGION` | configuration (carried through) |

## Answer sheet

One row per prompt. Type what the row says and press Return; "(nothing)" means Return alone.
Config values are asked first. After them, match each answer to the `--- server/tool ---` header
the review prints. A block that is not listed here, or whose description differs from the one
reviewed, is answered `N`.

| Review block | Prompt that is waiting | Type, then Return |
|---|---|---|
| `local:vendor.env.LEDGER_API_KEY` | `is LEDGER_API_KEY a CREDENTIAL?` | (nothing): credential |
| `local:vendor.env.LEDGER_REGION` | `is LEDGER_REGION a CREDENTIAL?` | `n`: configuration |
| `memory/add_observations` | `add_observations: [y] admit ...` | (nothing): skip |
| `memory/create_entities` | `create_entities: [y] admit ...` | `e` |
| | `effect [write] ...` | (nothing): keep |
| | `reversible [True] ...` | `n` |
| | `egress_arg [None] ...` | (nothing): keep |
| | `create_entities: [y] admit ...` | `y` |
| `memory/read_graph` | `read_graph: [y] admit ...` | `y` |

## Verification

| Check | Result | Date |
|---|---|---|
| `tegh status` lists exactly the admitted tools with these classifications | | |
| `tegh diff` names exactly the skipped tools and ends `DRIFT=0` | | |
| One admitted read through `tegh call` exits 0 | | |
| One skipped tool through `tegh call` is refused, exit 1 | | |
| `tegh audit --verify` ends `CHAIN CONSISTENT` | | |

## Changes

<!-- One dated line per change to the purpose or the role, and which wrap applied it. -->
