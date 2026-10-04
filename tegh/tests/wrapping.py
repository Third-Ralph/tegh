"""Helpers shared by the tests that drive a REAL wrap (`harness` in `conftest.py`).

`test_wrap_end_to_end.py` and `test_call.py` both stand on the same thing: a
`tmp_path` harness home naming the toy ledger server, a real `tegh init` and
`tegh wrap`, and a real gateway spawned from what the wrap wrote. The fixture
lives in `conftest.py` because pytest finds fixtures there; these are plain
functions, so they live in a module a test imports by name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tegh.cli import _parse_args, main, wrap_command
from tegh.store import TeghStore

REPO = Path(__file__).resolve().parents[2]
TOY = "tegh.tests.toys.restricted_mcp_server"

#: Broker configuration a developer's shell may have exported. Cleared before a
#: wrap test so it runs on what tegh names and never on what leaked in.
LEAKABLE_BROKER_ENV = (
    "BROKER_STORE", "BROKER_SQLITE_PATH", "BROKER_MANIFEST", "BROKER_HMAC_KEY",
    "BROKER_AUDIT_PATH", "BROKER_SECRETS", "BROKER_SECRETS_FILE",
    "BROKER_GRANT_LOAD", "BROKER_ENVELOPE_LOAD", "BROKER_LOCAL_IDENTITY",
    "ISSUER_SIGNING_KEY_SECRET_ARN",
)

#: The review script that corrects one tool to a read and admits it:
#: edit -> effect=read -> keep egress_arg -> admit. The toy has two tools.
ADMIT_AS_READ = ("e", "read", "", "y")
TOY_TOOL_COUNT = 2


def minimal_child_env() -> dict[str, str]:
    """What a harness actually hands a spawned stdio child: essentially PATH.

    Deliberately NOT `os.environ`. Passing the parent environment is what hid a
    real defect: the gateway entry named no tegh home, so it resolved TEGH_HOME
    from the spawning process, which worked in a test that leaked its own env
    and failed for a real operator, whose harness passes almost nothing. The
    entry must be self-contained, and this is what proves it.
    """
    return {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)}


def wrap_admit_all(harness: dict) -> int:
    return main(
        [
            "wrap", "claude",
            "--project", str(harness["project"]),
            "--harness-home", str(harness["home"]),
            "--admit-all",
        ]
    )


def scripted(answers: list[str]):
    """A review prompt that replays a fixed script, and says so when it runs dry.

    Raising on exhaustion rather than returning "": an empty answer is `skip`,
    so a script that fell short would quietly admit nothing and the test would
    fail somewhere far away with a misleading symptom.
    """
    remaining = list(answers)

    def _prompt(text: str) -> str:
        if not remaining:
            raise AssertionError(f"review asked more than the script answers: {text!r}")
        return remaining.pop(0)

    return _prompt


def wrap_as_reads(harness: dict) -> int:
    """Wrap with a human correcting both toy tools to reads (the correction flow).

    `--admit-all` takes the proposal, and an unannotated server's tools are
    proposed as irreversible writes, which the broker holds. This is the wrap
    under which a call to the toy EXECUTES.
    """
    return wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
            ]
        ),
        prompt=scripted(list(ADMIT_AS_READ) * TOY_TOOL_COUNT),
    )


def written_gateway_entry(harness: dict) -> dict:
    """The `tegh` entry a wrap wrote into the harness config, read back off disk."""
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    return document["projects"][str(harness["project"])]["mcpServers"]["tegh"]


def tape_records(harness: dict) -> list[dict]:
    """Every record on the project's audit tape, by path; `[]` if none was written."""
    tapes = list((harness["tegh_home"] / "projects").glob("*/audit.jsonl"))
    if not tapes:
        return []
    return [
        json.loads(line)
        for line in tapes[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def give_ledger(harness: dict, **blocks: dict) -> bytes:
    """Put literal `env`/`headers` values on the toy server. Returns the new bytes.

    Written with the same `indent=2, ensure_ascii=False` the fixture and
    `interpose` use, so the format round-trip tegh proves before it will touch
    a config still holds — a test whose setup could not survive that proof would
    be exercising the refusal, not the feature.
    """
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    document["projects"][str(harness["project"])]["mcpServers"]["ledger"].update(blocks)
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return harness["claude_json"].read_bytes()


def wrap_answering(harness: dict, prompt) -> int:
    """`wrap_admit_all`, but with a seam for the prompt. `--admit-all` covers only tools.

    `--admit-all` is deliberately not an answer to a credential question, so a
    wrap of a project with literal config values is interactive even under it.
    That is the reason this helper exists rather than `main`.
    """
    return wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
                "--admit-all",
            ]
        ),
        prompt=prompt,
    )


def store_of(harness: dict) -> TeghStore:
    return TeghStore(home=harness["tegh_home"])
