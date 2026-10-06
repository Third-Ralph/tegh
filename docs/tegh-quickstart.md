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

To wrap a project you already use, skip this step and pass your real home instead — but read §9
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
claim decides what happens at call time: the broker holds every irreversible external write for your
approval, and lets a reversible one run when the turn making it has read nothing from outside. For
this walk, do not take the server's word. Answer `e`, keep `effect`, set `reversible` to `n`, keep
`egress_arg`:

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
     really a read, run `tegh unwrap`, then `tegh wrap` again, and correct it with
     [e] — a missing readOnlyHint is what proposed the write.
```

Then the admissions, a signed `tegh.lock` in the project, and `INTERPOSED`:

```
==============================================================================
PROPOSING (maker; nothing is admitted until the checker ratifies)
==============================================================================
  proposed  memory/create_entities
  proposed  memory/open_nodes
  proposed  memory/read_graph
  proposed  memory/search_nodes

==============================================================================
ADMITTING (checker ratifies, one tool at a time)
==============================================================================
  grants issued for the admitted coordinates (store: /Users/you/.tegh/tegh.db)
  admitted  memory/create_entities
  admitted  memory/open_nodes
  admitted  memory/read_graph
  admitted  memory/search_nodes

wrote /Users/you/tegh-demo/tegh.lock — 4 tool(s) pinned across 1 server(s)
  signed by tegh-local-248fb1d327fa -> tegh.lock.sig

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

A wrap has a commit point, and it is the line between those two headings. Before it, the wrap
writes files (its manifest, the lock, the backup and the harness config, in that order) and
proposes each admission. A proposal makes no tool callable. If the wrap stops there (Ctrl-C, a
`kill`, a closed terminal, input that ends at a question, no server reachable, no tool admitted, a
failed proposal), it puts back every file it had written, withdraws its proposals, and says so in
one line that starts `REFUSED`, `INTERRUPTED`, `NOT WRAPPED` or `FAILED`. Nothing was admitted, so
a call gets the answer it got before the command.

After the commit point the wrap issues the grants and ratifies the admissions, one tool at a time,
and its manifest names a tool only once that tool's admission is ratified. None of that can be
taken back, so a wrap that stops there is not rolled back. It ends `FAILED` or `INTERRUPTED` with
one line that says the project is wrapped, names the tools that were admitted and the ones that
were not, and gives the commands to run: `tegh unwrap` to undo it, and then `tegh wrap` again to
finish. A tool that was not admitted is refused if it is called.

The order of those writes is what covers the stop nothing can report, a `kill -9` or a power cut.
At every point in a wrap of this project, a call for it executes only what it executed before the
wrap or what you reviewed in this one, and never under a definition that neither the earlier
`tegh.lock` nor the one on disk pins.

> If you admit `create_entities` as the server proposes it, with `y` alone, the classification no
> longer holds the write. Made on its own with `tegh call` (§5), it executes, because you accepted
> the server's claim that it is reversible. That is the control working as configured, and it is why
> the claim is marked `[UNTRUSTED]`. Claude Code's write in §5 is held either way, for the second
> reason §5 describes: it comes after a read.

**The per-tool budget.** Every admitted tool is granted a number of calls per day, 200 unless
you pass `--daily-cap N` to `tegh wrap`. A call past the cap is refused with `capacity budget
breached` and recorded. The count is kept per tool, so one tool running out does not stop the
others.

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

The broker has two grounds for holding it, and the audit tape (§7) names the one that applied:

- **`tainted external write`** is what the tape says when Claude Code made the call. The session
  had already read the graph, and once a turn has read anything from outside, the broker holds every
  external write in it, a reversible one included. What the agent read may be steering what it
  writes, so a person decides. You did not configure this rule and cannot admit your way past it.
- **`irreversible external write`** is what the tape says when the write is the first thing in its
  turn, which is the `tegh call` below. This one comes from the classification you set in §4, not
  from anything the agent or the server said at call time.

To see the broker's answers verbatim, or to do this step without Claude Code, `tegh call` drives one
call through the same gateway as the same principal. The samples on this page were captured this
way:

```bash
tegh call memory__read_graph --project "$(pwd)"
tegh call memory__create_entities --project "$(pwd)" \
  --args '{"entities":[{"name":"tegh-demo","entityType":"note","observations":["created through the gateway"]}]}'
```

The first prints the server's result and exits 0. The second prints the held line above and exits 1.
Your intent id will differ from the one shown here. Each `tegh call` is its own turn, so the read in
the first command does not taint the write in the second.

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

This tape came from the two `tegh call` commands in §5, which is why record 1 gives `irreversible
external write` as its reason. Driven from Claude Code, the same record reads `tainted external
write`, because the session read the graph first.

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

## 8. Check the pins

`tegh.lock` in the project is the signed record of what you admitted. Two commands read it, and
neither changes anything:

```bash
tegh status --project "$(pwd)"
tegh diff --project "$(pwd)"
```

`tegh status` checks the lock against its signature and lists each pinned tool with the
classification that binds:

