"""tegh — the product layer built ON the safe-agents base platform.

safe-agents is the domain-invariant floor: a deterministic tool broker, the
seven base schemas, the two-key MCP admission ceremony. `tegh` is the
Mac-installable product that packages that floor for a developer with no AWS
account — `pip install tegh`, a local sqlite store, and a wrapped coding agent
whose MCP tools are pinned by a human-run admission ceremony.

This package is top-level and consumes the base as a dependency: the
`safe-agents` distribution the public reference implementation publishes to
PyPI, pinned to an exact version in `pyproject.toml`. Two disciplines keep it a consumer rather than a
fork, and are enforced, not merely intended:

- **It stands on the base's PUBLIC surface only.** `safe_agents.broker.schemas`
  is the one broker subpackage tegh imports whole, and from
  `safe_agents.broker.api` it takes the gateway client alone (three names, in
  `call.py`), never `build_runtime` or anything else that decides. The base
  publishes those two modules as what a consumer fills and what it runs (the
  `safe_agents.broker.api` module docstring is the statement of that), and
  `tests/test_lock.py::TestImportBoundary` holds tegh to the narrower set named
  here. Reaching into any other `safe_agents.broker.<X>` would make tegh a fork
  of the broker rather than a consumer of it.
- **It re-uses base primitives rather than restating them.** The drift hash,
  the tool-definition model, and the ToolOp classification come from the base:
  one drift primitive, not two.
"""
