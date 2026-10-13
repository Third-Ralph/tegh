# Claude Code: extension and interposition surfaces

## About this document

This is the reference that tegh's wrap mechanics for Claude Code are built against. It records how
Claude Code stores and loads MCP server configuration, where MCP credentials sit, and which
mechanisms gate a change to that configuration. tegh's code and the output of `tegh posture` cite
it by section number and by line range.

**What it was checked against.** The documentation claims come from the vendor's public Claude
Code documentation, read on 2026-07-25. All URLs are under `code.claude.com/docs/en/` unless noted,
and 2026-07-25 is the access date for every page in the Sources list unless the Sources list says
otherwise. Where a documentation page states a version for a behaviour (for example "Requires
Claude Code v2.1.203 or later"), that stamp is copied here verbatim. The stamps are the
documentation's own and were not re-verified against a running binary. The observed behaviour was
recorded on 2026-07-27 on one developer machine. The Claude Code version running at the time was
not recorded.

**Update of 2026-10-06.** §3 (hooks) and §4 (permissions) were re-read against the documentation
on 2026-10-06, when the current release was 2.1.292, because tegh was about to install hooks built
against them. A claim checked on that date carries the date inside its tag, as in
`[S2, "PostToolUse input", 2026-10-06]`, and the pages added on that date are listed in Sources.
Material from 2026-07-25 that the re-reading contradicted is kept and marked **Changed by
2026-10-06**. The other sections were not re-read, apart from one note in §5. A second set of
behaviour was observed on 2026-10-06 against Claude Code 2.1.292 ("Coverage and limits").

**Update of 2026-10-12.** Two sets of behaviour were observed against Claude Code 2.1.295 and are
recorded here: a run on 2026-10-09 of failing hooks in every permission mode, and a managed-settings
matrix run twice on 2026-10-12 on a disposable macOS runner. The hooks page and the changelog were
read again on 2026-10-12 for `onFailure`, which the hooks page now documents. They add the
`onFailure` paragraphs to §3 ("Failure semantics"), the subsection "Managed settings, observed 2026-10-12" to §4, and two
blocks to "Behaviour observed since". A claim from them carries `[observed, <date>]` and, for the
matrix, the id of the cell it rests on (c01b, c06h and so on), which names a row of the verdict
table in §4.

**How to read the confidence labels.**

| Label | Meaning |
|---|---|
| Documented | The claim carries a source tag such as `[S1, "Section name"]`, pointing at a page in the Sources list. Section names are as recorded when the page was read. |
| Observed | The claim was seen on a running installation. It carries the date of the observation. |
| Inferred | The claim is reasoning from documented or observed facts, or from the absence of a documented mechanism. It is marked `[Inferred]` with its basis. |
| Unsourced | No source was recorded for the claim. It is kept, and marked `[Unsourced]`, so that code relying on it can be traced. |
| Not established | An open question. Marked `[unverified]` or stated as "Not established". |

Paragraphs that begin "For tegh" state a design consequence drawn by tegh's authors. They are not
claims about Claude Code.

**Re-verify before relying on it.** Configuration formats, flag names, version thresholds and
permission behaviour change between releases. Check a volatile claim against the current vendor
documentation, or against a running installation, before building on it. The section "Coverage and
limits" lists what was read, what was not, and all of the behaviour that was observed.

## Summary for tegh

**Interposition class: config rewrite.** The plugin and CLI-adapter classes do not fit, for the
reasons in this summary. Claude Code's MCP client reads server definitions from a small set of JSON
files/registry keys (`.mcp.json`, `~/.claude.json`, `managed-mcp.json`) and connects to whatever
they name (§1). There is no built-in gateway/proxy hook and no "MCP middleware" extension point
[Inferred: none is described in the pages read]. Plugins can only add servers, and cannot suppress
or wrap a server that a user or another source already configured [S1, "Plugin-provided MCP
servers"; see §6 for how far the page supports this]. For tegh, the lever is to write tegh's own
gateway as the sole entry in every scope that currently names a real server, using either direct
JSON writes or the `claude mcp` CLI. Both are described in §7 as sitting outside Claude Code's own
runtime permission system (see also §5).

The caveats below are ordered by how much each can silently break "the gateway is the only server
Claude Code sees".

1. **Scopes are shadowed and are never merged.** Local (`~/.claude.json` per-project), project
   (`.mcp.json`), user (`~/.claude.json` global), plugin-provided, and claude.ai connector are five
   independent sources. When the same name collides, the highest-precedence source wins as a whole
   entry, and different names at different scopes all still load side by side [S1, "Scope hierarchy
   and precedence"]. For tegh: rewriting only one scope leaves real servers reachable from the
   others.
2. **claude.ai connectors match by endpoint URL, and names play no part.** They are a cloud-side
   configuration that Claude Code fetches automatically for subscription-authenticated users, and
   no local file governs them [S1, "Use MCP servers from claude.ai"]. For tegh: a local-only wrap
   misses these entirely unless tegh also sets `disableClaudeAiConnectors` or matching
   `deniedMcpServers` entries.
3. **Self-modification is gated by protected paths, which sit outside the permission rules.**
   `.mcp.json` and `.claude.json` are hard-coded protected paths, and existing `permissions.allow`
   rules cannot pre-approve a write to them [S11, "Protected paths"]. The outcome of such a write
   depends on the permission mode: prompted, routed to a classifier, or denied, and allowed without
   a prompt only under `bypassPermissions` (table in §5). Claude Code itself, mid-session, therefore
   cannot cleanly self-wrap. For tegh: the wrap must happen from tegh's own installer process (a
   `claude mcp add`/`remove` invocation or a direct file write before Claude Code starts), which is
   not subject to this gate at all [Unsourced: see the second subsection of §7].
4. **The credential exposure that exists today is the one tegh removes.** With no sandbox enabled
   (the default), `env` blocks in `.mcp.json`/`~/.claude.json` and static `headers.Authorization`
   values are plaintext on disk, readable by the Bash tool with no extra privilege. The sandboxing
   page states that the default filesystem-read policy has no credential deny list [S10,
   "Filesystem isolation"]. For tegh: relocating those secrets to a gateway process that the
   agent's own tools cannot read is the purpose of the wrap (§2).
5. **`--mcp-config`/`--strict-mcp-config`** are a non-destructive, session-scoped alternative to
   rewriting persisted files. An organisation can block them (`disableSideloadFlags`), and they do
   not help a user who launches `claude` directly and not through a tegh wrapper [S8; S4].

## 1. MCP client support

### Config locations and scopes

| Scope | Loads in | Shared | Stored in |
|---|---|---|---|
| Local (default) | current project only | no | `~/.claude.json`, under that project's path |
| Project | current project only | yes, via `.mcp.json` in project root, git-committable | `.mcp.json` |
| User | all projects | no | `~/.claude.json` |
| Plugin-provided | wherever the plugin is enabled | yes, bundled | `.mcp.json` at plugin root or inline in `plugin.json` |
| claude.ai connector | wherever the account is signed in | org-admin managed | cloud-side, not a local file |
| Managed (enterprise) | every session on the managed device | yes, IT-deployed | `managed-mcp.json` at a fixed OS path |

[S1, "MCP installation scopes"]

**Precedence when the same server name collides.** The entire entry wins, and fields are never
merged across scopes: (1) Local, (2) Project, (3) User, (4) Plugin-provided, (5) claude.ai
connector. Plugins and connectors match duplicates by endpoint (URL/command), and do not match by
name [S1, "Scope hierarchy and precedence"].

**Enterprise `managed-mcp.json`.** If deployed, it has exclusive control: no other server, of any
scope or source, loads at all, including plugin servers, unless additionally allowed via
`allowAllClaudeAiMcps` [S1 §"Managed MCP configuration"; S5]. Locations:

| Platform | Path |
|---|---|
| macOS | `/Library/Application Support/ClaudeCode/managed-mcp.json` |
| Linux/WSL | `/etc/claude-code/managed-mcp.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-mcp.json` |

[S5, "Exclusive control with managed-mcp.json"]. Writing this file requires admin/root privileges
[Unsourced: no page is recorded as stating this]. For tegh: it is not a lever that a non-admin
`pip install tegh` flow can assume it has. `claude mcp add` fails hard with `Cannot add MCP server:
enterprise MCP configuration is active and has exclusive control over MCP servers` when this file
is present [S5].

Enterprise also supports non-exclusive allowlist/denylist filtering (`allowedMcpServers` /
`deniedMcpServers`, matched by `serverUrl`, `serverCommand`, or `serverName`) layered on top of
whatever scope defines the server. The denylist always wins, and `allowManagedMcpServersOnly: true`
locks the allowlist to managed sources only [S5, full matching-rule tables].

### Exact JSON shape

```json
{
  "mcpServers": {
    "stdio-example": {
      "command": "/path/to/server",
      "args": ["--flag"],
      "env": { "API_KEY": "${MY_API_KEY}" }
    },
    "http-example": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${API_KEY}" },
      "alwaysLoad": false,
      "timeout": 600000
    },
    "sse-example": { "type": "sse", "url": "https://mcp.example.com/sse" },
    "ws-example": { "type": "ws", "url": "wss://mcp.example.com/socket", "headers": {} }
  }
}
```

No source tag was recorded for this example as a whole. An entry with `url` but no `type` is a
load-time error (treated as a malformed stdio server and skipped) [S1, "Option 1"].
`type: "streamable-http"` is accepted as an alias for `"http"` [S1].

### Transports

| Transport | What is documented |
|---|---|
| stdio | The default when no `type` is given; a local child process. `CLAUDE_PROJECT_DIR` is injected into the spawned process's environment [S1, "Option 3"]. |
| http | Recommended for remote servers; supports OAuth, static `headers`, `headersHelper` [S1, "Option 1"]. |
| sse | Deprecated; the HTTP alternative is preferred [S1, "Option 2"]. |
| ws | Persistent and bidirectional; header-only auth, no OAuth, not reachable via `claude mcp add --transport` (JSON-only) [S1, "Option 4"]. |

Not established: which other environment variables Claude Code passes to a stdio child. The pages
read are recorded here only as stating the `CLAUDE_PROJECT_DIR` injection above.

### Env var expansion

`${VAR}` and `${VAR:-default}` in `.mcp.json` are expanded in `command`, `args`, `env`, `url`, and
`headers` [S1, "Environment variable expansion in .mcp.json"]. An unset variable with no default
still loads (as unexpanded literal text) and is flagged as a warning in `claude mcp list` [S1]. The
source section is about `.mcp.json`. Whether the same expansion applies to entries stored in
`~/.claude.json` is not recorded here.

### OAuth-authenticated remote servers

Discovery uses RFC 9728 Protected Resource Metadata first, with RFC 8414 as the fallback, and can
be overridden via `oauth.authServerMetadataUrl` [S1, "Override OAuth metadata discovery"].
`oauth.scopes` pins the requested scope set [S1, "Restrict OAuth scopes"]. Dynamic Client
Registration and Client ID Metadata Documents (CIMD) are supported, with a fallback to
pre-registered `--client-id`/`--client-secret` plus `--callback-port` when DCR is not supported
[S1, "Use pre-configured OAuth credentials"]. Sign-in is via `/mcp` (interactive) or `claude mcp
login <name>` / `claude mcp logout <name>` (from a shell, v2.1.186+) [S1, "Authenticate from the
command line"]. Non-interactive mode has no OAuth flow: a `claude -p` run against an
unauthenticated OAuth server reports the server's tools as unavailable and does not hang [S1,
"Authenticate with remote MCP servers"]. For tegh: a gateway that runs non-interactively must
pre-authenticate real upstream servers itself.

The documentation states generically that "Authentication tokens are stored securely and refreshed
automatically" for MCP OAuth [S1, Tips under "Authenticate with remote MCP servers"], and does not
name the storage backend for per-MCP-server OAuth tokens on that page. Claude Code's own login
credentials, which are distinct from MCP-server tokens, are documented precisely (§2). The location
of per-MCP-server OAuth token caches is [unverified] against the primary documentation.

### `claude mcp` CLI subcommands

| Command | Purpose |
|---|---|
| `claude mcp add [options] <name> -- <cmd> [args...]` | add a stdio server |
| `claude mcp add --transport http\|sse <name> <url>` | add a remote server |
| `claude mcp add-json <name> '<json>'` | add from a raw JSON config |
| `claude mcp add-from-claude-desktop` | import from Claude Desktop's config (macOS/WSL only) |
| `claude mcp list` | list all configured servers |
| `claude mcp get <name>` | show details / approval / OAuth status for one server |
| `claude mcp remove <name>` | remove a server |
| `claude mcp reset-project-choices` | reset per-project `.mcp.json` approval decisions |
| `claude mcp login <name>` / `claude mcp logout <name>` | run/clear OAuth from the shell |
| `claude mcp serve` | run Claude Code itself as a stdio MCP server |

Flags: `-s`/`--scope {local,project,user}`, `-e`/`--env KEY=value` (repeatable), `-t`/`--transport`,
`-H`/`--header`, `--client-id`, `--client-secret`, `--callback-port`, `--no-browser`
[S1, "Managing your servers" + Tips throughout].

Reserved server names that Claude Code refuses or silently skips: `workspace`, `claude-in-chrome`,
`computer-use`, `Claude Preview`, `Claude Browser` [S1, "Installing MCP servers", final paragraph].

**Approval mechanics for `.mcp.json`.** Project-scoped servers are not connected until a human runs
`claude` interactively and accepts them (`⏸ Pending approval`), unless
`enableAllProjectMcpServers`/`enabledMcpjsonServers` is set in a settings scope that is not itself
gated by workspace trust: user `~/.claude/settings.json`, managed settings, or `--settings` [S1,
"Managing your servers"; workspace-trust detail also in S3 §"Project allow rules and workspace
trust"]. A cloned repository cannot self-approve its own `.mcp.json` servers via a committed
`.claude/settings.json` in an untrusted folder [S1].

## 2. Where MCP credentials live in practice

### Static credentials in MCP configuration

`env` blocks and static `headers` in `.mcp.json` and `~/.claude.json` are plaintext on disk. The
`managed-mcp.json` documentation warns: *"Any user on the machine can read this file, so don't
store API keys or other credentials in `env` blocks"* [S5, "Authenticate with per-user
credentials"]. That warning is about the enterprise file specifically. The underlying fact
(plaintext JSON, protected only by OS file permissions) applies equally to `.mcp.json` and
`~/.claude.json` [Inferred from the file formats in §1].

### Claude Code's own login credentials

These are distinct from per-MCP-server credentials. They are stored in the encrypted Keychain on
macOS, in `~/.claude/.credentials.json` with mode `0600` on Linux, and in
`%USERPROFILE%\.claude\.credentials.json` inheriting user-profile ACLs on Windows. The location is
relocatable via `CLAUDE_CONFIG_DIR` [S7, "Credential management"].

### Whether the agent's own tools can read them

By default the Bash tool can read these files without restriction. Sandboxing, which is off by
default, is the only mechanism that can wall this off, and even then: *"Default read behavior: read
access to the entire computer, except certain denied directories... this default still allows
reading credential files such as `~/.aws/credentials` and `~/.ssh/`"* and *"There is no built-in
credential deny list, so only the files and variables you list are restricted"* [S10, "Filesystem
isolation" and "Protect credentials"]. Nothing in the default, unsandboxed configuration prevents a
Bash tool call from running `cat` on `~/.claude.json` or `.mcp.json` and reading every static
secret in them [Inferred from the S10 statements above; not observed against a running binary].
For tegh: this is the exposure that credential relocation removes.

### What opt-in sandboxing can protect

If an operator does enable sandboxing, `sandbox.credentials.files`/`envVars` with `mode: deny` (or
`mask`, which substitutes a per-session sentinel and reinjects the real value only at the proxy for
listed `injectHosts`) can protect specific paths and variables. This is opt-in and per-path, and
`mask` requires `network.tlsTerminate` to work at all [S10, "Protect credentials"]. It is not a
general protection for MCP configuration and is not on by default.

## 3. Hooks API

This section was first written from the documentation read on 2026-07-25. It was re-read against
the documentation on 2026-10-06, when the current release was 2.1.292. Claims that carry a
2026-10-06 tag were checked on that date. Claims from 2026-07-25 that the re-reading changed are
marked **Changed by 2026-10-06**. Observed behaviour is in "Coverage and limits".

### Events (partial list; the source table lists 33 on 2026-10-06)

`SessionStart`, `UserPromptSubmit`, `PreToolUse` (before any tool call; can block), `PostToolUse`,
`PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `Notification`,
`SubagentStart`/`SubagentStop`, `Stop`, `StopFailure`, `ConfigChange` (fires when a config file
changes mid-session), `PreCompact`/`PostCompact`, `Elicitation`/`ElicitationResult` (MCP
elicitation), `SessionEnd`, and others [S2, full table]. For tegh: `ConfigChange` is a candidate
for detecting drift. Exit 2 from a `ConfigChange` hook blocks the change from taking effect,
except for `policy_settings` [S2, "Exit code 2 behavior per event", 2026-10-06].

`PostToolUse` fires "after a tool call succeeds" and `PostToolUseFailure` "after a tool call
fails" [S2, "Hook lifecycle", 2026-10-06]. `PostToolUseFailure` does not fire for a call rejected
before it ran: an unknown tool name, input that fails validation, or a permission denial. A
permission denial fires `PreToolUse` but not `PostToolUseFailure` [S2, "PostToolUseFailure",
2026-10-06].

### When the tool-call hooks fire

`PreToolUse` and `PostToolUse` fire "on every tool call inside the agentic loop", except
`EndConversation` calls, which skip both [S2, "Hook lifecycle", 2026-10-06]. "PreToolUse hooks run
before every tool call, whether or not it needs permission" [S2, "PermissionRequest input",
2026-10-06]. `PreToolUse` hooks "fire before any permission-mode check, in every permission mode,
including `dontAsk`" [S12, "Hooks and permission modes", 2026-10-06]. The pages do not name reads
that Claude Code auto-allows inside the working directory as a case. That such reads also fire the
hooks is [Inferred] from "whether or not it needs permission" together with the permission table,
under which file reads inside the working directory need no approval [S3, "Permission system",
2026-10-06]. It was also observed on 2026-10-06 ("Coverage and limits").

Three gaps in that coverage are documented:

1. **`@` references fire no hook.** Files referenced with `@` in a prompt are inserted while the
   prompt is built, with no tool call, "so no PreToolUse hook fires for them, including hooks
   matching `Read`". The page directs a reader who needs to block such paths to a `Read` deny rule
   [S2, "PreToolUse", 2026-10-06].
2. **Searches arrive as `Bash`.** On macOS, Linux and WSL, Glob and Grep are absent from the default
   tool set. Claude searches with `find` and `grep` through the Bash tool, which run embedded
   versions of `bfs` and `ugrep`, "and the searches reach your hooks and permission rules as `Bash`
   calls". On Windows, Glob is in the default set [S15, "Glob tool behavior", 2026-10-06].
3. **A file-tool hook does not see a shell write.** Claude Code "doesn't run a `PostToolUse` hook
   matching `Edit|Write` when a `Bash` command or a process outside Claude Code rewrites the same
   file". `FileChanged` is the event for that, and it has no decision control [S2, "PostToolUse",
   2026-10-06].

Hooks from settings files, managed settings and plugins also run inside subagents, and the input
then carries `agent_id` and `agent_type` [S2, "Hook locations", 2026-10-06].

### Hook input

Every event receives `session_id`, `transcript_path`, `cwd` and `hook_event_name`.
`permission_mode` is one of `"default"`, `"plan"`, `"acceptEdits"`, `"auto"`, `"dontAsk"` or
`"bypassPermissions"`, and not every event receives it. The mode labelled Manual arrives as
`"default"`. The common fields also include `prompt_id`, `scratchpad_dir` (v2.1.257 or later) and
`effort`, each present only in some circumstances [S2, "Common input fields", 2026-10-06].
`agent_id` is present only inside a subagent call; `agent_type` is present inside a subagent or
when the session runs with `--agent` [S2, "Common input fields", 2026-10-06].

`PreToolUse` adds `tool_name`, `tool_input` and `tool_use_id`. For an MCP tool it adds
`mcp_server`, an object with the server's `name` and a `source` naming where the definition came
from (`plugin`, `sdk`, or a scope such as `user` or `project`). The page advises basing trust on
`source` and not on the name or the `mcp__<server>__` prefix. `mcp_server` requires v2.1.274 or
later [S2, "PreToolUse input", 2026-10-06].

For `Write`, `Edit` and `Read`, `tool_input.file_path` "is always absolute": `~` and relative
paths are expanded before hooks run. On Windows the path carries backslash separators [S2,
"PreToolUse input", 2026-10-06].

`PostToolUse` receives `tool_input` and `tool_response`, plus an optional `duration_ms` that
excludes time spent in permission prompts and `PreToolUse` hooks. File-tool paths arrive in the
same absolute form, and an MCP tool carries `mcp_server` [S2, "PostToolUse input", 2026-10-06].
"The exact schema for both depends on the tool." The `tool_response` shape is documented for:

| Tool | Documented `tool_response` |
|---|---|
| `Write` | example `{"filePath": ..., "type": "create"}` [S2, "PostToolUse input", 2026-10-06] |
| `Bash` | `stdout`, `stderr`, `interrupted`, `isImage` [S2, "PostToolUse decision control", 2026-10-06]; `bashEditDiff`, best effort and in public beta, v2.1.269 or later [S2, "Bash", 2026-10-06] |
| `Agent` | `status`, `agentId`, `content`, `resolvedModel`, token and duration fields; a background launch carries fewer [S2, "Agent", 2026-10-06] |
| `ExitPlanMode` | `plan` and `filePath`, plus internal status flags [S2, "ExitPlanMode", 2026-10-06] |

No `PostToolUse` `tool_response` shape is documented for `Read`. The `PostToolBatch` section says
that its own `tool_response` for `Read` is the line-number-prefixed text the model receives, and
that this differs from what `PostToolUse` passes, which is "the tool's structured `Output` object"
[S2, "PostToolBatch input", 2026-10-06]. For tegh: a `PostToolUse` consumer that reads a `Read`
result is relying on an undocumented shape.

### Matcher syntax

Simple alphanumeric/`_`/`-`/space/`,`/`|` strings match exactly (`Bash`, `Edit|Write`). Anything
else is treated as an unanchored JS regex (`^Notebook`, `mcp__memory__.*`) [S2]. MCP tools are
addressable as a wildcard over all of a server's tools (`mcp__memory__.*`) or as one tool
(`mcp__memory__create_entities`). Plugin-bundled servers use
`mcp__plugin_<plugin-name>_<server-name>__<tool-name>`, so a matcher written against the bare
server key never fires for a plugin server [S1, "Plugin MCP tool names"; S2].

**Changed by 2026-10-06.** The 2026-07-25 text also listed a bare server name (`mcp__memory`) as a
matcher. The current page says the `.*` is required: `mcp__memory` contains only exact-match
characters, "so it is compared as an exact string and matches no tool" [S2, "Match MCP tools",
2026-10-06]. The bare form remains valid in permission rules (§4), which are a different syntax.

### Can a hook block a tool call?

Yes. This is enforcement and goes beyond advice, with two mechanisms:

1. **Exit code 2** from a `PreToolUse` hook blocks the call outright.
2. **JSON on stdout** with exit 0: `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
   "permissionDecision":"deny","permissionDecisionReason":"..."}}`, or a top-level
   `{"decision":"block","reason":"..."}` for events like `UserPromptSubmit`/`PostToolUse`. A
   `PreToolUse` hook can also rewrite the call in flight via `updatedInput` [S2].

Blocking precedence: a blocking hook (exit 2) takes precedence over allow rules, because it stops
the call before permission rules are evaluated. Hook decisions never override deny/ask permission
rules: a matching `deny` rule blocks regardless of what the hook returned, and a matching `ask`
rule still prompts even if the hook said `"allow"` [S3, "Extend permissions with hooks"].

Re-read on 2026-10-06, these additions:

- `permissionDecision` takes `allow`, `deny`, `ask` or `defer`. When several `PreToolUse` hooks
  disagree, precedence is `deny` > `defer` > `ask` > `allow`. Exit 2 routes the same way as
  `"deny"`. `"defer"` is honoured only in `-p` mode [S2, "PreToolUse decision control" and "Defer
  a tool call for later", 2026-10-06].
- Exit 2 blocks "whether or not you print JSON: even a JSON `permissionDecision` of `"allow"`
  can't override it" [S2, "Exit code 2", 2026-10-06]. A hook that exits 1 but prints a JSON object
  that passes validation has that JSON honoured, `permissionDecision` included [S2, "Other exit
  codes", 2026-10-06].
- A `"deny"` blocks "even in `bypassPermissions` mode or with `--dangerously-skip-permissions`"
  [S12, "Hooks and permission modes", 2026-10-06].
- A hook's `"allow"` does not approve the actions no mode auto-approves (§4), and does not skip the
  prompt for `AskUserQuestion`, `ExitPlanMode`, or MCP tools marked `requiresUserInteraction` [S2,
  "PreToolUse decision control", 2026-10-06].
- **A mod can override a hook's block.** "A mod you install that handles `tool.check` can approve a
  call that your `PreToolUse` hook blocked, unless the hook is in managed settings" [S12, "Hooks
  and permission modes", 2026-10-06; S3, "Extend permissions with hooks", 2026-10-06]. For tegh: a
  `PreToolUse` deny placed in user or project settings is not final against a user-installed mod.

### What a `PostToolUse` hook can and cannot do

By the time it runs, the tool has already run. Its event-specific output fields are
[S2, "PostToolUse decision control", 2026-10-06]:

| Field | Effect |
|---|---|
| `decision: "block"` + `reason` | adds the reason next to the tool result; "Claude still sees the original output" |
| `additionalContext` | text added to Claude's context beside the result |
| `updatedToolOutput` | replaces what Claude sees; must match the tool's output shape, and for a built-in tool a mismatched value is ignored |
| `updatedMCPToolOutput` | the MCP-only predecessor of `updatedToolOutput` |
| `classifierContext` | a note for the auto-mode classifier (v2.1.236 or later) |

`updatedToolOutput` "only changes what Claude sees": files written, commands run and network
requests sent "have already taken effect", and OpenTelemetry spans capture the original output
before the hook runs [S2, "PostToolUse decision control", 2026-10-06]. The universal `continue:
false` stops Claude entirely, and for `PreToolUse` and `PostToolUse` the stop applies even when the
call completes while Claude is still streaming [S2, "JSON output", 2026-10-06]. Exit 2 from
`PostToolUse` shows stderr to Claude; "the tool already ran" [S2, "Exit code 2 behavior per event",
2026-10-06]. `PostToolUse` hooks "can't undo actions" [S12, "Limitations", 2026-10-06].

`PostToolUseFailure` has one event-specific output field, `additionalContext` [S2,
"PostToolUseFailure decision control", 2026-10-06]. The universal fields apply to it as to every
event. The page's own summary table lists it among the events using a top-level `decision:
"block"` [S2, "Decision control", 2026-10-06], which disagrees with its per-event section; neither
statement gives `decision` an effect beyond feedback.

### Failure semantics

The documented rule for command hooks [S2, "Exit code output", "Other exit codes" and "Timeouts",
2026-10-06]:

| Hook outcome | Effect on a `PreToolUse` call |
|---|---|
| exit 2 | blocks; stderr (or the JSON reason) becomes the denial reason |
| exit 0, stdout a JSON object that passes validation | the JSON decides |
| exit 0, empty stdout | no decision; normal permission flow |
| exit 0, JSON that fails to parse or fails validation | non-blocking error; the call proceeds |
| any other exit (1 included) without valid JSON | non-blocking error; the call proceeds |
| plain-text or empty stdout on a non-zero, non-2 exit | non-blocking error; the call proceeds |
| script missing or not executable (shell exits 127) | non-blocking error; "the action proceeds" |
| timeout | the hook is cancelled and its output discarded; a timed-out `command`, `http` or `mcp_tool` hook "doesn't block the tool call" |

The page's warning on the missing-script case: "a mistyped path in `settings.json` leaves the gate
silently disabled" [S2, "Other exit codes", 2026-10-06]. An Agent SDK callback hook is the
exception: one that exceeds its timeout on `PreToolUse` blocks the call [S2, "Timeouts",
2026-10-06].

HTTP hooks use status and body instead of exit code and stdout [S2, "HTTP response handling",
2026-10-06]:

| HTTP outcome | Effect |
|---|---|
| 2xx, empty body | success, equivalent to exit 0 with no output |
| 2xx, JSON object body | parsed with the same output schema; a body that fails validation is a non-blocking error |
| 2xx, any other body (plain text) | non-blocking error, handled as a non-2xx |
| non-2xx status | non-blocking error, execution continues |
| connection failure | non-blocking error, execution continues |
| timeout | the hook is cancelled, as for command hooks |

"HTTP hooks can't signal a blocking error through status codes alone." To block, the endpoint
returns a 2xx with a JSON decision [S2, "HTTP response handling", 2026-10-06].

Default timeouts: 600 seconds for `command`, `http` and `mcp_tool`; 30 for `prompt`; 60 for
`agent`. The first three drop to 30 seconds on `UserPromptSubmit`, `PreModelSwitch` and
`PostModelSwitch`, and to 10 on `MessageDisplay` [S2, "Common fields", 2026-10-06; S12,
"Limitations", 2026-10-06].

No setting was found, in the pages read on 2026-10-06, that makes a settings-file hook's failure
block the call [Inferred from absence: the pages' fail-closed settings concern managed-settings
delivery (`forceRemoteSettingsRefresh`) and malformed managed keys, not hooks; S13, "Keys that fail
closed"; S17, "Enforce fail-closed startup"]. The page directs a reader who needs a hard allow or
deny to the permission system, because the `if` filter is best effort [S2, "How `if` patterns
match Bash commands", 2026-10-06].

**Changed by 2026-10-12.** The finding above is superseded: a setting now makes a hook's
failure block the call, and the hooks page documents it in a section titled "Block the action when
a hook fails" [S2, 2026-10-12]. `onFailure` is a field on the hook handler object, inside a matcher
group's `hooks` array, for `command` and `http` hooks, as in `{"type": "command", "command":
"<cmd>", "timeout": 10, "onFailure": "block"}`. The field tables give its meaning as "What happens
to the action when the hook fails: `"continue"`, the default, or `"block"`", and the page says
"Requires Claude Code v2.1.295 or later". The section reads: "To block the action instead, set
`"onFailure": "block"` on a `command` or `http` hook. The default value is `"continue"`." [S2,
"Block the action when a hook fails", 2026-10-12]. The Timeouts section now adds, after the
sentence quoted in the table above: "The call continues through the normal permission flow, so
don't count on a stalled hook to act as a gate. To block the call when a `command` or `http` hook
times out, set `onFailure: "block"`." [S2, "Timeouts", 2026-10-12]. The exit-code section defines
the failure the default lets through: "Non-blocking error: your hook exits with any other code, or
fails in some other way, such as not starting or printing invalid JSON. The action goes ahead, and
on events such as `PreToolUse` you see a `<hook name> hook error` notice in the transcript." [S2,
2026-10-12]. On 2026-10-09 the key was named by the 2.1.295 changelog entry and by neither the hooks
page nor the settings reference [S16, 2.1.295, 2026-10-09]; the hooks page documented it by
2026-10-12. The observations below are the evidence that it behaves as documented on a running
binary. Claude Code printed no unknown-key warning, the hooks still loaded, and the debug log named
the key when it acted (`PreToolUse http hook failed; blocking (onFailure: "block")`) [observed,
2026-10-09].

Observed on 2026-10-09, headless (`claude -p`), haiku, macOS, one trial per cell, with the hook
under test on `PreToolUse` only and an allow rule for the probe command, so that the permission
system alone would have let the call run:

| Hook failure | Without `onFailure` | With `onFailure: "block"` |
|---|---|---|
| HTTP, connection refused | ran | refused |
| HTTP, hangs past its `timeout` | ran | refused |
| HTTP 500 | ran | refused |
| HTTP 200, body not JSON | ran | refused |
| command, exits 1 | ran | refused |
| command, script path missing (`sh` exits 127) | ran | refused |
| command, `python3` given a missing script | refused | refused |
| command, runs past its `timeout` | ran | refused |

The `python3` row refuses without the key by accident: `python3` exits 2 when it cannot open its
file, and exit 2 blocks (the first table above). The same missing script run through `sh` exits 127
and the call proceeds. In every "ran" cell Claude Code printed nothing about the failed hook on
stderr; the failure showed only in the debug log [observed, 2026-10-09].

With no allow rule anywhere and the decider down, the key on the decider's `PreToolUse` hook also
refused a non-read-only shell command and an in-project Read in `auto`, `default` and
`bypassPermissions` [observed, 2026-10-09, one trial each]. The managed-settings matrix repeated
the down and hang cases on a managed hook (c08a to c08d in §4).

The key acts only on a hook that runs and fails. An HTTP hook that a lower-scope
`allowedHttpHookUrls` rejects counts as failed, so the call is refused. A hook switched off by
`disableAllHooks` in the local settings file never runs, so it cannot fail, and the call proceeds
with the key present [observed, 2026-10-09]. The same holds for a hook `--bare` skips [Inferred:
`--bare` was observed skipping a project hook on 2026-10-09, without the key on it]. Not tested:
the key on any event but `PreToolUse`, and at matcher-group or top level. The page documents it
for `command` and `http` hooks only, with `"continue"` as the only other value.

**Changed in 2.1.288.** "Fixed PreToolUse and PermissionRequest hooks being skipped when matching
them failed or the tool's input could not be serialized to JSON; the call is now blocked" [S16,
2.1.288, 2026-10-06]. That entry covers a hook the harness skipped. It does not change the table
above for a hook that ran and failed.

### HTTP hooks

`type: "http"` posts "the hook's JSON input as the POST request body with `Content-Type:
application/json`", the same JSON a command hook receives on stdin [S2, "HTTP hook fields",
2026-10-06; S12, "HTTP hooks", 2026-10-06]. Fields [S2, "HTTP hook fields" and "Common fields",
2026-10-06]:

| Field | Meaning |
|---|---|
| `url` | required; the POST target |
| `headers` | extra headers; values interpolate `$VAR` / `${VAR}` |
| `allowedEnvVars` | the variables that may be interpolated; an unlisted reference becomes an empty string |
| `timeout` | seconds, default 600 |
| `if` | one permission-rule pattern; evaluated only on tool events |

Two settings bound HTTP hooks from every source, managed included [S2, "Hook locations",
2026-10-06; S14, "Hooks and automation", 2026-10-06]:

- `allowedHttpHookUrls`: when defined at any level, an HTTP hook runs only if its URL matches the
  merged allowlist; an empty array blocks every HTTP hook. Arrays merge across settings files.
- `httpHookAllowedEnvVars`: a variable is interpolated only if both the hook's own
  `allowedEnvVars` and this list name it.

Output caps: a hook's `additionalContext`, `systemMessage` and `initialUserMessage` strings, and
its plain stdout, are capped at 10,000 characters each. Over the cap, Claude Code saves the text to
a file and passes the path plus a preview of up to 2,000 characters; no setting raises the cap [S2,
"JSON output", 2026-10-06].

### Configuration locations

`~/.claude/settings.json` (user, all projects), `.claude/settings.json` (project, committable),
`.claude/settings.local.json` (project, gitignored), managed policy settings (org-wide, not
editable by user or project), plugin `hooks/hooks.json`, and skill/agent frontmatter [S2,
"Hook Configuration Locations"].

Re-read on 2026-10-06: hook entries "merge across settings levels rather than replacing each
other", and the same handler defined in more than one settings file runs once [S2, "Hook
locations" and "Hook handler fields", 2026-10-06]. In an interactive session, hooks from every
settings file wait for the workspace trust dialog. In a `-p` or SDK session Claude Code "treats the
folder as trusted, so hooks committed in a repository's `.claude/settings.json` run in a folder
you've never trusted" [S2, "Workspace trust", 2026-10-06].

### Apply to built-in tools too?

Yes. Hooks fire for Bash, Read, Write, Edit, WebFetch, and MCP tools alike. The only carve-out is
`EndConversation`, which a bare-name deny/ask rule (hook-adjacent permission machinery) can never
remove while any other tool remains [S2, "Application to Built-in Tools"; S3, "Manage permissions"].
On 2026-10-06 the hooks page also states that `EndConversation` skips `PreToolUse` and
`PostToolUse` entirely [S2, "Hook lifecycle", 2026-10-06]. The documented gaps are listed under
"When the tool-call hooks fire" above.

### Security considerations

Hooks execute arbitrary shell commands with the full privileges of the Claude Code process, with no
sandboxing, and parse untrusted JSON from stdin [S2, "Security Considerations"]. Enterprise can
lock this down with `allowManagedHooksOnly` (blocks user/project/plugin hooks except force-enabled
plugins) [S2; S3, "Managed-only settings"].

Re-read on 2026-10-06. Under `allowManagedHooksOnly: true`, managed hooks, Agent SDK in-process
hooks and hooks from plugins force-enabled in managed `enabledPlugins` (matched on the full
`plugin@marketplace` ID) run. User, project and local hooks, hooks and mods from other plugins, and
hooks in agent frontmatter are blocked. Mods built into Claude Code keep running [S14, "What runs
under `allowManagedHooksOnly`", 2026-10-06]. `disableAllHooks` set anywhere but managed settings
disables user, project, local and plugin hooks, and leaves managed hooks, SDK hooks and
force-enabled plugin hooks running. "Only managed settings can disable managed hooks" [S14,
"`disableAllHooks`", 2026-10-06; S2, "Disable or remove hooks", 2026-10-06].

### Version and changelog, 2.1.280 to 2.1.292

The current release on 2026-10-06 was 2.1.292, dated October 6, 2026 [S16, 2026-10-06]. Entries in
that range that bear on settings-file hooks, permission checks and managed settings, quoted from
[S16, 2026-10-06]. The 2.1.295 row was added on 2026-10-12, quoted from the page as fetched that
day, when the newest release it listed was 2.1.296, dated October 9, 2026 [S16, 2026-10-12]:

| Version | Entry |
|---|---|
| 2.1.295 | "Added `onFailure: "block"` for command and HTTP hooks: a hook that can't start, times out, or exits with an unexpected code blocks the action instead of letting it through" [S16, 2.1.295, 2026-10-12]. This one entry is the only one taken from past 2.1.292. |
| 2.1.292 | "Security: Fixed PreToolUse hook approvals and auto mode bypassing the permission prompt for file reads from network (UNC) paths" |
| 2.1.292 | "Improved hook output handling: `<system-reminder>` tags written in a hook's output are escaped before they reach Claude" |
| 2.1.292 | "Fixed a tampered on-disk cache of server-managed settings being able to switch off or unseat the built-in policy plugin while the settings fetch failed" |
| 2.1.290 | "Fixed some permission rules and safety checks not being applied to a tool call after a PreToolUse hook rewrote its input" |
| 2.1.290 | "Fixed Bash permission checks auto-approving some read-only commands (such as `rg` or `git grep`) whose arguments the shell would still expand as wildcards; these now prompt for approval" |
| 2.1.288 | "Fixed PreToolUse and PermissionRequest hooks being skipped when matching them failed or the tool's input could not be serialized to JSON; the call is now blocked" |
| 2.1.287 | "Changed whole-tool `Bash` allow rules and allowing hooks to prompt for, not run, shell writes to files Claude Code's file tools refuse outright (the Anthropic profile store, the host credentials file)" |
| 2.1.285 | "Fixed synchronous hooks hanging Claude Code while a background process the hook started (for example `some-daemon &`) kept its output open; the hook now finishes shortly after its own process exits" |
| 2.1.284 | "Fixed the debug log dropping a failed hook's stderr when the hook also wrote to stdout, and logging nothing for a failed hook with no output; failed hooks now also log their status code" |
| 2.1.282 | "Fixed managed settings ignoring a mistyped value for boolean lock keys such as `disableClaudeAiConnectors` or `allowManagedPermissionRulesOnly`; the lock now applies and startup names the key" |
| 2.1.281 | "Fixed `--setting-sources` (and SDK `settingSources`) not being forwarded to spawned sessions: teammates, `/bg`, `claude agents` sessions and `--worktree --tmux` now start with the parent's restriction" |
| 2.1.281 | "Fixed `mcp_tool` hooks on blocking events (PreToolUse and similar) being skipped while their MCP server was still connecting; they now wait for it, up to the MCP connect timeout" |
| 2.1.281 | "Fixed `claude --bg` starting a background session, and running its project hooks, in a directory that had not passed the workspace trust prompt" |
| 2.1.280 | "Changed `PermissionRequest` hooks: an agent-type hook no longer runs there, since its answer could never allow or deny the request" |

Entries about mod hooks (the plugin `tool.check`/`tool.call` interface) and `/hooks` display
changes in the same range are omitted.

## 4. Permission system

Re-read on 2026-10-06 under the same convention as §3.

### Rules

`permissions.allow`/`ask`/`deny` arrays live in any settings file and merge across scopes (unlike
most settings, which override) [S3, "Manage permissions"]. Evaluation order is deny, then ask, then
allow; the first match wins regardless of specificity, so a broad deny like `Bash(aws *)` beats a
narrower allow like `Bash(aws s3 ls)` [S3, "Manage permissions"]. A bare tool name (`Bash`) as a
deny rule removes the tool from Claude's context entirely; a scoped rule (`Bash(rm *)`) leaves the
tool present and blocks matching calls [S3]. All three statements were confirmed on 2026-10-06
[S3, "Manage permissions", 2026-10-06].

MCP-specific rule syntax: `mcp__<server>` (any tool from that server), `mcp__<server>__*` (explicit
wildcard, same effect), `mcp__<server>__<tool>` (one tool); a bare `mcp__*` denies every MCP tool
[S3, "MCP"; "Tool name wildcards"].

### Permission modes

| Mode | Behavior |
|---|---|
| `default` (aka `manual`, v2.1.200+) | prompts on first use of each tool |
| `acceptEdits` | auto-accepts file edits + common filesystem commands (`mkdir`, `touch`, `mv`, `cp`) in working dir/`additionalDirectories` |
| `plan` | read-only exploration; no edits |
| `auto` | classifier-approved auto-run with background safety checks |
| `dontAsk` | auto-denies unless pre-approved via `/permissions`/`permissions.allow` |
| `bypassPermissions` | skips prompts entirely, **including protected-path writes** (see §5); still prompts for explicit `ask` rules, org-flagged connector tools, and `requiresUserInteraction`-annotated MCP tools; `rm -rf /`/`~` still prompts as a circuit breaker |

[S3, "Permission modes" table; S11]

The 2026-10-06 reading states each mode by what runs without a prompt [S11, "Available modes",
2026-10-06]:

| Mode | What runs without asking |
|---|---|
| `default` | reads only |
| `acceptEdits` | reads, file edits, and common filesystem commands; the mode's own section lists `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp` and `sed`, for paths inside the working directory or `additionalDirectories` [S11, "Auto-approve file edits with acceptEdits mode", 2026-10-06] |
| `plan` | reads, plus classifier-approved commands when auto mode is available |
| `auto` | everything, with background safety checks |
| `dontAsk` | reads and pre-approved tools; "anything that would prompt is denied" |
| `bypassPermissions` | everything, except the actions no mode auto-approves (below) |

`dontAsk` still runs actions that need no approval in Manual mode, "such as file reads inside your
working directories and read-only Bash commands", plus calls matching `permissions.allow` and calls
approved by a `PreToolUse` hook [S11, "Allow only pre-approved tools with dontAsk mode",
2026-10-06].

**Changed by 2026-10-06.** With v2.1.283 or later, auto mode is the built-in starting permission
mode for interactive terminal and VS Code sessions [S11, "Choose a permission mode", 2026-10-06]. A
session that sets no mode therefore starts in `auto`, and not in `default`. The 2026-07-25
`acceptEdits` row named four commands; the current mode section names seven, `rm` and `rmdir`
among them.

### Read-only Bash commands

"Claude Code recognizes a built-in set of Bash commands as read-only and runs them without a
permission prompt in every mode", subject to `permissions.blockReadsOutsideWorkingDirectories`.
The set includes `ls`, `cat`, `echo`, `pwd`, `head`, `tail`, `grep`, `find`, `wc`, `which`,
`diff`, `stat`, `du`, `cd`, and read-only forms of `git`. "The set is not configurable"; an `ask`
or `deny` rule is the way to make one of them prompt [S3, "Read-only commands", 2026-10-06]. In
Manual mode some of them still prompt: an unquoted glob with a write-capable command, `docker`
pointed at another daemon, a network path on Windows, writes to special shell variables, and any
command the analysis cannot parse, including every command over 10,000 characters [S3, "Read-only
commands", 2026-10-06]. For tegh: `cat` on a file inside the project is an approval-free call in
every mode, so the permission layer alone records nothing about it.

### Actions no mode auto-approves

No mode, `bypassPermissions` included, auto-approves [S11, "Actions no mode auto-approves",
2026-10-06]:

- tools matched by an explicit `ask` rule;
- connector tools the organization set to `ask`;
- tools that require user interaction: `AskUserQuestion` and MCP tools marked
  `requiresUserInteraction`;
- `rm` and `rmdir` removals targeting a critical path, "which no allow rule or `PreToolUse` hook
  `"allow"` approves";
- the cross-session messaging safeguards;
- reads outside the working directories while `permissions.blockReadsOutsideWorkingDirectories`
  is on (v2.1.257 or later).

In `dontAsk` these are denied rather than prompted [S11, "Allow only pre-approved tools with
dontAsk mode", 2026-10-06].

### Managed/enterprise policy a user cannot override

Delivery mechanisms: server-managed settings (fetched at sign-in), MDM/OS-level policy
(macOS `com.anthropic.claudecode` preferences domain; Windows `HKLM\SOFTWARE\Policies\ClaudeCode`),
or a file at a fixed OS path (`managed-settings.json`, with a `managed-settings.d/` drop-in dir)
[S4, "Settings File Locations"]. Precedence is managed > CLI args > local project settings > shared
project settings > user settings, and permission rules merge across these where other settings
override [S3, "Settings precedence"]. Managed-only locks relevant to tegh:

| Setting | Effect |
|---|---|
| `allowManagedMcpServersOnly` / `allowedMcpServers` / `deniedMcpServers` | server allowlist/denylist [S3; S5] |
| `allowManagedPermissionRulesOnly` | locks `allow`/`ask`/`deny` to managed sources only [S3] |
| `allowManagedHooksOnly` | locks hooks to managed/SDK/force-enabled-plugin sources [S3] |
| `disableSideloadFlags` | rejects `--plugin-dir`, `--plugin-url`, `--agents`, `--mcp-config` at startup, which closes the CLI-flag interposition lever in item 5 of the summary [S3, "Managed-only settings"; S4] |
| `strictPluginOnlyCustomization` | can force skills/agents/hooks/MCP to come only from plugins or managed settings, closing off user/project `.mcp.json` entirely [S3] |
| `disableBypassPermissionsMode` | can be set from any scope (including a user locking themselves out) and is "typically placed in managed settings to enforce organizational policy" [S3] |

Re-read on 2026-10-06, with the Windows detail the 2026-07-25 reading left unverified now taken
from the page text:

**File paths.** `managed-settings.json`, an optional `managed-settings.d/` directory and
`managed-mcp.json` live in `/Library/Application Support/ClaudeCode/` on macOS,
`/etc/claude-code/` on Linux and WSL, and `C:\Program Files\ClaudeCode\` on Windows. The legacy
`C:\ProgramData\ClaudeCode\managed-settings.json` is not read [S13, "Where each mechanism stores
the policy", 2026-10-06]. `managed-settings.json` merges first, then every `*.json` in
`managed-settings.d/` in alphabetical order: later single values replace earlier ones and lists
combine [S13, "Split a file-based policy across teams", 2026-10-06].

**Delivery and refresh** [S13, "Choose a delivery mechanism" and "Where each mechanism stores the
policy", 2026-10-06; S17, "Fetch and caching behavior", 2026-10-06]:

| Mechanism | Where it lives | When Claude Code reads it |
|---|---|---|
| Server-managed | claude.ai admin console or a self-hosted gateway; a local cache | fetched at startup, polled hourly |
| MDM / OS policy | macOS `com.anthropic.claudecode` managed preferences domain; Windows `Settings` value (`REG_SZ` or `REG_EXPAND_SZ`) under `HKLM\SOFTWARE\Policies\ClaudeCode` | at startup, checked every 30 minutes |
| File-based | the paths above | at startup, reloaded when a file changes |
| HKCU, Windows and WSL | `Settings` under `HKCU\SOFTWARE\Policies\ClaudeCode` | at startup, every 30 minutes; used only when no admin document sits above it |

By default (`managedSourcesBehavior: "first-wins"`) Claude Code uses the highest-ranked source that
delivers a policy key and ignores the others; `"merge"` (v2.1.242 or later) composes them [S13, "How
Claude Code combines managed sources", 2026-10-06]. Agent SDK sessions load managed settings even
when `settingSources` excludes user, project and local [S13, "Where and when a policy applies",
2026-10-06].

**The hook and permission locks** [S14, 2026-10-06]:

- `allowManagedHooksOnly` (managed only): what still runs and what is blocked is listed in §3,
  "Security considerations".
- `allowManagedPermissionRulesOnly` (managed only): Claude Code "ignores `allow`, `ask`, and `deny`
  rules in user, project, local, and `--settings` files, ignores `--allowedTools`, hides the
  always-allow choices in permission prompts, and stops saving new rules". `--disallowedTools` and
  the session's own `deny` and `ask` rules still apply, since they only restrict. From v2.1.282 it
  also ignores `allowed-tools` frontmatter in skills and commands from the repository, the user's
  directories and `--add-dir` [S14, "`allowManagedPermissionRulesOnly`", 2026-10-06].
- `permissions.disableBypassPermissionsMode: "disable"` (any file): Claude Code rejects
  `--dangerously-skip-permissions` and ignores an agent definition's `permissionMode:
  bypassPermissions` [S14, "`permissions.disableBypassPermissionsMode`", 2026-10-06].
  `permissions.disableAutoMode` does the same for `auto` [S3, "Permission modes", 2026-10-06].
- `disableAllHooks` outside managed settings cannot disable managed hooks [S14, "`disableAllHooks`",
  2026-10-06].

**What the managed tier does not bind.** The pages name four limits:

1. A user-installed mod that handles `tool.check` can approve a call a non-managed `PreToolUse`
   hook blocked (§3) [S3, "Extend permissions with hooks", 2026-10-06].
2. "A developer who is an administrator on the machine can edit the managed source itself" [S13,
   "What a developer can change", 2026-10-06].
3. Server-managed settings "operate as a client-side control, not a security boundary. On
   unmanaged devices, a user doesn't need admin or sudo access to bypass them." Editing the cached
   file applies at startup until the next fetch; a modified binary bypasses any client-side
   control; a third-party model provider bypasses server-managed settings [S17, "Security
   considerations", 2026-10-06].
4. "Managed settings bind Claude Code only" [S13, "What a developer can change", 2026-10-06].

**`--setting-sources`.** A comma-separated list of `user`, `project` and `local`, for example
`claude --setting-sources user,project` [S8, "CLI flags", 2026-10-06]. Since 2.1.281 the list is
forwarded to teammates, `/bg`, `claude agents` sessions and `--worktree --tmux` [S16, 2.1.281,
2026-10-06]. The permissions page names `--setting-sources user` as a way to keep a `claude -p` run
from reading a project's settings files and `.mcp.json` [S3, "What runs before you trust a folder",
2026-10-06].


### Managed settings, observed 2026-10-12

A matrix run on a disposable macOS runner on 2026-10-12, with Claude Code 2.1.295 installed by the
official installer, headless, on haiku for every cell but c01g and c01h, which ran on sonnet. One
trial per cell, and the whole matrix was run twice that day. Each cell installed a managed policy
file at the macOS path above and an HTTP `PreToolUse` hook in it, pointed at a decider that answers
the call with an allow (the table calls it the grantor). "Grantor down" means nothing listened at
the hook's URL. No allow rule was present anywhere unless the cell names one. The probe was a shell
pipeline that writes a marker file (`tee ran.txt`), which is not a read-only command. All of the
following is [observed, 2026-10-12], each with the cells it rests on.

**With the decider down and no allow rule**, the call was refused in `default`, in `dontAsk` and
with no mode flag (c01b, c01c, c01f): nothing approved it. In `auto` it ran, on haiku and on sonnet
(c01d, c01g), the two mismatches of both runs. Only a managed `"disableAutoMode": "disable"`, which
the matrix wrote at the top level of the managed file, refused it there (c01e, c01h). The vendor's
permissions page documents the key under `permissions` (`permissions.disableAutoMode`, above); that
placement was not tested. With the decider up, the managed hook's allow was the only approval the
call needed (c01a).

**The mode locks.** Without a lock, `--dangerously-skip-permissions` ran the call (c03a). Under
managed `permissions.disableBypassPermissionsMode: "disable"`, that flag, `--permission-mode
bypassPermissions` and a project `defaultMode` were each ignored and the session started in
`default` (c03b to c03d).

**Allow rules from below.** An allow rule from `--allowedTools`, the local file, the project file,
`--settings` or the user file each let the call run with the decider down, and managed
`allowManagedPermissionRulesOnly` removed each one (the ten c02 cells).

**Hooks from below.** Managed `allowManagedHooksOnly` dropped a project or a user command hook and
kept the managed one (c04a to c04d). `disableAllHooks: true` in the project file, the local file or
`--settings` did not switch off the managed hook (c05a to c05c); in the managed file it did (c05d).
`--bare` and `--safe-mode` each left the managed hook in place: the decider saw the call and the
call ran on its allow (c11a, c11b). The runner's own user could not overwrite the managed policy
file, add a drop-in beside it, or rename it (c09).

**The HTTP hook allowlists.** `allowedHttpHookUrls` arrays merge across scopes (c06a). When the
managed file sets no `allowedHttpHookUrls`, a list in the local file, the project file or
`--settings` that misses the decider's URL blocks the managed HTTP hook (c06b to c06d), and
`allowManagedHooksOnly` does not prevent it (c06e). A local `httpHookAllowedEnvVars` that omits the
token's variable strips the header, and the decider sees an unauthenticated request (c06g; c06f is
its control). In each of those cells the call was refused, because no allow rule and no decider
approved it; what was lost is the decider's sight of the call, and no authority. When the managed
file sets both arrays itself, they merge as a union: a local `allowedHttpHookUrls` naming something
else did not block the managed hook (c06h), and a local `httpHookAllowedEnvVars` naming another
variable did not strip the header (c06i). Both were added for the second run.

**MCP.** A managed `managed-mcp.json` took exclusive control of MCP servers: an empty map removed
the project's server and refused `claude mcp add`, and a map with one server listed only that one
(c07a to c07c).

**The source in force.** Every cell behaved as the managed file said, and the debug log said the
machine had managed settings. `claude doctor` reported no server-managed settings configured for
the organisation the runner signed in to (c10), so no server-managed source was present to outrank
the file, and whether one would is not established.

**The two cells that changed between runs.** c01f (no mode flag, decider down) and c03c
(`--permission-mode bypassPermissions` under the bypass lock, so the session ran in `default`)
matched in the first run and mismatched in the second. In both second-run trials the pipeline was
refused as predicted and no marker was written; the model then ran `echo spike-ok` alone, which
`default` mode permits as a read-only command without asking anyone, and the observer scores any
successful shell result as a run. Neither is a control failing. Both show again what was observed
on 2026-10-09: a read-only shell command runs in `default` mode with no decider and no allow rule
("Behaviour observed since"). What is lost there is the decider's sight of the call, not a write.

**Not established by the matrix.** Server-managed settings, MDM profile delivery,
`managedSourcesBehavior` first-wins against merge, interactive sessions, a determined agent trying
to switch its own hooks off, Linux and Windows, and repeatability beyond two runs.

The verdict table of the second run, transcribed from the run's results. "Expected" is the
matrix's own prediction; `grantor_bash_events` counts the decider's requests for the call, and
`bash_ran` whether the call's result came back without an error.

| Cell | What | Expected | Observed | Verdict |
|---|---|---|---|---|
| c00-environment | environment: Claude Code version, OS, managed directory listing | (no prediction) | managed_version=2.1.295 (Claude Code) | INFO |
| c01a-up-dontAsk | baseline managed hook, grantor up, mode dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c01b-down-default | baseline managed hook, grantor down, mode default | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=default, exit=0 | MATCH |
| c01c-down-dontAsk | baseline managed hook, grantor down, mode dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c01d-down-auto | baseline managed hook, grantor down, mode auto | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=yes, permission_mode=auto, exit=0 | MISMATCH |
| c01e-down-auto-locked | baseline managed hook, grantor down, mode auto, managed disableAutoMode | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=default, exit=0 | MATCH |
| c01f-down-nomode | baseline managed hook, grantor down, mode none | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=yes, permission_mode=default, exit=0 | MISMATCH |
| c01g-down-auto-sonnet | baseline managed hook, grantor down, mode auto, model sonnet | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=yes, permission_mode=auto, exit=0 | MISMATCH |
| c01h-down-auto-locked-sonnet | baseline managed hook, grantor down, mode auto, managed disableAutoMode, model sonnet | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=default, exit=0 | MATCH |
| c02-allowedTools-ctl | Bash allow rule from allowedTools; allowManagedPermissionRulesOnly off; grantor down; dontAsk | bash_ran=yes, grantor_bash_events=0 | bash_ran=yes, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-allowedTools-lock | Bash allow rule from allowedTools; allowManagedPermissionRulesOnly on; grantor down; dontAsk | bash_ran=no, grantor_bash_events=0 | bash_ran=no, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-local-ctl | Bash allow rule from local; allowManagedPermissionRulesOnly off; grantor down; dontAsk | bash_ran=yes, grantor_bash_events=0 | bash_ran=yes, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-local-lock | Bash allow rule from local; allowManagedPermissionRulesOnly on; grantor down; dontAsk | bash_ran=no, grantor_bash_events=0 | bash_ran=no, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-project-ctl | Bash allow rule from project; allowManagedPermissionRulesOnly off; grantor down; dontAsk | bash_ran=yes, grantor_bash_events=0 | bash_ran=yes, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-project-lock | Bash allow rule from project; allowManagedPermissionRulesOnly on; grantor down; dontAsk | bash_ran=no, grantor_bash_events=0 | bash_ran=no, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-settings-flag-ctl | Bash allow rule from settings-flag; allowManagedPermissionRulesOnly off; grantor down; dontAsk | bash_ran=yes, grantor_bash_events=0 | bash_ran=yes, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-settings-flag-lock | Bash allow rule from settings-flag; allowManagedPermissionRulesOnly on; grantor down; dontAsk | bash_ran=no, grantor_bash_events=0 | bash_ran=no, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-user-ctl | Bash allow rule from user; allowManagedPermissionRulesOnly off; grantor down; dontAsk | bash_ran=yes, grantor_bash_events=0 | bash_ran=yes, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c02-user-lock | Bash allow rule from user; allowManagedPermissionRulesOnly on; grantor down; dontAsk | bash_ran=no, grantor_bash_events=0 | bash_ran=no, grantor_bash_events=0, permission_mode=dontAsk, exit=0 | MATCH |
| c03a-ctl-flag | bypass permissions, variant ctl-flag, grantor down | bash_ran=yes | bash_ran=yes, permission_mode=bypassPermissions, exit=0 | MATCH |
| c03b-lock-flag | bypass permissions, variant lock-flag, grantor down | bash_ran=no | bash_ran=no, permission_mode=default, exit=0 | MATCH |
| c03c-lock-mode | bypass permissions, variant lock-mode, grantor down | bash_ran=no | bash_ran=yes, permission_mode=default, exit=0 | MISMATCH |
| c03d-lock-project-default | bypass permissions, variant lock-project-default, grantor down | bash_ran=no | bash_ran=no, permission_mode=default, exit=0 | MATCH |
| c04a-project-ctl | command hook in project settings writes a marker; allowManagedHooksOnly off; grantor up; dontAsk | hook_marker=present, grantor_bash_events=>0, bash_ran=yes | hook_marker=present, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c04b-project-lock | command hook in project settings writes a marker; allowManagedHooksOnly on; grantor up; dontAsk | hook_marker=absent, grantor_bash_events=>0, bash_ran=yes | hook_marker=absent, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c04c-user-ctl | command hook in user settings writes a marker; allowManagedHooksOnly off; grantor up; dontAsk | hook_marker=present, grantor_bash_events=>0, bash_ran=yes | hook_marker=present, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c04d-user-lock | command hook in user settings writes a marker; allowManagedHooksOnly on; grantor up; dontAsk | hook_marker=absent, grantor_bash_events=>0, bash_ran=yes | hook_marker=absent, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c05a-project | disableAllHooks: true in project settings; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c05b-local | disableAllHooks: true in local settings; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c05c-settings-flag | disableAllHooks: true in settings-flag settings; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c05d-managed-ctl | disableAllHooks: true in managed settings; grantor up; dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06a-merge-local | HTTP hook allowlists, variant merge-local; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c06b-unset-local | HTTP hook allowlists, variant unset-local; grantor up; dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06c-unset-project | HTTP hook allowlists, variant unset-project; grantor up; dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06d-unset-settings-flag | HTTP hook allowlists, variant unset-settings-flag; grantor up; dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06e-unset-local-hooksonly | HTTP hook allowlists, variant unset-local-hooksonly; grantor up; dontAsk | grantor_bash_events=0, bash_ran=no | grantor_bash_events=0, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06f-envvars-ctl | HTTP hook allowlists, variant envvars-ctl; grantor up; dontAsk | grantor_unauthorized=0, grantor_bash_events=>0, bash_ran=yes | grantor_unauthorized=0, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c06g-envvars-local | HTTP hook allowlists, variant envvars-local; grantor up; dontAsk | grantor_unauthorized=>0, bash_ran=no | grantor_unauthorized=1, bash_ran=no, permission_mode=dontAsk, exit=0 | MATCH |
| c06h-merge-local-hooksonly | HTTP hook allowlists, variant merge-local-hooksonly; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c06i-envvars-merge | HTTP hook allowlists, variant envvars-merge; grantor up; dontAsk | grantor_unauthorized=0, grantor_bash_events=>0, bash_ran=yes | grantor_unauthorized=0, grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |
| c07a-ctl | managed-mcp.json ctl; project .mcp.json names project-dummy | mcp_project_dummy=listed | mcp_project_dummy=listed, mcp_managed_dummy=absent | MATCH |
| c07b-empty | managed-mcp.json empty; project .mcp.json names project-dummy | mcp_project_dummy=absent, mcp_add=refused | mcp_project_dummy=absent, mcp_add=refused, mcp_managed_dummy=absent | MATCH |
| c07c-one | managed-mcp.json one; project .mcp.json names project-dummy | mcp_project_dummy=absent, mcp_managed_dummy=listed | mcp_project_dummy=absent, mcp_managed_dummy=listed | MATCH |
| c08a-ctl-down | managed allow for the probe command; hook onFailure ctl; grantor down; dontAsk | bash_ran=yes | bash_ran=yes, permission_mode=dontAsk, exit=0, managed_onfailure_mentioned=no | MATCH |
| c08b-block-down | managed allow for the probe command; hook onFailure block; grantor down; dontAsk | bash_ran=no | bash_ran=no, permission_mode=dontAsk, exit=0, managed_onfailure_mentioned=yes | MATCH |
| c08c-ctl-hang | managed allow for the probe command; hook onFailure ctl; grantor hang; dontAsk | bash_ran=yes | bash_ran=yes, permission_mode=dontAsk, exit=0, managed_onfailure_mentioned=no | MATCH |
| c08d-block-hang | managed allow for the probe command; hook onFailure block; grantor hang; dontAsk | bash_ran=no | bash_ran=no, permission_mode=dontAsk, exit=0, managed_onfailure_mentioned=yes | MATCH |
| c09-unprivileged-write | runner user tries to overwrite, add to, and rename the managed policy | write_file=denied, write_dropin=denied, write_rename=denied, write_unchanged=yes | write_file=denied, write_dropin=denied, write_rename=denied, write_unchanged=yes | MATCH |
| c10-status | which managed source Claude Code recognized (/status in -p, debug log, doctor) | (no prediction) | permission_mode=dontAsk, exit=0, managed_source_line=absent | INFO |
| c11a-bare | --bare with the baseline managed hook; grantor up; dontAsk | (no prediction) | permission_mode=dontAsk, exit=0 | INFO |
| c11b-safe-mode | --safe-mode with the baseline managed hook; grantor up; dontAsk | grantor_bash_events=>0, bash_ran=yes | grantor_bash_events=1, bash_ran=yes, permission_mode=dontAsk, exit=0 | MATCH |

### `--dangerously-skip-permissions`

Exact documentation wording: *"Skip permission prompts. Equivalent to `--permission-mode
bypassPermissions`"* [S8]. It is blocked automatically when run as root/sudo on Linux/macOS
(*"root access combined with no permission prompts can modify any file or service on the
system"*), except inside a recognized sandbox [S10, Troubleshooting]. It offers no protection
against prompt injection, per an explicit documentation warning [S11, bypassPermissions warning
block].

## 5. Self-modification surface

Claude Code hard-codes a protected-paths list that is evaluated before permission rules are
consulted, so `permissions.allow` entries (for example `Edit(.claude/**)`) cannot pre-approve these
writes [S11, "Protected paths"]:

| Mode | Protected-path write outcome |
|---|---|
| `default`, `acceptEdits` | always prompted |
| `plan` | prompted (routed to classifier if auto mode is available, v2.1.218+) |
| `auto` | routed to the classifier |
| `dontAsk` | denied |
| `bypassPermissions` | **allowed, no prompt** (since v2.1.126; before that it still prompted) |

Protected directories include `.claude` (except `.claude/worktrees`), `.git`, `.vscode`, `.idea`,
and others. Protected files explicitly include `.mcp.json` and `.claude.json` [S11, "Protected
paths"; the page carries the full lists]. A prompted write offers a session-scoped **"Yes, and
allow Claude to edit its own settings for this session"** opt-in [S11].

**Guardrail conclusion for tegh** [Inferred from S11 and S10; not observed against a running
binary]. Under a default configuration (no `bypassPermissions`, no `dontAsk`), Claude Code cannot
silently self-modify `.mcp.json`/`settings.json`/hooks: a human sees a prompt every time. The
exception is an operator who has explicitly chosen `bypassPermissions` (documented as "no
protection against prompt injection," used advisedly in isolated containers/VMs [S11, S3]) or
`dontAsk` with a broad pre-approval. The table above qualifies "a human sees a prompt every time":
in `auto`, and in `plan` where auto mode is available, the write is routed to the classifier.

**Changed by 2026-10-06.** Three facts above moved [S11, "Protected paths" and "Choose a
permission mode", 2026-10-06]. The `plan` row now reads: allowed in interactive terminal sessions
with bypass permissions available, otherwise routed to the classifier when auto mode is available
during planning, otherwise prompted. The session-scoped opt-in is now worded per folder: "Yes, and
allow Claude to edit files in this project's .claude folder for this session", with a matching
option for `~/.claude/`. And with v2.1.283 or later, `auto` is the built-in starting mode for
interactive terminal and VS Code sessions (§4), so "a default configuration" now starts in the
mode whose protected-path writes go to the classifier, and the guardrail conclusion's "a human sees
a prompt every time" holds only for a session started in `default`. The protected-file list still
names `.mcp.json` and `.claude.json`. The page still does not say which tools the gate covers.

If sandboxing is additionally enabled, there is a second, OS-level backstop: **"the sandbox
automatically denies write access to Claude Code's `settings.json` files at every scope and to the
managed settings directory, so a sandboxed command can't modify its own policy"** unless filesystem
isolation is explicitly disabled [S10, "Settings files protected"]. That protection covers
`settings.json`. The sandbox page does not equivalently name `.mcp.json` in the same bullet, so
treat sandbox-layer protection of `.mcp.json` as [unverified] beyond the protected-paths mechanism
above, which does cover it.

Not established: which tools the protected-path gate covers. The table above names an outcome per
mode and is not recorded here as naming the tools (for example file-edit tools as against a shell
command run through Bash) to which it applies. See the second subsection of §7.

## 6. Plugins / skills / agents / Agent SDK

**Plugins** package skills, hooks, subagents and MCP servers as one installable unit, distributed
via marketplaces [S1, "Plugin-provided MCP servers"]. The plugin pages themselves were not read;
this comes from the plugin sections of the MCP page (see "Coverage and limits"). Plugin-bundled MCP
servers connect automatically on session start and are managed by enabling or disabling the
plugin, and `/mcp` add/remove does not manage them [S1]. Plugins can only add servers: nothing in
the pages read describes a plugin mechanism for suppressing or intercepting a server defined by
another source [Inferred from absence]. For tegh: shipping tegh as a plugin does not by itself
satisfy the interposition requirement. A plugin can add the gateway, and cannot hide the real
servers a user already configured elsewhere.

**Agent SDK** (`claude_agent_sdk` / `@anthropic-ai/claude-agent-sdk`) runs the same agent loop,
tools, and MCP client as the CLI, embedded in a Python/TypeScript process [S9, "Agent SDK
overview"]. Relevant programmatic surfaces:

| Surface | What is documented |
|---|---|
| `ClaudeAgentOptions.mcp_servers` | Same config shape as `.mcp.json`, set in code [S9, "MCP" tab]. |
| `ClaudeAgentOptions.hooks` | Python/TS callback functions (not shell commands) for `PreToolUse`, `PostToolUse` and other events; can block, log, or transform [S9, "Hooks" tab]. |
| `canUseTool` permission callback | Referenced on the MCP page, in the context of `requiresUserInteraction`-annotated tools reaching the SDK's own approval surface where the CLI would prompt [S1, "Require approval for a specific tool"]. Its full semantics are [unverified] (see "Coverage and limits"). |
| `settingSources`/`setting_sources` | Restricts which of `.claude/` and `~/.claude/` load into an SDK session at all [S9, "Claude Code features"]. |

For tegh: the SDK matters mainly as a second interposition surface distinct from the CLI. A product
embedding the SDK directly can pass `mcp_servers={"tegh-gateway": {...}}` and skip file rewriting
entirely, at the cost of covering only that one embedding and none of a user's ambient `claude` CLI
sessions.

## 7. Unwrapping / config-rewrite mechanics

### Whether Claude Code rewrites the configuration itself

The `/mcp` disable toggle writes to two disjoint opt-out/opt-in lists in `~/.claude.json`
(`disabledMcpServers`, `enabledMcpServers`) and leaves the server definitions themselves unchanged
[S1, "Disable a server without removing it"].

The inference drawn from the documentation on 2026-07-25 was this. Claude Code does not appear to
rewrite `mcpServers` entries in `.mcp.json`/`~/.claude.json` on its own initiative outside of
explicit `claude mcp add`/`remove` invocations or that toggle. A snapshot-before-wrap,
restore-on-unwrap strategy over the relevant JSON files should therefore be sufficient and
deterministic, because nothing else races to mutate these files at runtime. [Assessment, not a
documented guarantee: inferred from the absence of any other documented runtime writer.]

**Corrected by observation, 2026-07-27.** The strong reading of that inference is wrong.
`~/.claude.json` is a live state file: alongside MCP config, each project entry carries
`lastSessionId`, `lastCost`, `lastAPIDuration`, `lastTotalInputTokens`, `lastGracefulShutdown` and
more, and the file was observed changing during a session with no MCP config edit. The harness
itself is the other writer, at minimum at session boundaries.

What survives: nothing else appears to mutate the `mcpServers` block, so snapshot-and-restore of
that block is sound [Inferred; no mutation of the block was observed, and no test was run to
provoke one]. What does not survive: snapshot-and-restore of the whole file, which would roll back
every session's telemetry written while a project was wrapped. tegh therefore backs up the block
and never the file (`tegh/interpose.py`).

### The CLI and direct file edits, run from a shell

`claude mcp add`/`add-json`/`remove` and direct file edits are **not gated by Claude Code's own
permission system at all** when run from a shell outside a session. They are gated only by
managed-settings locks (`disableSideloadFlags`, `allowManagedMcpServersOnly`, `managed-mcp.json`
exclusivity). [Unsourced: no documentation page is recorded for this statement.] For tegh: this is
the sanctioned lever. tegh's installer runs these as an ordinary CLI tool, before or outside any
Claude Code session, with no interactive approval step required for the scope changes it makes. The
target project's `.mcp.json` approval flow still applies the next time a human runs `claude` there
(§1).

Not established: whether the wrapped agent can take the same path from inside a session, by
running `claude mcp remove` or editing the file through its Bash tool, and whether the
protected-path gate in §5 applies to that route. Settling it needs a test against a running
installation.

### `claude mcp add` does not check what it is pointed at

The documentation says of `claude mcp add` that it *"saves the configuration without validating
credentials"* [S1, GitHub example]. The quoted words concern credentials. The broader reading, that
`claude mcp add` does not validate the server it is pointing at, is [Inferred] from that sentence.
For tegh: the gateway must be live and MCP-protocol-correct before the rewrite lands, or the user
sees a `failed` or pending server with no further diagnostic than `/mcp`.

### The non-persistent alternative

`--mcp-config <file>` / `--strict-mcp-config` load an explicit server set for one invocation,
optionally excluding every other source [S8; S1 only by implication from S8's flag descriptions].
This is cleaner to unwrap (stop passing the flag). It requires control over how `claude` is
launched (a wrapper binary or alias), and is blockable organisation-wide via `disableSideloadFlags`
[S3, "Managed-only settings"].

## Coverage and limits

### Pages read

All on `code.claude.com`, all on 2026-07-25.

| Page | What was taken from it |
|---|---|
| `/mcp` (full page) | MCP config, scopes, precedence, JSON shape, transports, env expansion, OAuth, CLI subcommands |
| `/hooks` (full page) | hook events, matchers, blocking, config locations, built-in-tool coverage |
| `/permissions` (full page) | permission rules, modes, managed settings, `--dangerously-skip-permissions` context |
| `/settings` (full page) | settings file locations, precedence, available keys |
| `/managed-mcp` (full page) | enterprise MCP allowlists/denylists and `managed-mcp.json` |
| `/security` (full page) | security threat model and credential-storage pointers |
| `/authentication` (full page) | Claude Code's own login-credential storage |
| `/cli-reference` (targeted extraction) | exact flag wording for `--dangerously-skip-permissions`, `--permission-mode`, `--mcp-config`, `--strict-mcp-config`, `--allowedTools`, `--disallowedTools`, `--settings` |
| `/sandboxing` (full page) | sandbox filesystem/network isolation, credential protection, settings-file self-modification protection |
| `/permission-modes` (full page) | protected-paths table |
| `/agent-sdk/overview` (full page) | Agent SDK overview, capabilities tabs, Claude Code feature parity |

On 2026-10-06, for §3 and §4 only, each fetched as the page's Markdown form
(`https://code.claude.com/docs/en/<page>.md`):

| Page | What was taken from it |
|---|---|
| `/hooks` (full page) | lifecycle, input fields, decision control, exit-code and HTTP failure handling, timeouts, HTTP hook fields, output caps, workspace trust |
| `/hooks-guide` (targeted sections) | hooks and permission modes, the mod override, limitations, HTTP hooks |
| `/permissions` (targeted sections) | permission table, rules, modes, read-only commands, hooks, managed settings, workspace trust |
| `/permission-modes` (targeted sections) | modes table, actions no mode auto-approves, `acceptEdits`, `dontAsk`, `bypassPermissions`, protected paths |
| `/managed-settings` (targeted sections) | delivery mechanisms, paths, refresh, `managed-settings.d/`, what a developer can change, managed-only keys |
| `/server-managed-settings` (targeted sections) | fetch cadence, security considerations |
| `/settings-reference` (targeted entries) | `allowedHttpHookUrls`, `allowManagedHooksOnly`, `disableAllHooks`, `httpHookAllowedEnvVars`, `allowManagedPermissionRulesOnly`, `permissions.disableBypassPermissionsMode` |
| `/tools-reference` (targeted sections) | Glob and Grep default availability |
| `/cli-reference` (targeted extraction) | `--setting-sources` |
| `/changelog` (2.1.280 to 2.1.292) | current version and hook-relevant entries |

`/settings` was also fetched on 2026-10-06 and nothing was taken from it.

On 2026-10-09 `/hooks`, `/settings-reference` and `/changelog` were fetched again, to look for
`onFailure`. Only the changelog's 2.1.295 entry named it. On 2026-10-12 `/hooks` and `/changelog`
were fetched again, and the hooks page then documented it (§3, "Failure semantics").

### Pages not read, and what that limits

`/plugins` and `/plugins-reference` were not read. The plugin MCP-server mechanics in §6 come from
the plugin-related sections embedded in the `/mcp` page. That is enough to answer "can a plugin
suppress another server" (no mechanism is described) and is not enough to characterize plugin
marketplace distribution, trust dialogs, or the full `plugins-reference` schema.

`/agent-sdk/permissions` and `/agent-sdk/hooks` were not read. The SDK's `canUseTool` callback is
referenced here only through its mention on the `/mcp` page, so its full signature and semantics
are not verified.

`/server-managed-settings` and `/claude-apps-gateway` were seen only as cross-links inside pages
that were read, and were not opened. `/server-managed-settings` was read on 2026-10-06 [S17];
`/claude-apps-gateway` remains unread. The mods pages (`/plugins/mods/*`) were not read on
2026-10-06, so the mod override in §3 rests on the hooks guide and the permissions page only.

### Not established

The storage backend for per-MCP-server OAuth tokens is [unverified] (§1). Claude Code's own login
credentials are fully sourced (§2). The only lead found for the MCP-server tokens was a third-party
issue report, not written by the vendor, and it is not used as a basis for any claim here.

Windows-specific managed-settings registry precedence and the WSL inheritance flag were taken from
a machine-generated summary of the `/settings` page and not from the page text. Re-verify them
against `/settings` directly before building on them. The registry paths, the HKCU fallback and
`wslInheritsWindowsSettings` were re-read from the `/managed-settings` page text on 2026-10-06 (§4)
[S13]; registry precedence beyond what §4 records is still unverified.

Also open, and stated where they arise: which environment variables a stdio child receives (§1),
whether `${VAR}` expansion applies outside `.mcp.json` (§1), whether the sandbox protects
`.mcp.json` (§5), which tools the protected-path gate covers (§5), and whether a wrapped agent can
undo the wrap from its own shell (§7).

Open after the 2026-10-09 run and the 2026-10-12 matrix: server-managed settings (the organisation
the runner signed in to had none configured, c10, so whether one outranks the managed file is
untested); MDM profile delivery; `managedSourcesBehavior` first-wins against merge; interactive
sessions; a model or agent that tries to switch its own hooks off or edit its own settings; Linux
and Windows; repeatability beyond two runs of the matrix and one to four trials per cell of the
2026-10-09 run; `onFailure` anywhere but the handler object of a `PreToolUse` hook, and with any
value but `"block"`; whether `--bare` leaves a managed hook with `onFailure: "block"` able to fail
(the matrix ran `--bare` with the decider up only, c11a); and whether a wrapped agent can overwrite
the launcher or the package a command hook runs.

### What was run

No command was run against a Claude Code installation when the documentation was read on
2026-07-25. Every claim that carries a source tag is a documentation claim and was not observed
against a running binary.

### Behaviour observed since

The blocks below, the `onFailure` paragraphs in §3 and the subsection "Managed settings,
observed 2026-10-12" in §4 are the only statements in this document that are not documentation
claims.

Observed on 2026-07-27, on one developer machine, Claude Code version not recorded:

1. The structure of `~/.claude.json`, on a config of roughly 220 KB with over a hundred project
   entries: there is no top-level `mcpServers` key when the user has no user-scope servers.
2. Its live-state mutation (the correction in §7).
3. The file round-trips byte-identically through `json.dumps(indent=2, ensure_ascii=False)`.
4. `claude mcp list` reports a rewritten local-scope entry as `✔ Connected`.

**Observed, 2026-10-06.** On one developer machine, Claude Code 2.1.292, headless (`claude -p`),
with the hooks in a project `.claude/settings.json` and the session started with
`--setting-sources project`. Managed settings were not tested, because they need root.

1. `PreToolUse` and `PostToolUse` fired for Read, Bash, Write and Edit calls.
2. A hook blocked a Bash call both by exiting 2 and by returning `permissionDecision: "deny"`.
3. A hook that exited 1, one that printed non-JSON, and one that ran past its timeout each let the
   call proceed. So did an `http` hook whose endpoint was down.
4. With Write and Edit allowed, the agent's writes to `.claude/settings.json` asked for a permission
   that was never granted. The agent then overwrote the hook's script file with Write, and the next
   Bash call ran. The script sat at the project root, outside the protected `.claude/` directory
   (§5); only the settings file that named it was protected.
5. An `http` hook delivered the `Authorization` header set in the settings file.
6. With no allow rules in the harness and a `PreToolUse` hook answering every call with an explicit
   allow or deny, stopping the hook's endpoint left a Bash call refused in `dontAsk` mode
   ("Permission to use Bash has been denied because Claude Code is running in don't ask mode") and
   left it as an unanswered permission prompt in `default` mode, while a Read inside the project
   still proceeded.

Items 3 and 6 match the failure table in §3: a failed hook renders no decision, and the harness's
own permission flow then decides [Inferred]. For tegh, from item 4 [Inferred]: the settings file
that defines a hook is a protected path, and the script a command hook runs need not be, so a hook
is only as fixed as the least protected file it executes.

**Observed, 2026-10-09.** On macOS, Claude Code 2.1.295, headless (`claude -p`), haiku, each trial
in a new directory with its own `.claude/settings.json` (and `.claude/settings.local.json` where
named), started with `--setting-sources project` or `project,local`. Every prompt was a plain,
cooperative request. One to four trials per cell. Managed settings were not used.

1. With an HTTP `PreToolUse` and `PostToolUse` hook pointed at a port with nothing listening, and
   no allow rules anywhere:

   | Mode | `echo hi` (read-only) | `python3 -c` (not read-only) | Write a new file |
   |---|---|---|---|
   | `default` | ran | refused | refused |
   | `acceptEdits` | ran | refused | ran |
   | `plan` | ran (1 of 2) | not attempted | not attempted |
   | `auto` | ran | ran | ran |
   | `dontAsk` | ran | refused | refused |
   | `bypassPermissions` | ran | ran | ran |

   With the decider answering every call with an allow, the `python3` call and the Write ran in
   `default`, `dontAsk` and `auto`. So in `default` and `dontAsk` the decider's allow was the only
   approval, and in `auto` the call ran without one.
2. A Read inside the trial directory ran in all six modes with the decider down, and the decider
   saw neither hook for it. `onFailure: "block"` refused it (§3, "Failure semantics").
3. `--bare`, and `CLAUDE_CODE_SIMPLE=1` in the environment without the flag, each skipped the
   project's hooks: a witness hook did not fire, and the call ran. Nothing on stderr or in the
   debug log said hooks were skipped.
4. A local `allowedHttpHookUrls` that missed the decider's URL skipped the project's HTTP hook,
   with a warning in the debug log only, and the call ran. With `onFailure: "block"` on the hook the
   call was refused.
5. A local `disableAllHooks: true` stopped every project hook, overrode a `false` in the project
   file, and defeated `onFailure: "block"`: the call ran. Nothing on stderr said so.
6. In a workspace never marked trusted, under `-p`, the project file's `permissions.allow` was
   ignored with a notice on stderr while the same file's hooks ran and the local file's
   `allowedHttpHookUrls` and `disableAllHooks` took effect.

Item 1 corrects the conclusion drawn from item 6 of 2026-10-06: a failed hook leaves the call to
the harness's own permission flow, and that flow refuses a call with no allow rule only in
`default` and `dontAsk`, and only for a call that prompts there. In `auto`, the built-in starting
mode for interactive sessions from 2.1.283 (§4), it ran.

**Observed, 2026-10-12.** The managed-settings matrix, with its cells and verdict table, is in §4,
"Managed settings, observed 2026-10-12".

Everything else here remains unverified against a binary.

## Sources

[S1] to [S11] were read on 2026-07-25. [S2], [S3], [S8] and [S11] were read again on 2026-10-06
for §3 and §4, and [S12] to [S17] were first read on 2026-10-06. [S16] was read again on 2026-10-09
for its 2.1.295 entry, and [S2] and [S16] on 2026-10-12 for `onFailure`. A tag that carries a date
names the reading it comes from.

| Tag | Page | URL |
|---|---|---|
| [S1] | Connect Claude Code to tools via MCP | `https://code.claude.com/docs/en/mcp` |
| [S2] | Hooks reference | `https://code.claude.com/docs/en/hooks` |
| [S3] | Configure permissions | `https://code.claude.com/docs/en/permissions` |
| [S4] | Settings | `https://code.claude.com/docs/en/settings` |
| [S5] | Control MCP server access for your organization | `https://code.claude.com/docs/en/managed-mcp` |
| [S6] | Security | `https://code.claude.com/docs/en/security` |
| [S7] | Authentication | `https://code.claude.com/docs/en/authentication` |
| [S8] | CLI reference | `https://code.claude.com/docs/en/cli-reference` |
| [S9] | Agent SDK overview | `https://code.claude.com/docs/en/agent-sdk/overview` |
| [S10] | Configure the sandboxed Bash tool | `https://code.claude.com/docs/en/sandboxing` |
| [S11] | Choose a permission mode | `https://code.claude.com/docs/en/permission-modes` |
| [S12] | Automate actions with hooks | `https://code.claude.com/docs/en/hooks-guide` |
| [S13] | Deploy managed settings | `https://code.claude.com/docs/en/managed-settings` |
| [S14] | Settings reference | `https://code.claude.com/docs/en/settings-reference` |
| [S15] | Tools reference | `https://code.claude.com/docs/en/tools-reference` |
| [S16] | Changelog | `https://code.claude.com/docs/en/changelog` |
| [S17] | Configure server-managed settings | `https://code.claude.com/docs/en/server-managed-settings` |

[S6] was read and no claim in this document cites it.

Not read, and referenced only through cross-links inside the pages above (see "Coverage and limits"
for what this limits): `/plugins`, `/plugins-reference`, `/agent-sdk/permissions`,
`/agent-sdk/hooks`, `/claude-apps-gateway`, and the mods pages.
