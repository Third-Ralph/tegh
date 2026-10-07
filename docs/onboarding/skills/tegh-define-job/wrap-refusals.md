# What `tegh wrap` prints when it stops, and what to do

A stop is reported on a line that starts with a label: `REFUSED`, `NOT WRAPPED`, `INTERRUPTED`
or `FAILED`. Two rows below differ. `Nothing to wrap:` carries none of those labels, and the
`INCOMPLETE` refusal is five lines long with its finding in a separate `Findings:` block, which
was seen both before and after the `REFUSED` block. Before the wrap's commit point (the heading
`ADMITTING`), a stop puts back every file the wrap wrote. Several of the messages say so in the
words "Nothing was wrapped"; the `INCOMPLETE` refusal does not carry that sentence. Show the user
the whole message. Do not retry with a flag that makes the refusal go away unless the
row below says the user may choose to.

Each row quotes the start of the real message. Paths are shortened to `<...>`. Every row was
reproduced with the text and exit status shown, except where "What was not reproduced" at the end
says otherwise.

## Before the review (no tool block has been printed)

| The line starts | Exit | Why | What to do |
|---|---|---|---|
| `REFUSED: no tegh home at <dir>. Run `tegh init` first` | 2 | This machine has no tegh keys yet. | Run `tegh init` once (the `tegh-install` skill), then wrap again. |
| `REFUSED: <project> is already wrapped: its harness config (<scope> scope, <file>) runs tegh's gateway for this project as the server 'tegh'.` | 2 | A second wrap would review the gateway as if it were a server. Nothing was changed. | To change what is admitted, the user runs the `tegh unwrap` command the message gives, then wraps again. Read "Changing the tool set" in `SKILL.md` first. |
| `REFUSED: this wrap would be INCOMPLETE.` with a finding `!! claude-ai-not-enumerable` above it | 2 | claude.ai connectors live in the user's account and cannot be listed from disk, so they would stay reachable around the gateway. | The user chooses between two things. One is to set `"disableClaudeAiConnectors": true`, which turns those connectors off for them. tegh reads that key from `~/.claude.json`, `~/.claude/settings.json`, and the project's `.claude/settings.json` and `.claude/settings.local.json`; one file is enough, and a `false` in any of them keeps the refusal. The other is to pass `--accept-gaps` and wrap with the gap on record. Do not choose for them, and do not edit their settings yourself. |
| `REFUSED: this wrap would be INCOMPLETE.` with a finding `!! unreadable-config: <file> is present but could not be read: invalid JSON (...)` or `... could not be read: unreadable (PermissionError)` | 2 | A config scope that cannot be read cannot be wrapped, and its servers would stay live. | The user repairs the file or its permissions. Do not edit their config yourself. |
| `Nothing to wrap: no server this harness loads can be pinned.` | 1 | The project has no MCP server tegh can pin. `Discovered 0 MCP server(s)` is printed above it. | Check the project path and, if the wrap was given `--harness-home`, that directory. A project with no MCP servers has nothing for tegh to gate. |
| `!! <server>: transport 'sse' cannot be pinned by the lock format. It loads in the harness and is NOT wrapped.` | (continues) | `sse` and `ws` servers cannot be recorded in the lock. | Tell the user that server stays outside the wrap. Record it in the job file. |
| `REFUSED: <file> does not round-trip through tegh's JSON serializer byte-for-byte` | 2 | tegh writes JSON with two-space indent and no newline after the closing brace, and will not reformat a file that differs. One trailing newline is enough: a hand-edited `.mcp.json` that an editor ended with a newline is refused. | The user decides whether to reformat that file. Do not reformat it yourself. |
| `REFUSED: you classified 1 value(s) as CREDENTIALS in a block tegh cannot yet relocate: <scope>:<server>.headers.<name>` | 2 | Printed after the config-value questions. A credential in a remote server's HTTP header cannot be moved into tegh's store yet ([#4](https://github.com/Third-Ralph/tegh/issues/4)), and tegh will not keep a second copy of it. | Show the user the line: it gives the two things that work today, a `${VAR}` reference in place of the literal, or taking that server out of the agent's config. Answering `n` to get past it is wrong for a real credential, and the review warns that a header classified as configuration is not delivered. |

A server from a project's `.mcp.json` is listed as `pending-approval`, with the finding
`not-active [<server>]: '<server>' is pending-approval: a .mcp.json server is not connected until a
human accepts it interactively`. That finding is information and stops nothing. The server is
reviewed and wrapped like any other.

## During the review (nothing admitted yet, everything is put back)

| The line starts | Exit | Why | What to do |
|---|---|---|---|
| `REFUSED: input ended before this wrap's questions were answered; the one left open was `...`` | 2 | The wrap was run without a terminal, or piped answers ran out. | The user runs the wrap in a terminal and answers each question. This is the refusal you get if you run the wrap yourself. |
| `    REFUSED: effect must be 'read' or 'write', not '<what was typed>'` (indented, inside an edit) | (continues) | A wrong answer at the `effect` prompt. It is not printed at once: the `reversible` prompt is skipped, `egress_arg` is asked, and this line follows that answer. The tool's classification is unchanged, the proposed block is printed again, and the `[y] admit` question is asked again. | Not a stop. The user presses Return at `egress_arg`, types `e` at the `[y] admit` prompt, and answers that block again from the sheet. Typing `y` there admits the tool as proposed. |
| `NOT WRAPPED: no tool was admitted` | 1 | Every tool was skipped. | Expected at the end of the inventory pass. In a real review, it means every answer was `N`. |
| `NOT WRAPPED: no server could be reached` with `!! <server>: could not be reached` and `the server said:` above it | 1 | No server started. The most common cause is printed with it: the config names a variable and the wrap delivered no value. A `${VAR}` reference is not expanded by tegh. | Show the user what the server said. Fix the server's own start-up, then wrap again. |
| `INTERRUPTED: tegh wrap was stopped before it finished. Nothing was wrapped:` | 130 after Ctrl-C, 143 after SIGTERM | The wrap was stopped by a signal during the review. | The line says nothing was wrapped. Wrap again. |
| `FAILED: ... the admission proposal for <server>/<tool>` | not 0 | The platform's ceremony refused a proposal. Nothing was admitted. | Show the user the reason the line carries. Do not retry in a loop. |

## After the commit point (`ADMITTING` was printed)

A wrap that stops here is not rolled back. It ends `FAILED` or `INTERRUPTED` with one line that
says the project **is** wrapped, lists the tools admitted and served, lists the tools not
admitted, and gives the two commands to run: `tegh unwrap` to undo it, then `tegh wrap` to
finish. A tool that was not admitted does not execute.

Two known defects bear on it:

- Finishing with a second wrap that admits a different tool set leaves the earlier tools refused,
  while a tool that wrap adds can be called
  ([#14](https://github.com/Third-Ralph/tegh/issues/14)). Wrap again with the same answers.
- A ceremony that hangs after the commit point can only be stopped with SIGKILL
  ([#20](https://github.com/Third-Ralph/tegh/issues/20)).

## Other commands

| The line starts | Exit | What to do |
|---|---|---|
| `REFUSED: <project> has not been wrapped, so there is no gateway to call` (`tegh call`) | 2 | Wrap first. |
| `REFUSED: <project> has not been wrapped — nothing has been held for it.` (`tegh approve`) | 2 | Wrap first. |
| `REFUSED: stdin is not a terminal, so nobody can answer this prompt.` then `NOT RELEASED — the intent is still held.` (`tegh approve`) | 1 | The user runs it in a terminal. This is the refusal you get if you run it yourself. The call stays held. |
| `REFUSED: --args is not JSON` (`tegh call`) | 2 | Pass one JSON object in single quotes. |
| `<server>.<tool> was allowed by the broker but failed at the connector` (`tegh call`) | 1 | The broker allowed the call and the server failed. Seen when a project has been unwrapped: the relocated credential is back in the agent's config and no longer in tegh's store. Check `tegh posture`. |
| `<server>.<tool> refused by the broker: tool not granted to this principal` (`tegh call`) | 1 | On a tool the wrap reported as admitted, this is [#14](https://github.com/Third-Ralph/tegh/issues/14). Stop and tell the user. |
| `REFUSED: no held intent '<id>' for this principal.` (`tegh approve`) | 2 | The hold expired (the held call prints its expiry), was already released, or belongs to another project. |
| `REFUSED: stdin and stdout are not terminals` (`tegh unwrap`) | 1 | The user runs it in a terminal, or passes `--yes` themselves. Nothing was changed. |
| `REFUSED: no wrap backup for <project>` (`tegh unwrap`) | 2 | The project is not wrapped, or was already unwrapped. |
| `tegh: error: unrecognized arguments: --harness-home <dir>` (`tegh status`, `diff`, `call`, `audit`) | 2 | Only `tegh wrap` and `tegh posture` take `--harness-home`. Drop it from the other commands. |
| `TAMPER: ...` (`tegh status`, `tegh diff`) | 3 | `tegh.lock` does not match its signature. The broker does not read the lock, so no decision changed, and somebody edited the file. Tell the user. |
| `UNVERIFIABLE: ...` | 4 | The lock was signed by a key this tegh home does not know, for example on another machine. Tell the user. |

## What was not reproduced

Five things in this file come from the source and the quickstart and were not made to happen:
the `FAILED: ... the admission proposal` row, the whole "After the commit point" section, the
hang in [#20](https://github.com/Third-Ralph/tegh/issues/20), exit status 130 after Ctrl-C (143
after SIGTERM was observed), and the `unreadable (PermissionError)` form of the unreadable-config
finding (the `invalid JSON` form was observed). Match those on meaning, and show the user the
line as printed.
