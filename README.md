# tegh

tegh pins the MCP tools a coding agent uses. It wraps a project's agent so that every tool call
goes through a local broker, which allows the call, refuses it, or holds it for your approval, and
writes each decision to an audit tape you can verify. It runs on a Mac with a local SQLite store
and needs no cloud account.

NOTE: This project contains code that was written with AI.

## Status

Early software. `tegh` is published on [PyPI](https://pypi.org/project/tegh/). Claude Code is the
only agent tegh wraps today.

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
