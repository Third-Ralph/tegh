---
name: tegh-verify-wrap
description: >-
  Check that a tegh-wrapped project does what its review said, using the tegh CLI alone. Use
  right after the user finishes `tegh wrap claude`, after any second wrap, when the user asks
  "is tegh working" or "what can the agent call here", or when an agent reports a call was held
  or refused. Drives one call that executes, one the broker holds or refuses, and verifies the
  audit chain. Reads and reports; the user is the one who releases a held call or unwraps.
---

# Check a wrapped project: one call allowed, one stopped, the tape verified

A wrap that printed `INTERPOSED` has rewritten the agent's MCP configuration so that tegh's
gateway is its only server. This skill shows that the broker behind the gateway decides calls the
way the review said, without starting a coding agent. `tegh call` drives one call through the
same gateway as the same principal and prints the broker's answer.

Rules: you do not run `tegh approve` or `tegh unwrap` (the user does), and you stop at any line
starting `REFUSED`, `TAMPER` or `UNVERIFIABLE` and show it whole. Every `tegh call` you make is
recorded on the project's audit tape, so make the ones below and no others.

Run everything from the project root. `--project <path>` works from elsewhere, with one side
effect: `tegh posture` creates an empty credential map in the tegh home, under a directory named
for the directory it is run from, when that file is absent.

## Step 1. Is the project wrapped now?

```bash
tegh posture --project .
```

Pass `--harness-home <dir>` as well if the wrap was given one. Posture is the one command in this
skill that takes it. `tegh status`, `diff`, `call` and `audit` answer
`tegh: error: unrecognized arguments: --harness-home <dir>` and exit 2.

**Expect:**

```
  POSTURE 1  (docs/posture-ladder.md)
    This configuration is at posture 1 — MCP calls are gated on this one machine, by tegh, as the same OS user as the agent
```

`POSTURE pre-1` means the agent's configuration does not point at the gateway: the project was
never wrapped, was unwrapped, or posture was given a different `--harness-home` from the wrap.
Stop there and tell the user which. No later step means anything on an unwrapped project.

## Step 2. What is pinned?

```bash
tegh status --project .
tegh diff --project .
```

**Expect** from `tegh status`, one line per admitted tool:

```
/Users/you/tegh-demo/tegh.lock  (format v1, generated 2026-10-06T13:05:31.771536+00:00)
  signature: present, VERIFIED against tegh-local-a2292051cce5

  memory  [claude-code / local / stdio]  npx -y @modelcontextprotocol/server-memory
    create_entities              write external=true  reversible=false [solo-attested by local-solo:tegh-local-a2292051cce5]
    open_nodes                   read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-a2292051cce5]
    read_graph                   read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-a2292051cce5]
    search_nodes                 read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-a2292051cce5]
```

**Expect** from `tegh diff`, which starts each server again to compare:

```
lock signature: present, VERIFIED against tegh-local-a2292051cce5


  memory: 5 advertised tool(s) not admitted (not callable): add_observations, create_relations, delete_entities, delete_observations, delete_relations

DRIFT=0 WITHDRAWN=0 UNCHANGED=4 UNADMITTED=5
```

Exit status 0 when nothing drifted, 1 when something did. If the project has a `tegh-job.md`,
compare both outputs with its role table line by line and report every difference.

From these two outputs pick three tools:

- **a read**: a line with `read`;
- **a held write**: a line with `write` and `reversible=false`, if there is one;
- **an unadmitted tool**: a name from the `not admitted (not callable)` line. If that line is
  absent, every advertised tool was admitted; use a name the server does not have, such as
  `<server>__no_such_tool`.

## Step 3. A call the broker allows

The tool name is `<server>__<tool>`, with two underscores.

```bash
tegh call memory__read_graph --project .
echo "exit status: $?"
```

**Expect** the server's result as JSON, then `exit status: 0`. With `--json` the answer is one
line: `{"tool": "memory__read_graph", "executed": true, "text": "..."}`.

A tool with required arguments needs `--args '<one JSON object>'`. Read the argument names from
the tool's schema, and do not guess at values that would change anything.

**If it does not execute**

