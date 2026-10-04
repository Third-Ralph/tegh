# Claude Code: extension and interposition surfaces

## About this document

This is the reference that tegh's wrap mechanics for Claude Code are built against. It records how
Claude Code stores and loads MCP server configuration, where MCP credentials sit, and which
mechanisms gate a change to that configuration. tegh's code and the output of `tegh posture` cite
it by section number and by line range.

**What it was checked against.** The documentation claims come from the vendor's public Claude
Code documentation, read on 2026-07-25. All URLs are under `code.claude.com/docs/en/` unless noted,
and 2026-07-25 is the access date for every page in the Sources list. Where a documentation page
states a version for a behaviour (for example "Requires Claude Code v2.1.203 or later"), that stamp
is copied here verbatim. The stamps are the documentation's own and were not re-verified against a
running binary. The observed behaviour was recorded on 2026-07-27 on one developer machine. The
Claude Code version running at the time was not recorded.

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
limits" lists what was read, what was not, and the only behaviour that was observed.

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

### Events (partial list; the source table lists about 30)

`SessionStart`, `UserPromptSubmit`, `PreToolUse` (before any tool call; can block), `PostToolUse`,
`PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `Notification`,
`SubagentStart`/`SubagentStop`, `Stop`, `StopFailure`, `ConfigChange` (fires when a config file
changes mid-session), `PreCompact`/`PostCompact`, `Elicitation`/`ElicitationResult` (MCP
elicitation), `SessionEnd`, and others [S2, full table]. For tegh: `ConfigChange` is a candidate
for detecting drift.

### Matcher syntax

Simple alphanumeric/`_`/`-`/space/`,`/`|` strings match exactly (`Bash`, `Edit|Write`). Anything
else is treated as an unanchored JS regex (`^Notebook`, `mcp__memory__.*`) [S2]. MCP tools are
addressable as a bare server (`mcp__memory`), as a wildcard over all of a server's tools
(`mcp__memory__.*`), or as one tool (`mcp__memory__create_entities`). Plugin-bundled servers use
`mcp__plugin_<plugin-name>_<server-name>__<tool-name>`, so a matcher written against the bare
server key never fires for a plugin server [S1, "Plugin MCP tool names"; S2].

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

### Configuration locations

`~/.claude/settings.json` (user, all projects), `.claude/settings.json` (project, committable),
`.claude/settings.local.json` (project, gitignored), managed policy settings (org-wide, not
editable by user or project), plugin `hooks/hooks.json`, and skill/agent frontmatter [S2,
"Hook Configuration Locations"].

### Apply to built-in tools too?

Yes. Hooks fire for Bash, Read, Write, Edit, WebFetch, and MCP tools alike. The only carve-out is
`EndConversation`, which a bare-name deny/ask rule (hook-adjacent permission machinery) can never
remove while any other tool remains [S2, "Application to Built-in Tools"; S3, "Manage permissions"].

### Security considerations

Hooks execute arbitrary shell commands with the full privileges of the Claude Code process, with no
sandboxing, and parse untrusted JSON from stdin [S2, "Security Considerations"]. Enterprise can
lock this down with `allowManagedHooksOnly` (blocks user/project/plugin hooks except force-enabled
plugins) [S2; S3, "Managed-only settings"].

## 4. Permission system

### Rules

`permissions.allow`/`ask`/`deny` arrays live in any settings file and merge across scopes (unlike
most settings, which override) [S3, "Manage permissions"]. Evaluation order is deny, then ask, then
allow; the first match wins regardless of specificity, so a broad deny like `Bash(aws *)` beats a
narrower allow like `Bash(aws s3 ls)` [S3, "Manage permissions"]. A bare tool name (`Bash`) as a
deny rule removes the tool from Claude's context entirely; a scoped rule (`Bash(rm *)`) leaves the
tool present and blocks matching calls [S3].

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

### Managed/enterprise policy a user cannot override

Delivery mechanisms: server-managed settings (fetched at sign-in), MDM/OS-level policy
(macOS `com.anthropic.claudecode` preferences domain; Windows `HKLM\SOFTWARE\Policies\ClaudeCode`),
or a file at a fixed OS path (`managed-settings.json`, with a `managed-settings.d/` drop-in dir)
[S4, "Settings File Locations"]. The Windows detail needs re-verification (see "Coverage and
limits"). Precedence is managed > CLI args > local project settings > shared project settings >
user settings, and permission rules merge across these where other settings override [S3,
"Settings precedence"]. Managed-only locks relevant to tegh:

| Setting | Effect |
|---|---|
| `allowManagedMcpServersOnly` / `allowedMcpServers` / `deniedMcpServers` | server allowlist/denylist [S3; S5] |
| `allowManagedPermissionRulesOnly` | locks `allow`/`ask`/`deny` to managed sources only [S3] |
| `allowManagedHooksOnly` | locks hooks to managed/SDK/force-enabled-plugin sources [S3] |
| `disableSideloadFlags` | rejects `--plugin-dir`, `--plugin-url`, `--agents`, `--mcp-config` at startup, which closes the CLI-flag interposition lever in item 5 of the summary [S3, "Managed-only settings"; S4] |
| `strictPluginOnlyCustomization` | can force skills/agents/hooks/MCP to come only from plugins or managed settings, closing off user/project `.mcp.json` entirely [S3] |
| `disableBypassPermissionsMode` | can be set from any scope (including a user locking themselves out) and is "typically placed in managed settings to enforce organizational policy" [S3] |

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

### Pages not read, and what that limits

`/plugins` and `/plugins-reference` were not read. The plugin MCP-server mechanics in §6 come from
the plugin-related sections embedded in the `/mcp` page. That is enough to answer "can a plugin
suppress another server" (no mechanism is described) and is not enough to characterize plugin
marketplace distribution, trust dialogs, or the full `plugins-reference` schema.

`/agent-sdk/permissions` and `/agent-sdk/hooks` were not read. The SDK's `canUseTool` callback is
referenced here only through its mention on the `/mcp` page, so its full signature and semantics
are not verified.

`/server-managed-settings` and `/claude-apps-gateway` were seen only as cross-links inside pages
that were read, and were not opened.

### Not established

The storage backend for per-MCP-server OAuth tokens is [unverified] (§1). Claude Code's own login
credentials are fully sourced (§2). The only lead found for the MCP-server tokens was a third-party
issue report, not written by the vendor, and it is not used as a basis for any claim here.

Windows-specific managed-settings registry precedence and the WSL inheritance flag were taken from
a machine-generated summary of the `/settings` page and not from the page text. Re-verify them
against `/settings` directly before building on them.

Also open, and stated where they arise: which environment variables a stdio child receives (§1),
whether `${VAR}` expansion applies outside `.mcp.json` (§1), whether the sandbox protects
`.mcp.json` (§5), which tools the protected-path gate covers (§5), and whether a wrapped agent can
undo the wrap from its own shell (§7).

### What was run

No command was run against a Claude Code installation when the documentation was read on
2026-07-25. Every claim that carries a source tag is a documentation claim and was not observed
against a running binary.

### Behaviour observed since

Observed on 2026-07-27, on one developer machine, Claude Code version not recorded. These are the
only statements in this document that are not documentation claims:

1. The structure of `~/.claude.json`, on a config of roughly 220 KB with over a hundred project
   entries: there is no top-level `mcpServers` key when the user has no user-scope servers.
2. Its live-state mutation (the correction in §7).
3. The file round-trips byte-identically through `json.dumps(indent=2, ensure_ascii=False)`.
4. `claude mcp list` reports a rewritten local-scope entry as `✔ Connected`.

Everything else here remains unverified against a binary.

## Sources

All read on 2026-07-25.

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

[S6] was read and no claim in this document cites it.

Not read, and referenced only through cross-links inside the pages above (see "Coverage and limits"
for what this limits): `/plugins`, `/plugins-reference`, `/agent-sdk/permissions`,
`/agent-sdk/hooks`, `/server-managed-settings`, `/claude-apps-gateway`.
