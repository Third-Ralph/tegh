"""Fixtures shared across tegh's tests.

One so far: `harness`, the project every real-wrap test starts from. It was
local to `test_wrap_end_to_end.py` until `test_call.py` needed the same
starting point; two copies of a fixture this size drift, and the second copy is
the one nobody remembers to update. The plain helpers that go with it are in
`wrapping.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tegh.tests.wrapping import LEAKABLE_BROKER_ENV, REPO, TOY


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