```
/Users/you/tegh-demo/tegh.lock  (format v1, generated 2026-10-05T12:57:46.666945+00:00)
  signature: present, VERIFIED against tegh-local-60db415f342a

  memory  [claude-code / local / stdio]  npx -y @modelcontextprotocol/server-memory
    create_entities              write external=true  reversible=false [solo-attested by local-solo:tegh-local-60db415f342a]
    open_nodes                   read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-60db415f342a]
    read_graph                   read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-60db415f342a]
    search_nodes                 read  external=true  reversible=none  [solo-attested by local-solo:tegh-local-60db415f342a]
```

If the lock was edited after it was signed, `tegh status` prints `TAMPER`, says the lock does not
match its signature, and exits 3. An edit to the lock changes no decision, because the broker does
not read it (`docs/tegh-lock.md`, TL1); what the edit does is show up here.

`tegh diff` spawns each server again and compares what it advertises now with what you admitted:

```
lock signature: present, VERIFIED against tegh-local-60db415f342a


  memory: 5 advertised tool(s) not admitted (not callable): add_observations, create_relations, delete_entities, delete_observations, delete_relations

DRIFT=0 WITHDRAWN=0 UNCHANGED=4 UNADMITTED=5
```

It exits 0 when nothing has drifted and 1 when something has. A tool drifts when the server changes
its definition after you admitted it. A changed description is the case to care about, since the
description is text written by the server for the model to read. `tegh diff` prints the admitted
description and the live one in full, one above the other. Until you admit the tool again the broker
refuses calls to it, the tape records `not callable (drifted)`, and the server's other tools keep
working. The memory server does not change its descriptions, so this walk cannot show you a drift.

Two things about recovering from one:

- As of 0.1.1 the drift report ends by naming a flag, `--acknowledge-description-change`, that no
  `tegh` command accepts (#10). To admit the changed tool, run `tegh unwrap` and then `tegh wrap`
  again. The review shows you the live description, and what you admit there is what gets pinned.
- Run `tegh unwrap` first. A `tegh wrap` on a project that is still wrapped refuses and changes
  nothing, and the refusal names the `tegh unwrap` command to run. tegh 0.1.1 did not refuse: it
  found only the gateway, admitted nothing, and replaced `tegh.lock` with an empty one (#11).

## 9. Undo

```bash
tegh unwrap --project "$(pwd)"
```

Quit the coding agent in this project first. A session that is still running can rewrite its
config from memory after the restore, and tegh does not check for one: the plan reminds you, and
that is all it does.

tegh lists everything it is about to change and what it will leave alone, then asks. Answer `y`
(or press Enter) to go ahead. Any other answer changes nothing and exits 1.

```
tegh unwrap will change, for /Users/you/tegh-demo:

  restore  local:/Users/you/tegh-demo-home/.claude.json  (memory)
  remove   tegh's wrap backup at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/wrap-backup.json

It leaves untouched:

  tegh.lock and the admitted rows, so re-wrapping does not re-run the ceremony
  the audit tape at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/audit.jsonl

Quit the coding agent in this project before you proceed: a session that is still running can rewrite its config from memory after the restore.

Proceed? [Y/n] y
  restored  local:/Users/you/tegh-demo-home/.claude.json  (memory)
  removed   tegh's wrap backup at /Users/you/.tegh/projects/tegh-demo-7f74e45c2bca/wrap-backup.json

unwrapped /Users/you/tegh-demo (wrapped 2026-10-04T15:11:54.699203+00:00). The harness reaches its original servers directly again. tegh.lock, the admitted rows and the audit tape are untouched, so re-wrapping does not re-run the ceremony.
```

Restores the displaced config **byte-for-byte**, with the permission bits the file already had.

The closing line says re-wrapping does not re-run the ceremony. As of 0.1.1 a second `tegh wrap`
does ask the tool review again, for every tool (#13). It remembers only the values you classified as
configuration.
Only the files the unwrap really changes are listed: this project had no `.mcp.json` and no
user-scope servers, so neither appears.

The unwrap takes out only what the wrap put in. A server you added to a wrapped scope since the
wrap (with `claude mcp add`, or a new `.mcp.json`) is kept, and the plan names it on a `keep` line
ending `added since the wrap`. If it has the same name as a server the unwrap is about to put back,
tegh refuses before changing anything and names the scope and the server; rename or remove one of
the two and run it again.

If the harness home had no `.claude.json` when you wrapped, the wrap created one for its entry.
The unwrap removes that file again when nothing else has been added to it, on a `remove` line
ending `the file itself, which the wrap created and nothing else has been added to`. If something
has been added, the file stays holding just that, and the empty `projects` entry the wrap made for
this project comes out on a `remove` line of its own. A file that was there before the wrap is
never removed.

This walk relocated no credential. When a wrap did move one out of your config (you answered
CREDENTIAL in the review), the plan carries two more lines for it, naming the server and the field
and never the value: the credential is put back into the harness config, and then removed from
tegh's store. The removal happens only after tegh has read the restored config back and found the
value there, so a failed restore leaves the store's copy where it was. If the restore stops
part-way, the message lists which files were restored and which were not, and says which
credential is for now in both a restored file and the store. Run `tegh unwrap` again to finish; it
works from what is on disk, including after an unwrap that was killed.

From a script, where nobody can answer, pass `--yes`. Without it, the prompt needs a terminal on
both stdin and stdout: a piped answer, or `tegh unwrap > out.txt` with the plan going into a file
nobody is reading, is refused and nothing is changed.

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
