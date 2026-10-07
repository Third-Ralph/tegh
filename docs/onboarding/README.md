# Onboarding: tegh for a coding agent to follow

**Status: draft, on a branch, not reviewed for release.** These files were written on 2026-10-06
against the wrap behaviour that became tegh 0.2.0 and have not been re-read against 0.2.0 as
released. A cold reader that day found statements in them that were false at the time, mostly
about which version printed what. Do not link to them or follow them as documentation yet.

`docs/tegh-quickstart.md` is the walk for a person reading along. This directory is the same
path written for a coding agent to carry out with a person: every step says what to run, what it
prints, and what to do when it refuses.

| File | What it is |
|---|---|
| `starter-prompt.md` | One block of text a newcomer pastes into their own coding agent. It installs tegh, defines a job for a throwaway project, has the person answer the review, and ends at one call allowed, one held, one refused, and a verified audit chain. It stands alone and needs nothing else from this directory. |
| `skills/tegh-install/` | Install the package and run `tegh init`. |
| `skills/tegh-define-job/` | State what the agent is for in a project, derive the wrap review from that statement, and write both down. Covers a first wrap, a project that is already wrapped, and a person who arrives with a written scope. |
| `skills/tegh-verify-wrap/` | After a wrap: what is pinned, one call allowed, one stopped, the tape verified. |

## A job definition

A wrap's review asks, for each tool a server advertises, whether to admit it and how to classify
it. The answers are a role. `tegh-define-job` has the person state the agent's purpose in a few
sentences first, and then derives each answer from a sentence of that purpose: a tool no sentence
needs is skipped, and a write the person wants to approve is admitted as held. The purpose is the
justification and the admitted set is the role. The result is a `tegh-job.md` in the project,
beside `tegh.lock`.

The person answers the review. The agent prepares the answer sheet and checks the result with
`tegh status`, `tegh diff`, `tegh call` and `tegh audit --verify`.

## Using the skills with Claude Code

Each directory under `skills/` is one skill: a `SKILL.md` with `name` and `description`
frontmatter, and the files it refers to beside it. Copy a skill's directory into `.claude/skills/`
in a project, or into `~/.claude/skills/` to have it in every project. Claude Code reads the
description to decide when a skill applies, and a person can ask for one by name.

`docs/references/harnesses/claude-code.md` does not cover the skill format, so the paragraph
above rests on the vendor's documentation and has not been checked into that reference.

## What these files were checked against

Every command and every quoted output was run against the CLI on the branch these files were
written on, in a home directory made for the purpose, with the review answers piped or typed by
a script through a pseudo-terminal. Paths in the samples are rewritten to `/Users/you`. These
were not run:

- `pip install tegh` from the package index, and the hash-pinned install. The walk used a
  checkout's environment.
- A live Claude Code session against a wrapped project. `tegh call` stood in for the agent, and
  one check drove two calls through a single gateway session with an MCP client.
- A person at the review. Nobody but the maintainer has walked any of this
  ([#2](https://github.com/Third-Ralph/tegh/issues/2)), so no file here states how long it takes.
- A wrap that fails or is stopped after its commit point. `wrap-refusals.md` names the rows this
  leaves unreproduced.
- A wrap of a project with user-scope servers. That such servers are displaced for every project
  on the machine rests on [#8](https://github.com/Third-Ralph/tegh/issues/8) and was not observed.
- The byte metering behind `egress_arg`. What `tegh-define-job` says of it is read from the
  source.

The files describe what the main branch prints: a wrap is a transaction, a second wrap of a
wrapped project is refused, and a config that cannot be read is refused. The package index serves
0.1.1 as this is written, which does none of those, and the branch carries the same version
number. The starter prompt's steps 3 to 10 were also run against the code tagged v0.1.1, loaded
from the tag beside the same installed dependencies and not from a published wheel. They print
the same except at step 5, and the prompt gives both endings. On that tag's code a wrap with its
input closed ended in a Python traceback, and a second wrap of a wrapped project exited 0 and
replaced `tegh.lock` with one pinning no tool. The skills quote the later
behaviour, and `tegh-install` says how to tell which is installed.

They also carry one known defect into their instructions. A second wrap that changes a project's
tool set leaves the tools from the earlier wrap refused, while a tool it adds can be called
([#14](https://github.com/Third-Ralph/tegh/issues/14)). So `tegh-define-job` settles the tool set
before the first wrap and checks a call after any later one.

## Posture

Everything here is posture 1, in the README's terms: a boundary against an agent that follows a
poisoned instruction and edits where it normally edits. tegh's keys and store sit outside the
project tree, in `~/.tegh`, and the same OS user can still read and write that directory. It does
not stop someone who already has your login, the local audit tape is self-consistent without
being tamper-evident, and Claude Code's built-in shell, file and network tools are not routed
through tegh.

Two consequences for these skills in particular. The rules in them that tell the agent not to
answer the review, release a held call or unwrap are instructions to the agent, and tegh does not
enforce them. And `tegh-job.md` is a record in the project tree, which the agent can write, so it
decides nothing: the broker reads neither it nor `tegh.lock`.
