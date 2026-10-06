"""versioned_mcp_server — the toy ledger with a description that can change.

The same two read-only tools as `restricted_mcp_server`, except that one of
them advertises whatever description is in the file named on the command
line, read when the server starts. A test rewrites that file to stand for a
server that changed a tool's definition after it was admitted, which is the
one thing the fixed toy cannot do. The tool is `get_entry` unless a second
argument names `list_entries`, for a test where the definition that changes
must not be the first one a wrap admits.

That tool answers with the description it is running under, so a test can
tell WHICH definition a call that executed was served by.

Run:  python -m tegh.tests.toys.versioned_mcp_server <description file> [tool]
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ledger")

_DESCRIPTION = Path(sys.argv[1]).read_text(encoding="utf-8")
_VERSIONED = sys.argv[2] if len(sys.argv) > 2 else "get_entry"
_FIXED = {
    "get_entry": "Return one ledger entry by id. Read-only.",
    "list_entries": "Return up to `limit` ledger entry ids. Read-only.",
}


def _described(tool: str) -> str:
    return _DESCRIPTION if tool == _VERSIONED else _FIXED[tool]


def _answer(tool: str, said: str) -> str:
    return f"{said} served under: {_DESCRIPTION}" if tool == _VERSIONED else said


@mcp.tool(description=_described("get_entry"))
def get_entry(entry_id: str) -> str:
    return _answer("get_entry", entry_id)


@mcp.tool(description=_described("list_entries"))
def list_entries(limit: int) -> str:
    return _answer("list_entries", f"{limit} listed")


if __name__ == "__main__":
    mcp.run()
