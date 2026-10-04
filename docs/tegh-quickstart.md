# tegh quickstart — from install to a refused call

The claim this walks: a coding agent on your Mac, with the tools it may call **pinned**, every call
**decided** before it runs, and every decision **on a tape you can read**. No AWS account, no
external service.

It ends at a refusal you can see — and then at releasing that one call by hand and reading both
halves back off the tape — because an install that ends at "it's installed" demonstrates nothing a
config file could not.

**What it proves and does not:** this is **posture 1** of the platform's
[posture ladder](https://github.com/wjatx/ptc-gal-reference/blob/main/docs/posture-ladder.md) — the gateway
decides every brokered call and records it, on the same OS user as the agent. It is a boundary
against an *injected* agent, not against someone who already has your login. Read
"What this does not prove" at the end before repeating any of it as a guarantee.

---

## 0. What you need

- macOS, Python 3.12+, and **Claude Code** (the agent being wrapped; v1 wraps Claude Code only).
- **Node**, only for the example server below — `npx` fetches it. Nothing in tegh needs Node.
- Time to read nine tool classifications in §4, which is where a person's time goes. See "Timings"
  for what has and has not been measured.

## 1. Install

In a virtual environment, with Python 3.12 or newer:

```bash
python3 -m venv venv && source venv/bin/activate
python -m pip install tegh
```

`tegh` is then on your `PATH`. Verify with `tegh --help`.

> There is no `mcp` extra to ask for. tegh does not declare one, and its dependency on the platform
> already includes the MCP client that discovery needs, so the plain install is complete.
> `safe-agents` on PyPI is the platform tegh depends on, and installing it alone does not give you
> tegh.

**With every dependency checked against a pinned hash.** `pip install tegh` resolves version ranges
from the package's metadata, and a range cannot carry a hash. Each GitHub release of this repository
(<https://github.com/Third-Ralph/tegh/releases>) attaches `tegh-<version>-requirements.txt`, which
pins `tegh` at the hash of the released wheel and every package it depends on, the platform
included, at an exact version with hashes. Download it and install from it instead:

```bash
python -m pip install --require-hashes -r tegh-<version>-requirements.txt
```

The same release carries `SHA256SUMS`, which lists the sha256 of the wheel, the sdist and that file.

**From a checkout**, to work on tegh itself. `requirements/dev.txt` is the project's dependencies and
its `dev` extra at exact versions with hashes, and the second command installs the checkout against
them without downloading anything:

```bash
git clone https://github.com/Third-Ralph/tegh.git && cd tegh
python3 -m venv venv && source venv/bin/activate
python -m pip install --require-hashes -r requirements/dev.txt
python -m pip install --no-index --no-build-isolation --check-build-dependencies -e ".[dev]"
```

## 2. A project to wrap

Use a throwaway one the first time. This creates a project whose agent has one real, third-party
MCP server — the standard knowledge-graph memory server — in an isolated harness home, so your own
Claude Code config is never touched:

```bash
mkdir -p ~/tegh-demo ~/tegh-demo-home && cd ~/tegh-demo

python - <<'PY'
import json, pathlib
project = str(pathlib.Path.home() / "tegh-demo")
config = {
    "disableClaudeAiConnectors": True,
    "projects": {project: {"mcpServers": {
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}
    }}},
}
path = pathlib.Path.home() / "tegh-demo-home" / ".claude.json"
path.write_text(json.dumps(config, indent=2), encoding="utf-8")
print("wrote", path)
PY
```

`~/tegh-demo-home` is the **harness home** — the directory holding the `.claude.json` that tegh
will rewrite. Claude Code reads it when you set `CLAUDE_CONFIG_DIR` to that directory, which is how
§5 drives the demo without going near your real config.

To wrap a project you already use, skip this step and pass your real home instead — but read §8
first, and know that `tegh unwrap` restores the config byte-for-byte, after showing you what it
will change.

## 3. Mint this machine's authority

```bash
tegh init
```

This writes `~/.tegh`: an HMAC key and an issuer signing key, both `0600`. It is **local
authority** — the tool namespace and the activation rows live here, deliberately **outside any
project tree**, which is what keeps `tegh.lock` a projection rather than something an agent with
write access to your repo could forge (`docs/tegh-lock.md`, TL1).

## 4. Wrap

```bash
tegh wrap claude --project "$(pwd)" --harness-home ~/tegh-demo-home
```

tegh spawns each declared server, reads what it actually advertises, and shows you every tool
before anything binds. For each one you answer `[y] admit  [e] edit classification  [N] skip`.

**This review is the product.** You are deciding, per tool, what the broker will let the agent do
with it. Read the provenance markers. This is the second tool the review shows you:

```
  --- memory/create_entities ---
  title: Create Entities
  DESCRIPTION (verbatim, in full — this text steers the model):
  --- description ---
Create multiple new entities in the knowledge graph
  input_schema: entities (array, REQUIRED)
  proposed classification — CONFIRM before it binds:
     effect     write    [UNTRUSTED] the server advertises readOnlyHint: false
     external   true     [FACT     ] an MCP tool always crosses a trust boundary: the call leaves this process for code we do not control, and the server's openWorldHint: false claim does not lower it (that hint is about the server's own reach, not this boundary)
     reversible true     [UNTRUSTED] the server advertises destructiveHint: false — its own claim that a mistake here is recoverable
     egress_arg (none)   [DEFAULT  ] not proposed — no annotation names which argument egresses. Egress metering stays OFF for this tool unless you name one (worth naming: this tool reaches an external party)
     NB fields marked UNTRUSTED come from the server's own annotations —
        a hostile server can claim readOnlyHint on a tool that deletes.
  def_hash: ba40bef601e1f43031d8e8b9a79f2530795fbf433862223c95baec27adfc2c63
  create_entities: [y] admit  [e] edit classification  [N] skip
```

`[FACT]` is something tegh established. `[UNTRUSTED]` is the *server's own claim about itself*,
shown so you can overrule it with `e`. `[DEFAULT]` is a field nobody proposed. A server that lies
about `readOnlyHint` is exactly the case this review exists for.

Look at the `reversible` line. The server says a mistaken `create_entities` is recoverable, and that
claim decides what happens at call time: the broker lets a reversible external write run and holds
an irreversible one for your approval. For this walk, do not take the server's word. Answer `e`,
keep `effect`, set `reversible` to `n`, keep `egress_arg`:

```
  create_entities: [y] admit  [e] edit classification  [N] skip e
    effect [write] (read/write, enter to keep):
    reversible [True] (y/n, enter to keep): n
    egress_arg [None] (argument name, '-' for none, enter to keep):

  classification AS CORRECTED — CONFIRM before it binds:
     effect     write    [UNTRUSTED] the server advertises readOnlyHint: false
     external   true     [FACT     ] an MCP tool always crosses a trust boundary: the call leaves this process for code we do not control, and the server's openWorldHint: false claim does not lower it (that hint is about the server's own reach, not this boundary)
     reversible false    [YOU SET  ] set to False by the human reviewing this wrap, overriding the proposed True (annotation)
     egress_arg (none)   [DEFAULT  ] not proposed — no annotation names which argument egresses. Egress metering stays OFF for this tool unless you name one (worth naming: this tool reaches an external party)
     NB fields marked UNTRUSTED come from the server's own annotations —
        a hostile server can claim readOnlyHint on a tool that deletes.
  create_entities: [y] admit  [e] edit classification  [N] skip y
  -> ADMIT create_entities
```

The `reversible` line now reads `[YOU SET  ]`, and that is what binds.

Admit the three reads (`read_graph`, `open_nodes`, `search_nodes`) as proposed, and skip
`add_observations`, `create_relations` and the three `delete_*` tools. The order is alphabetical,
so the whole review is:

| Tool | Answer |
|---|---|
| `add_observations` | `[Enter]` (skip) |
| `create_entities` | `e`, `[Enter]`, `n`, `[Enter]`, then `y` |
| `create_relations` | `[Enter]` |
| `delete_entities` | `[Enter]` |
| `delete_observations` | `[Enter]` |
| `delete_relations` | `[Enter]` |
| `open_nodes` | `y` |
| `read_graph` | `y` |
| `search_nodes` | `y` |

The review ends by telling you what you just chose:

```
  memory: 4 of 9 tool(s) admitted

  !! 1 admitted tool(s) are classified as IRREVERSIBLE WRITES: create_entities
     The broker HOLDS every call to these; none executes until you release it with
     `tegh approve <intent-id>` — once per call, not once for the tool. If any is
     really a read, re-run `tegh wrap` and correct it with [e] — a missing
     readOnlyHint is what proposed the write.
```

Then the admissions, a signed `tegh.lock` in the project, and `INTERPOSED`:

```
wrote /Users/you/tegh-demo/tegh.lock — 4 tool(s) pinned across 1 server(s)
  signed by tegh-local-248fb1d327fa -> tegh.lock.sig

grants issued for the admitted coordinates (store: /Users/you/.tegh/tegh.db)

==============================================================================
INTERPOSED
==============================================================================
  displaced local:/Users/you/tegh-demo-home/.claude.json: memory
  gateway   local:/Users/you/tegh-demo-home/.claude.json as 'tegh'
  backup    /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/wrap-backup.json
  audit     /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/audit.jsonl

See what the broker records: tegh audit --verify --project /Users/you/tegh-demo
Restore with: tegh unwrap --project /Users/you/tegh-demo
```

The harness config now names one server, `tegh`, and the memory server is reached only through it.

> If you admit `create_entities` as the server proposes it, with `y` alone, the rest of this walk
> does not happen: the write in §5 executes without a hold, because you accepted the server's claim
> that it is reversible. That is the control working as configured, and it is why the claim is
> marked `[UNTRUSTED]`.

> **Do not use `--admit-all` here.** It admits every proposal as-is, so a server that advertises no
> annotations yields tools classified as irreversible writes — and the broker then holds EVERY call
> to them, each needing its own `tegh approve` (§6). The human correcting a classification is the
> path, not friction to trim.

## 5. Watch the control fire

Point Claude Code at the isolated home and ask it to read the graph, then create an entity:

```bash
cd ~/tegh-demo
export CLAUDE_CONFIG_DIR=~/tegh-demo-home
claude
```

`claude mcp list` should show `tegh … ✔ Connected`, and it will be the only server. The read
executes. The write does not, and the agent reports the broker's answer in its own words. The
answer it was given is this line:

```
memory.create_entities is held for approval (intent intent-db63e8346c9910d9); it has NOT executed
```

That is the whole thesis in one line, and note **who said it**: the agent reported its own write
being stopped. It did not have the option of not telling you, because the refusal came back in
place of the result.

`create_entities` is held because it is an irreversible external write. The broker decided that
from the classification you set in §4 — not from anything the agent or the server said at call time.

To see the broker's answers verbatim, or to do this step without Claude Code, `tegh call` drives one
call through the same gateway as the same principal. The samples on this page were captured this
way:

```bash
tegh call memory__read_graph --project "$(pwd)"
tegh call memory__create_entities --project "$(pwd)" \
  --args '{"entities":[{"name":"tegh-demo","entityType":"note","observations":["created through the gateway"]}]}'
```

The first prints the server's result and exits 0. The second prints the held line above and exits 1.
Your intent id will differ from the one shown here.

## 6. Release it

The hold is a question, not a wall. Answer it with the intent id you were just given:

```bash
tegh approve intent-db63e8346c9910d9 --project "$(pwd)"
```

You get the stored call to look at before you agree to it, and then it runs:

```
[broker] envelope load mode: manifest
[broker] store backend: sqlite (/Users/you/.tegh/tegh.db)
[broker] audit sink: file (/Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/audit.jsonl); secrets: file (/Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/secrets.json)
[broker] grant load mode: read

  HELD CALL
    intent      intent-db63e8346c9910d9
    principal   tegh-tegh-demo-7f74e45c2bca
    tool.op     memory.create_entities
    argsDigest  sha256:9eea35dc281b7f39a916cf3c24e7c9fd2c8a86551c19b3546847eb1bddbd7c20
    held at     2026-10-04T15:11:56.301231+00:00
    expires     2026-10-04T16:11:56.301231+00:00

  irreversible external write: tegh-tegh-demo-7f74e45c2bca requests memory.create_entities (external=True, reversible=False)

  Releasing executes the STORED call above — not anything the agent has
  said since. It does not change the grant: the next such call is held
  again. Tell the agent the write is DONE rather than to retry it, or it
  will ask for a second one.

  Release as local-solo:you@yourmac#owner? [y/N] y
Knowledge Graph MCP Server running on stdio
MCP tool quarantined at discovery: memory.create_relations reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
MCP tool quarantined at discovery: memory.add_observations reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
MCP tool quarantined at discovery: memory.delete_entities reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
MCP tool quarantined at discovery: memory.delete_observations reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
MCP tool quarantined at discovery: memory.delete_relations reason=unlisted detail=advertised tool is not declared in AgentManifest.mcp_servers
RELEASED — memory.create_entities executed, approvedBy local-solo:you@yourmac#owner
  The audit tape now carries the hold and this release; see `example-wrapper audit`.
```

Three things in that output are the point:

- **It executes the *stored* call.** Not a re-render, and not whatever the agent has said since the
  turn ended. That is WYSIWYE, and it is why the digest is printed next to the summary.
- **The grant did not change.** You released one call. The next `create_entities` is held again.
  Releasing is not promoting.
- **`local-solo:` is an honest label, not an authentication.** Nothing here verified who you are —
  anyone who can run the command is that operator. That is posture 1, said out loud rather than
  papered over with an official-looking approver name.

The rest is the platform's broker starting up to run the call. The `[broker]` lines say where it
reads its store and writes the tape. `Knowledge Graph MCP Server running on stdio` is the memory
server starting. The five `quarantined at discovery` lines are the five tools you skipped in §4: the
server still advertises them, and the broker refuses to load a tool you did not admit.

The last line names a command that does not exist. `example-wrapper` is a placeholder printed by the
platform package (`wjatx/ptc-gal-reference#155`); the command you want is `tegh audit`, in §7.

Then tell the agent the write is **done**. Do not tell it to retry: the call has already executed,
and a retry asks for a second one.

> **A one-command release is act-safe *friction*, and that is a choice** — it says a blocked call
> is worse than an executed one, which is reasonable for a coding agent and wrong for an agent that
> can move money. This is a different axis from the polarity tegh *records*: holding rather than
> executing is abstain-safe at the decision point, and `tegh wrap --polarity` (default `abstain`)
> declares that per project. **Setting it changes no decision today** — the value is stored and
> nothing reads it (`wjatx/ptc-gal-reference#124`). `tegh posture` reports both, and which is which,
> rather than letting either imply the other.

## 7. Read the tape

Every decision above is on an append-only, hash-chained JSON-lines file, and tegh will show you
where it is and check it:

```bash
tegh audit --verify --project "$(pwd)"
```

```
audit tape  /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/audit.jsonl
  3 record(s)

  [   0] 2026-10-04T15:11:55.629021+00:00  memory.read_graph  allow/executed
         args sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
  [   1] 2026-10-04T15:11:56.301516+00:00  memory.create_entities  require_approval/held
         args sha256:9eea35dc281b7f39a916cf3c24e7c9fd2c8a86551c19b3546847eb1bddbd7c20
         why  irreversible external write
         intent intent-db63e8346c9910d9
  [   2] 2026-10-04T15:11:58.369288+00:00  memory.create_entities  allow/executed
         args sha256:9eea35dc281b7f39a916cf3c24e7c9fd2c8a86551c19b3546847eb1bddbd7c20
         approved by local-solo:you@yourmac#owner
         intent intent-db63e8346c9910d9

CHAIN CONSISTENT — 3 records, seq 0..2
  The chain is unkeyed SHA-256, so this proves the tape is SELF-CONSISTENT: no record was edited, dropped or reordered in place. It is NOT tamper-evidence — anyone who can write this file can rewrite it whole and recompute every hash. That needs off-device append-only storage (posture 3, docs/posture-ladder.md).
```

Records 1 and 2 are the interesting pair: the same `argsDigest` and the same `intentId`, one held
and one executed. That is the proof that what you approved is what ran — the digest on the release
is recomputed from the stored bytes, never copied off the hold.

Each record's `prevHash` is the previous record's `hash`, so a line cannot be removed from the
middle without breaking the chain. Arguments are never stored — only `argsDigest`.

Read `CHAIN CONSISTENT` narrowly. It means nothing was edited or dropped *in place*; it does not
mean the file is trustworthy against someone who can write to it, which at this posture is anyone who
can run `tegh`. The `docs/posture-ladder.md` that verdict cites is a file in the platform's
repository, [here](https://github.com/wjatx/ptc-gal-reference/blob/main/docs/posture-ladder.md).

**The record this walk has not produced yet is a refusal, and where one comes from is the
interesting part.** An honest agent will never produce it. tegh does not advertise the tools you
skipped, so Claude Code simply does not see `delete_entities`; ask it to delete something and it
will tell you no such tool exists and stop. A refusal is what happens when a client calls a
coordinate it was **never offered** — a compromised or injected agent guessing at a name. It is
refused by the broker and recorded anyway.

That is "a fully compromised agent can still only ask", and it is the line worth checking yourself.
To reproduce it you have to *act* like the compromised agent and call a coordinate the gateway
never offered you:

```bash
tegh call memory__delete_entities --args '{"entityNames": ["x"]}' --project "$(pwd)"
echo "exit status: $?"
```

```
memory.delete_entities refused by the broker: no manifest entry for memory.delete_entities
exit status: 1
```

Exit status 1 means the broker answered and the call did not execute. 0 means the call executed, and
2 means tegh could not ask at all. Run `tegh audit --project "$(pwd)"` again and the tape holds one
more record, which is yours:

```
  [   3] 2026-10-04T15:12:10.823571+00:00  memory.delete_entities  deny/denied
         args sha256:c3b3b982fef23fcb97eb4e7f1403da4b02eeb8b3e64f9fa452e9d985a5a8039e
         why  no manifest entry for memory.delete_entities
```

## 8. Undo

```bash
tegh unwrap --project "$(pwd)"
```

tegh first lists everything it is about to change and what it will leave alone, then asks. Answer
`y` (or press Enter) to go ahead. Any other answer changes nothing and exits 1.

```
tegh unwrap will change, for /Users/you/tegh-demo:

  restore  local:/Users/you/tegh-demo-home/.claude.json  (memory)
  remove   tegh's wrap backup at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/wrap-backup.json

It leaves untouched:

  tegh.lock and the admitted rows, so re-wrapping does not re-run the ceremony
  the audit tape at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/audit.jsonl

Proceed? [Y/n] y
  restored  local:/Users/you/tegh-demo-home/.claude.json  (memory)
  removed   tegh's wrap backup at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/wrap-backup.json

unwrapped /Users/you/tegh-demo (wrapped 2026-10-04T15:11:54.699203+00:00). The harness reaches its original servers directly again. tegh.lock, the admitted rows and the audit tape are untouched, so re-wrapping does not re-run the ceremony.
```

Restores the displaced config **byte-for-byte**. Only the files the unwrap really changes are
listed: this project had no `.mcp.json` and no user-scope servers, so neither appears.

This walk relocated no credential. When a wrap did move one out of your config (you answered
CREDENTIAL in the review), the plan carries two more lines for it, naming the server and the field
and never the value: the credential is put back into the harness config, and then removed from
tegh's store. The removal happens only after tegh has read the restored config back and found the
value there, so a failed restore leaves the store's copy where it was.

From a script, where nobody can answer, pass `--yes`. Without it, a stdin that is not a terminal is
refused and nothing is changed.

---

## Timings

Timed by the maintainer on 2026-07-29, on a warm machine, knowing every answer in advance. It is
**not** a measurement of how long this takes anyone else. Nobody else has walked this yet; the first
timed walk by someone other than the maintainer is tracked as #2.

| Step | Elapsed |
|---|---|
| `python -m venv` + `pip install` (warm pip cache) | 6s |
| `tegh init` + `tegh wrap` (warm `npx` cache, answers piped) | 3s |
| Claude Code driving one read + one write | 30s |

Those are machine times, with nobody reading or deciding anything. A person's time goes almost
entirely to §4: reading nine tool classifications and deciding on each. That cost scales with the
number of tools a project's servers advertise, not with anything tegh does, and it is the number a
real onboarding measurement has to capture. A first run also pays an unmeasured cold-cache cost for
`npx` fetching the server.

## What this does not prove

Stated because every claim here names its posture or is an overclaim (the platform's
[posture ladder](https://github.com/wjatx/ptc-gal-reference/blob/main/docs/posture-ladder.md)):

- **This is posture 1.** The gateway is a boundary against an injected agent. It runs as the same OS
  user as the agent, so anything that user could do directly — editing `~/.tegh`, rewriting the
  harness config back, calling the memory server itself — is not stopped by it. Posture 2 puts the
  agent in a container with the gateway outside; tegh does not do that yet.
- **The local tape is not tamper-evident.** The hash chain proves *self-consistency*: it catches a
  line edited or removed in isolation. Anyone who can write the file can also rewrite every
  subsequent hash. Tamper-evidence needs write-once storage the agent's user cannot reach (S3
  Object Lock on the cloud floor). The local file is the stand-in, and it is stated as such in
  the platform package's `safe_agents/broker/audit/_file_sink.py`.
- **`tegh audit --verify` is a consistency check, not a tamper check.** It is worth running and it
  is not evidence against an adversary; see the caveat it prints with every verdict.
- **The release is attributed, not authenticated.** `tegh approve` records `local-solo:<you>@<host>`
  because that is all a laptop can honestly say. Anyone who can run the command can release a held
  call, and maker≠checker is degenerate here by construction — one human, one credential. On the
  cloud floor the approver comes from an authenticated owner channel, which is why the local
  identity is refused there rather than silently downgrading the record.
- **MCP servers still run unconfined.** tegh spawns them as child processes with the environment it
  was told to give them. It decides *whether a call happens*, not what the server does once it runs.
