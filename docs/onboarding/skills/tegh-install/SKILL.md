---
name: tegh-install
description: >-
  Install tegh on a Mac and mint its local keys, checking what each step prints. Use when the
  user asks to install or set up tegh, when a tegh command answers "command not found", or when
  a command answers "REFUSED: no tegh home". Ends with `tegh --help` listing ten subcommands and
  `tegh init` reporting a provisioned home. It does not wrap a project; the tegh-define-job
  skill does that next.
---

# Install tegh and mint this machine's keys

tegh pins the MCP tools a coding agent uses behind a local broker. Installing it is two steps: the
Python package, and one `tegh init` per machine. Neither touches any agent's configuration.

Run one step at a time. After each, compare what was printed with "Expect". If a step prints
something under "If it refuses", do what that row says and do not go on.

This file states no duration for any step. Nobody but the maintainer has timed the walk
([#2](https://github.com/Third-Ralph/tegh/issues/2)), so do not tell the user how long it takes.

## What you need

- macOS.
- Python 3.12 or newer. Check with `python3 --version`.
- Claude Code, if a project is going to be wrapped afterwards. It is the one agent tegh wraps.
- No cloud account and no other service.

If `python3 --version` prints a version below 3.12, stop and tell the user. Do not install a
Python for them without asking.

## Step 1. Install into a virtual environment

Pick a directory that will stay where it is. A wrap writes the absolute path of this `tegh`
executable into the agent's MCP configuration, so moving or deleting the environment while a
project is wrapped breaks that project's gateway.

```bash
python3 -m venv ~/.venvs/tegh
~/.venvs/tegh/bin/python -m pip install tegh
~/.venvs/tegh/bin/tegh --help
```

Use the absolute path to `tegh` in every later command, or activate the environment in the shell
you are using (`source ~/.venvs/tegh/bin/activate`). A shell you start later does not inherit an
activation.

**Expect** from `tegh --help`:

```
usage: tegh [-h]
            {init,wrap,gateway,call,approve,audit,unwrap,status,diff,posture}
            ...

Pin the MCP tools a coding project already uses.
```

Then record the version:

```bash
~/.venvs/tegh/bin/python -m pip show tegh
```

The number alone does not say which behaviour is installed. The package index serves 0.1.1 as
this is written, and the skills in this directory describe what the repository's main branch
prints, which carries the same number. On 0.1.1 as published, a wrap is not a transaction: a
second wrap of a wrapped project is not refused and replaces `tegh.lock` with an empty one
([#11](https://github.com/Third-Ralph/tegh/issues/11)), and a wrap whose input ends during the
review exits with a traceback ([#12](https://github.com/Third-Ralph/tegh/issues/12)).

The inventory pass in `tegh-define-job` step 3 is what tells them apart. Later code ends that pass
with a line starting `NOT WRAPPED: no tool was admitted` and exit status 1. The published 0.1.1
ends it with `Nothing was admitted, so the config is left alone`, exit status 0, and leaves a
`tegh.lock` that pins no tool in the project. Both defects above were reproduced on the code
tagged v0.1.1, loaded from the tag and not from the wheel the index serves: a wrap with its input
closed ended in a traceback with `EOFError: EOF when reading a line`, and a second wrap of a
wrapped project exited 0 and wrote a lock pinning 0 tools. If the user gets the second ending, tell them the
two defects above before any real wrap, and that the refusals quoted in these skills are the later
ones.

**With every dependency checked against a pinned hash.** Each release of the repository attaches
`tegh-<version>-requirements.txt`, which pins tegh and everything it depends on at exact versions
with hashes. If the user wants that, they download the file from the release and you run:

```bash
~/.venvs/tegh/bin/python -m pip install --require-hashes -r tegh-<version>-requirements.txt
```

**If it refuses**

These three come from pip and the shell, and were not reproduced when this file was written, so
match on the meaning and not on exact wording.

| You see | Do this |
|---|---|
| pip cannot find a version of `tegh` for this Python, or says it requires a different one | The interpreter is older than 3.12. Stop and tell the user. |
| pip reports that a package does not match the hashes in the requirements file | Stop. Do not retry without `--require-hashes`. Tell the user which package failed. |
| The shell cannot find `tegh` | The environment is not active in this shell. Use the absolute path. |

There is no `mcp` extra to ask for, and `pip install safe-agents` alone does not install tegh:
that package is the platform tegh depends on.

## Step 2. Mint this machine's keys

```bash
tegh init
```

**Expect**, with the user's own home in the paths:

```
tegh home provisioned: /Users/you/.tegh
  store database   /Users/you/.tegh/tegh.db (created on first admission)
  HMAC key         /Users/you/.tegh/hmac.key (0600)
  issuer key       /Users/you/.tegh/issuer.pem (0600)
  issuer key_id    tegh-local-a2292051cce5

This directory is local authority: it holds the tool namespace and the
activation rows, deliberately outside any project tree. A same-user
attacker can still read it — that is posture 1, and `tegh posture` says so.
```

The key id differs on every machine. Exit status 0.

**If it refuses**

| You see | Exit | Do this |
|---|---|---|
| `REFUSED: /Users/you/.tegh/hmac.key already exists — refusing to overwrite it.` | 2 | The machine already has a tegh home. That is fine: go on. Never delete `~/.tegh` to get past this. Every project already wrapped on the machine depends on those keys. |

## Step 3. Confirm the state you are leaving

```bash
tegh posture --project <the project the user means to wrap>
```

**Expect**, for a project that is not wrapped yet:

```
tegh posture

  POSTURE pre-1  (docs/posture-ladder.md)
    This configuration is BELOW posture 1 — the evidence machinery is real, the enforcement half is not wired
```

and, lower down, `[-] This project is not wrapped`. Exit status 0. Nothing is gated until a
project is wrapped.

The report is long. Its findings are the lines that start with a mark: `[+]` (holds), `[-]` (does
not hold), `[~]` (partly) or `[?]` (unknown). Under each finding are indented lines of explanation
and one `source:` line, and the findings sit under section headings such as `WHAT HOLDS` and
`WHAT DOES NOT`. On an unwrapped project the run this file was checked with printed 17 findings:
4 `[+]`, 7 `[-]`, 3 `[~]` and 3 `[?]`. Do not summarise the report as "secure" or "protected".
Give the user the posture line and the count of each mark.

## What to tell the user now

- tegh is installed and has keys. No project is wrapped, so no tool call is gated yet.
- The next step is deciding what the agent is for in one project, which is the `tegh-define-job`
  skill. It ends with the user answering the wrap review themselves.
- What the wrap will and will not give them, in the README's terms: a boundary against an agent
  that follows a poisoned instruction, on MCP tools only, as the same OS user as the agent. It
  does not stop someone who already has their login, and the agent's built-in shell, file and
  network tools are not routed through it.

## Undoing the install

Unwrap every wrapped project first (`tegh unwrap --project <path>`, run by the user). Then the
environment can be removed. Removing `~/.tegh` is the user's decision and theirs to carry out: it
holds the keys, the admission store and every project's audit tape.
