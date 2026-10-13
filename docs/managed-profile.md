# The managed settings profile for Claude Code

`tegh wrap claude` puts two hook entries in the project's `.claude/settings.local.json`: a
`PreToolUse` entry that asks the broker before each of Claude Code's own tool calls, and a
`PostToolUse` entry that reports the call after it ran. Anything the developer's user can write can
switch those entries off, add an allow rule that approves a call the broker never saw, or start a
session in a mode that runs a call with no approval at all. Claude Code's managed settings file is
the one place a lower scope cannot reach. This page describes the document tegh defines for it.

**Nothing installs it yet.** `tegh/profile.py::managed_profile` defines the document, and its tests
hold this page to it. Writing the managed file needs root, and deploying it to a fleet of machines
is a later phase. `tegh posture` cannot yet read whether a machine has managed settings, so it does
not change its report when one does.

## The document

For a project at `/Users/you/tegh-demo`, a tegh home at `/Users/you/.tegh` and tegh installed in
`/Users/you/venv`, the profile is:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/you/venv/bin/tegh hook --project /Users/you/tegh-demo --home /Users/you/.tegh",
            "timeout": 10,
            "onFailure": "block"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/you/venv/bin/tegh hook --project /Users/you/tegh-demo --home /Users/you/.tegh",
            "timeout": 10
          }
        ]
      }
    ]
  },
  "allowManagedHooksOnly": true,
  "allowManagedPermissionRulesOnly": true,
  "disableAutoMode": "disable",
  "permissions": {
    "disableBypassPermissionsMode": "disable"
  },
  "allowedHttpHookUrls": [],
  "httpHookAllowedEnvVars": []
}
```

The command is the one the wrap writes into the project's local settings. On macOS the file is
`/Library/Application Support/ClaudeCode/managed-settings.json`; the harness reference (§4) gives
the Linux and Windows paths.

## What each key does, and the evidence for it

The evidence is a matrix run on a disposable macOS runner on 2026-10-12, Claude Code 2.1.295, one
trial per cell, run twice. Its findings and its verdict table are in
`docs/references/harnesses/claude-code.md` §4, "Managed settings, observed 2026-10-12", and a cell
id below names a row of that table.

| Key | What it does | Cells |
|---|---|---|
| `hooks` | Carries the wrap's two entries in the managed file. The matrix's managed hook stayed in place under `disableAllHooks` in the project file, the local file and `--settings`, and under `--bare` and `--safe-mode`. The runner's user could not overwrite, add to or rename the policy file. | c05a to c05c, c11a, c11b, c09 |
| `onFailure: "block"` on the `PreToolUse` entry | A hook that fails refuses the call instead of letting it run. Refused when the decider was down and when it hung past the timeout; without it the call ran. | c08b, c08d; controls c08a, c08c |
| `allowManagedHooksOnly` | Drops a project or user hook and keeps the managed one. The wrap's own entries in the local file are dropped with them, which is why the profile carries the same two. | c04a to c04d |
| `allowManagedPermissionRulesOnly` | Removes the allow rules from `--allowedTools`, the local, project and user files and `--settings`, each of which otherwise let the call run with the decider down. The broker's answer is then the only approval a call gets from below the managed file. | the ten c02 cells |
| `disableAutoMode: "disable"` | With no decider and no allow rule, `auto` mode ran the call, on haiku and on sonnet. This key refused it. | c01d, c01g; c01e, c01h |
| `permissions.disableBypassPermissionsMode: "disable"` | `--dangerously-skip-permissions`, `--permission-mode bypassPermissions` and a project `defaultMode` are ignored and the session starts in `default`. | c03b to c03d; control c03a |
| `allowedHttpHookUrls`, `httpHookAllowedEnvVars` | Set by the managed file, each merges with any lower list as a union, and a hostile lower list neither blocks the managed HTTP hook nor strips its header. Left unset, a lower list that misses the decider's URL blocks a managed HTTP hook, `allowManagedHooksOnly` does not prevent it, and a lower list that omits the token's variable strips the header. | c06h, c06i; c06b to c06g |

The keys sit where the matrix put them. The matrix wrote `allowManagedPermissionRulesOnly` and
`disableAutoMode` at the top level of the file and `disableBypassPermissionsMode` under
`permissions`. The vendor's permissions page names the auto-mode key as `permissions.disableAutoMode`
(harness reference §4); that placement was not among the cells, and the profile keeps the one that
was.

The two allowlists bind HTTP hooks only, and tegh's hook is a command hook, so with their default
empty values they govern nothing tegh installs. They are in the profile so that a decider reached
over HTTP is named by the managed file from the start: `managed_profile` takes the decider's URL
patterns (`decider_urls`) and its token's variable (`token_env_vars`). An empty managed list was not
among the cells.

## What it does not cover

- **One project, one tegh home.** The command names both, so the profile as defined serves one
  wrapped project on one machine. A profile for a fleet is the deploying phase's to define.
- **A managed command hook was not tested.** The matrix's managed hook was an HTTP hook. That a
  managed command hook holds under the same conditions is inferred, not observed.
- **The launcher is outside the managed directory.** The hook command runs tegh from the
  developer's own environment, which the developer's user can overwrite. A recorded run on
  2026-10-06 saw an agent overwrite the script a project hook ran (harness reference, "Behaviour
  observed since"). Whether a wrapped agent can overwrite tegh's launcher or package is untested.
- **No `managed-mcp.json`.** That file takes exclusive control of MCP servers (c07a to c07c), so one
  that does not name the gateway unloads it. The profile leaves MCP configuration to the wrap.
- **No server-managed settings and no MDM profile.** The runner's organisation had no
  server-managed settings configured (c10), so whether one would outrank this file is untested, and
  MDM delivery was not tried.
- **A read-only shell command when the hook does not decide it.** Where the hook never runs,
  `default` mode runs a read-only command such as `echo` with no decider and no allow rule
  (harness reference, "Behaviour observed since", 2026-10-09; c01f and c03c in the second run). The
  broker loses sight of the call; the command writes nothing.
- **A session that never calls home.** A session whose hooks never run leaves nothing on the tape,
  so tegh cannot report it until the gateway records when its mouths open and close
  ([wjatx/ptc-gal-reference#192](https://github.com/wjatx/ptc-gal-reference/issues/192)).
- **Containment.** Under this profile the agent's built-in tools are mediated by the broker and are
  not contained: an answer binds only as far as Claude Code enforces it, and a call the broker
  permits runs with the full reach of the developer's user. The platform states the same limit in
  `broker/GATEWAY.md`, "Known limits": built-in tools are observed, and decided when asked; that gap
  closes by containment.

Also not established by the matrix: interactive sessions, an agent that tries to switch its own
hooks off, Linux and Windows, and repeatability beyond two runs.
