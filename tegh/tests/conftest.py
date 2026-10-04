"""Fixtures shared across tegh's tests.

Two: `harness`, the project every real-wrap test starts from, and `wrapped`,
that project after a real wrap that relocated one credential. `harness` was
local to `test_wrap_end_to_end.py` until `test_call.py` needed the same
starting point; two copies of a fixture this size drift, and the second copy is
the one nobody remembers to update. `wrapped` moved here from `test_unwrap.py`
for the same reason. The plain helpers that go with them are in `wrapping.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tegh.tests.wrapping import (
    LEAKABLE_BROKER_ENV,
    LEDGER_FIELD,
    LEDGER_SECRET,
    REPO,
    TOY,
    give_ledger,
    scripted,
    store_of,
    wrap_answering,
)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A project whose harness config names the toy ledger server at local scope."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "widget"
    project.mkdir()

    claude_json = home / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "numStartups": 12,
                # Set so this is a COMPLETE wrap rather than one waived through
                # with --accept-gaps: claude.ai connectors are cloud-side and
                # survive any local rewrite, so with them live the gateway is
                # not the only server and the test would prove less than it says.
                "disableClaudeAiConnectors": True,
                "projects": {
                    str(project): {
                        "lastSessionId": "before-the-wrap",
                        "mcpServers": {
                            "ledger": {
                                "command": sys.executable,
                                "args": ["-m", TOY],
                            }
                        },
                    }
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("TEGH_HOME", str(tmp_path / "tegh"))
    monkeypatch.chdir(REPO)  # the toy is `python -m tegh.tests.toys...` from the repo root
    for var in LEAKABLE_BROKER_ENV:
        monkeypatch.delenv(var, raising=False)
    return {"home": home, "project": project, "claude_json": claude_json,
            "tegh_home": tmp_path / "tegh"}


@pytest.fixture
def wrapped(harness, capsys) -> dict:
    """The `harness` project, wrapped, with one credential relocated."""
    pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")
    from tegh.cli import main  # noqa: PLC0415 - after the skip, as the wrap needs it

    before = give_ledger(harness, env={LEDGER_FIELD: LEDGER_SECRET})
    assert main(["init"]) == 0
    # The empty answer classifies the value as a credential, which relocates it.
    assert wrap_answering(harness, scripted([""])) == 0
    capsys.readouterr()
    store = store_of(harness)
    stored = store.secrets_path(harness["project"]).read_text(encoding="utf-8")
    assert LEDGER_SECRET in stored
    return {**harness, "before": before, "store": store, "root": harness["home"].parent}
