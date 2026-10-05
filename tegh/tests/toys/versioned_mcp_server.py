"""versioned_mcp_server — the toy ledger with a description that can change.

The same two read-only tools as `restricted_mcp_server`, except that
`get_entry` advertises whatever description is in the file named on the
command line, read when the server starts. A test rewrites that file to stand
for a server that changed a tool's definition after it was admitted, which is
the one thing the fixed toy cannot do.

`get_entry` answers with the description it is running under, so a test can
tell WHICH definition a call that executed was served by.

Run:  python -m tegh.tests.toys.versioned_mcp_server <description file>
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ledger")

_DESCRIPTION = Path(sys.argv[1]).read_text(encoding="utf-8")


@mcp.tool(description=_DESCRIPTION)
def get_entry(entry_id: str) -> str:
    return f"{entry_id} served under: {_DESCRIPTION}"


@mcp.tool(description="Return up to `limit` ledger entry ids. Read-only.")
def list_entries(limit: int) -> str:
    return f"{limit} listed"


if __name__ == "__main__":
    mcp.run()
