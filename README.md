# tegh

tegh pins the MCP tools a coding agent uses. It wraps a project's agent so that every tool call
goes through a local broker, which allows the call, refuses it, or holds it for your approval, and
writes each decision to an audit tape you can verify. It runs on a Mac with a local SQLite store
and needs no cloud account.

NOTE: This project contains code that was written with AI.

tegh wraps agents you don't control; agents you build can use the broker directly. MCP is how tegh
reaches a stock agent. An agent you write yourself can call the platform's broker without it, and
[`docs/canonical-consumer.md`](https://github.com/wjatx/ptc-gal-reference/blob/main/docs/canonical-consumer.md)
and [`examples/`](https://github.com/wjatx/ptc-gal-reference/tree/main/examples) in the platform
repository show that path.

## Status

Alpha, in single-player mode: one person, one machine, one OS user. `tegh` is published on
[PyPI](https://pypi.org/project/tegh/). Nobody but the maintainer has walked the quickstart yet
([#2](https://github.com/Third-Ralph/tegh/issues/2)), so nothing here states a setup time.

## Supported agents

| Agent | Wrapped | Posture reached | Maintained by |
|---|---|---|---|
| Claude Code | Yes | 1 | core |
| OpenClaw | No, planned next | none | |
| OpenCode | No, planned | none | |
| Hermes | No, planned | none | |
| Cursor | No, planned | none | |

"Core" means the adapter lives in this repository under `tegh/harnesses/` and its tests run in this
repository's CI. No community-maintained adapter exists.

What the Claude Code row covers, and where it stops:

- **MCP tools only.** tegh rewrites the project's Claude Code configuration so that the gateway is
  its only MCP server. Claude Code's built-in shell, file and network tools are not routed through
  anything tegh controls.
- **Stdio servers, credentialed ones included.** A literal credential in a stdio server's `env` is
  moved out of the Claude Code configuration into tegh's store and delivered when the server is
  spawned. A remote server that takes a credential in an HTTP header is refused
  ([#4](https://github.com/Third-Ralph/tegh/issues/4)), and a server on the `sse` or `ws` transport
  cannot be recorded in the lock.
- **Whether a wrapped Claude Code can undo its own wrap is unresolved.** `tegh posture` reports it
  as unknown and cites what the vendor's documentation does and does not say.

## Posture

The platform's
[posture ladder](https://github.com/wjatx/ptc-gal-reference/blob/main/docs/posture-ladder.md) names
three postures by where the boundary between the agent and its controller sits. Every claim in this
repository is made at one of them, and `tegh posture` prints which one a project is on, with a
source for each line.

| Posture | What it is | What tegh does today |
|---|---|---|
| 1 | A local wrapper, running as the same OS user as the agent | Built. A wrapped project is at posture 1; `tegh posture` reports an unwrapped one as below it. |
| 2 | The agent inside a sandbox, the gateway outside, egress to the gateway only | Not built on a Mac. `tegh/openshift/` is a leg that reports posture from inside an OpenShift pod, and the report still reads posture 1 there because it does not attempt the refusals that would prove the containment. |
| 3 | The broker under its own cloud identity, the agent holding no credentials | Not reachable from tegh. The platform's cloud floor is posture 3, and tegh does not connect a project to it. |

Posture 1 is a boundary against an injected agent: one that follows a poisoned instruction and
edits where it normally edits. tegh's keys and store sit outside the project tree, in `~/.tegh`.
The same OS user can still read and write that directory, so posture 1 does not stop someone who
already has your login, and the local audit tape is self-consistent without being tamper-evident.
Posture 2 is the recommended hardening for an agent that handles anything you would mind losing:
tegh wrapping an agent that runs in a sandbox.

The quickstart ends with "What this does not prove", which lists these limits against the walk it
has just shown.

## Install

In a virtual environment, with Python 3.12 or newer:

```bash
python3 -m venv venv && source venv/bin/activate
python -m pip install tegh
tegh --help
```

To have every dependency checked against a pinned hash, use the lock file attached to each
[GitHub release](https://github.com/Third-Ralph/tegh/releases). `tegh-<version>-requirements.txt`
pins `tegh` at the hash of the released wheel and every package it depends on, the platform
included, at an exact version with hashes:

```bash
python -m pip install --require-hashes -r tegh-<version>-requirements.txt
```

`docs/tegh-quickstart.md` walks the whole path: `tegh init`, `tegh wrap claude` with a reviewed
admission, a call held and released with `tegh approve`, a call refused, and `tegh audit --verify`.
`docs/tegh-lock.md` describes the lockfile.

## How it relates to the platform

tegh is a consumer of [`safe-agents`](https://pypi.org/project/safe-agents/), the reference
implementation published from
[wjatx/ptc-gal-reference](https://github.com/wjatx/ptc-gal-reference), pinned to an exact version
in `pyproject.toml`. The broker, the admission ceremony, the approval release and the audit chain
are the platform's. tegh imports only what the platform publishes for a consumer to fill and to
run (`safe_agents.broker.schemas` and the gateway client from `safe_agents.broker.api`) and runs
the platform's commands as child processes. `tegh/tests/test_lock.py` enforces that boundary. A
change tegh needs in the platform lands there first and is consumed here at a version.

`docs/references/harnesses/claude-code.md` records, with sources, how Claude Code stores and loads
MCP configuration. The wrap mechanics are built against it.

## Develop

From a checkout, in a virtual environment. `requirements/dev.txt` holds the project's dependencies
and its `dev` extra at exact versions with hashes, and the second command installs the checkout
against them without downloading anything:

```bash
python -m pip install --require-hashes -r requirements/dev.txt
python -m pip install --no-index --no-build-isolation --check-build-dependencies -e ".[dev]"
pytest
ruff check .
```

The header of `.github/workflows/ci.yml` says how the lock files under `requirements/` are
regenerated when a dependency changes.

`tegh posture` cites its sources, and `tegh/tests/test_posture_citations.py` checks that each one
still resolves. Some resolve in the platform repository through the `gh` command; without `gh`
those checks skip, and `TEGH_CITATIONS_STRICT=1` makes them fail instead.

The fresh-install drill (`tegh/tests/drill_fresh_install.py`) installs the package into a clean
environment and walks the quickstart's chain against the installed command. It runs on macOS in
`.github/workflows/tegh-fresh-mac-drill.yml`.

## License

Apache-2.0. Copyright 2026 Wes Jackson.