| You see | Exit | Meaning |
|---|---|---|
| `refused by the broker: tool not granted to this principal` | 1 | The wrap reported this tool admitted and the broker has no usable grant for it. This follows a second wrap that changed the tool set ([#14](https://github.com/Third-Ralph/tegh/issues/14)): the tools from the earlier wrap are refused, and a tool the second wrap added can be called. Stop and tell the user both halves; the wrap's exit status was not the truth. |
| `was allowed by the broker but failed at the connector` | 1 | The broker allowed it and the server failed. Check step 1 again: this is what an unwrapped project answers when its credential has gone back to the agent's config. |
| `REFUSED: ... has not been wrapped, so there is no gateway to call` | 2 | No wrap has ever completed for this project and this tegh home. |
| `REFUSED: --args is not JSON` | 2 | Fix the quoting: one JSON object in single quotes. |

Exit status 2 always means tegh could not ask. 1 means the broker answered and the call did not
execute. 0 means it executed.

## Step 4. A call the broker stops

Do one of these. Do both if the project has a held write.

**A held write.** Ask the user before making this call: releasing it later would run it for real.
Use arguments the user agrees to.

```bash
tegh call memory__create_entities --project . \
  --args '{"entities":[{"name":"tegh-demo","entityType":"note","observations":["created through the gateway"]}]}'
echo "exit status: $?"
```

```
memory.create_entities is held for approval (intent intent-792774f5cd77da58); it has NOT executed
exit status: 1
```

Give the user the intent id. Releasing it is their command, run in their terminal:
`tegh approve <intent-id> --project <path>`. It prints the held call and asks before it runs:

```
  HELD CALL
    intent      intent-d23ef5702f1faf7c
    principal   tegh-proj1-a99b46190412
    tool.op     memory.create_entities
    argsDigest  sha256:9eea35dc281b7f39a916cf3c24e7c9fd2c8a86551c19b3546847eb1bddbd7c20
    held at     2026-10-06T13:26:39.860793+00:00
    expires     2026-10-06T14:26:39.860793+00:00

  irreversible external write: tegh-proj1-a99b46190412 requests memory.create_entities (external=True, reversible=False)
```

That sample is from another project and run, so its intent, principal and times differ from the
ones above. The screen carries a digest of the arguments and not the arguments, so tell the user
what you passed. The digest is the same `args sha256:` value the tape shows in step 5. A hold
expires, and the `expires` line says when.

Tell the user three more things about what they will see. Lines starting `[broker]` are printed
first. After they answer `y`, the server's own start-up line is printed, and then one line for
each tool the review skipped:

```
MCP tool quarantined at discovery: memory.delete_entities reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
```

Those lines are expected. "Quarantined" there means the server advertises a tool that was not
admitted, which is what skipping it asked for. Then comes the line starting `RELEASED`, and the
last line refers to `example-wrapper audit`: that wording comes from the platform package tegh
is built on, and the command is `tegh audit`. One release runs one call, and the next call to the
same tool is held again.

**A refusal.** This is the position of an agent calling a tool it was never offered.

```bash
tegh call memory__delete_entities --args '{"entityNames": ["x"]}' --project .
echo "exit status: $?"
```

```
memory.delete_entities refused by the broker: no manifest entry for memory.delete_entities
exit status: 1
```

## Step 5. Verify the tape

```bash
tegh audit --verify --project .
```

**Expect** one record per call made above, then the verdict:

```
audit tape  /Users/you/.tegh/projects/tegh-demo-a84a560e74c4/audit.jsonl
  3 record(s)

  [   0] 2026-10-06T13:05:34.117622+00:00  memory.read_graph  allow/executed
         args sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
  [   1] 2026-10-06T13:05:34.830928+00:00  memory.create_entities  require_approval/held
         args sha256:9eea35dc281b7f39a916cf3c24e7c9fd2c8a86551c19b3546847eb1bddbd7c20
         why  irreversible external write
         intent intent-792774f5cd77da58
  [   2] 2026-10-06T13:05:35.463334+00:00  memory.delete_entities  deny/denied
         args sha256:c3b3b982fef23fcb97eb4e7f1403da4b02eeb8b3e64f9fa452e9d985a5a8039e
         why  no manifest entry for memory.delete_entities

CHAIN CONSISTENT — 3 records, seq 0..2
  The chain is unkeyed SHA-256, so this proves the tape is SELF-CONSISTENT: no record was edited, dropped or reordered in place. It is NOT tamper-evidence — anyone who can write this file can rewrite it whole and recompute every hash. That needs off-device append-only storage (posture 3, docs/posture-ladder.md).
```

Exit status 0. Arguments are never on the tape, only their digest. If the user released the held
call, a fourth record follows with the same `args` digest and the same intent, `allow/executed`,
and an `approved by local-solo:<user>@<host>#owner` line.

`tegh audit --json --project .` prints the same records as JSON, with `decision`, `outcome`,
`reason`, `intentId` and `approvedBy` on each, for when you need to check them and not show them.

## What to report

Give the user five lines: the posture line, the admitted tools with their classifications, the
call that executed, the call that was held or refused with its reason, and the `CHAIN CONSISTENT`
line. Then say what this does not show, in the caveat's own terms:

- `CHAIN CONSISTENT` means no record was edited or dropped in place. It is not evidence against
  someone who can write the file, which at posture 1 is anyone who can run `tegh`.
- The broker ran as the same OS user as the agent. Anything that user can do directly, such as
  editing `~/.tegh` or putting the agent's configuration back, is not stopped.
- Only MCP tools go through the gateway. The agent's built-in shell, file and network tools do
  not.
- `tegh call` stood in for the agent's MCP client. No coding agent was started, so this says
  nothing about what a live session does.

## Undo

`tegh unwrap --project <path>` is the user's command. It prints what it will change and asks. It
needs a terminal, or `--yes` from the user. Ask the user to quit the coding agent in the project
first: the plan itself warns that a session still running can rewrite its configuration from
memory after the restore.
