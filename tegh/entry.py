"""The `tegh` console script, and the module the harness's hook and MCP entries run.

    tegh <subcommand> ...        (the console script, `pyproject.toml`)
    python -P -m tegh.entry ...  (the launcher form, `launch.tegh_launcher`)

`tegh hook` runs before and after every built-in tool call a wrapped Claude Code
makes, so what it imports is paid on each of those calls. `tegh/cli.py` imports
the base's schemas at module top, and through them pydantic, yaml, cryptography
and the rest of the base's import graph. This module routes `hook` to
`tegh.hook` before `tegh.cli` is imported, so the hook loads only the standard
library and the two tegh modules it names (`hook.py`, "A thin client"). Every
other subcommand imports `tegh.cli` here and runs its `main`.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

#: The subcommand routed past `tegh.cli`.
HOOK_SUBCOMMAND = "hook"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one `tegh` command line and return its exit status."""
    words = sys.argv[1:] if argv is None else list(argv)
    if words[:1] == [HOOK_SUBCOMMAND]:
        from tegh.hook import hook_main  # noqa: PLC0415 - the point of this module

        return hook_main(words[1:])
    from tegh.cli import main as cli_main  # noqa: PLC0415 - loaded only off the hook path

    return cli_main(words)


if __name__ == "__main__":
    raise SystemExit(main())
