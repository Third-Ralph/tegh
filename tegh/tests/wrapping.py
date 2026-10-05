"""Helpers shared by the tests that drive a REAL wrap (`harness` in `conftest.py`).

`test_wrap_end_to_end.py` and `test_call.py` both stand on the same thing: a
`tmp_path` harness home naming the toy ledger server, a real `tegh init` and
`tegh wrap`, and a real gateway spawned from what the wrap wrote. The fixture
lives in `conftest.py` because pytest finds fixtures there; these are plain
functions, so they live in a module a test imports by name.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from tegh import configvalues, interpose
from tegh.cli import _parse_args, main, unwrap_command, wrap_command
from tegh.harnesses import claude_code
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


def wrap_admit_all(harness: dict, *flags: str) -> int:
    return main(
        [
            "wrap", "claude",
            "--project", str(harness["project"]),
            "--harness-home", str(harness["home"]),
            "--admit-all",
            *flags,
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


# ---------------------------------------------------------------------------
# What the unwrap tests share
# ---------------------------------------------------------------------------

#: The credential the `wrapped` fixture relocates. Obviously not one.
LEDGER_SECRET = "not-a-real-credential-ledger-fixture"
LEDGER_FIELD = "LEDGER_API_KEY"


def files_holding(root: Path, value: str) -> list[Path]:
    """Every file under `root` whose bytes contain `value`."""
    needle = value.encode("utf-8")
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and needle in path.read_bytes()
    ]


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under `root`, by content. Equal means nothing changed."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def unwrap_cli(state: dict, *flags: str, prompt=None) -> int:
    """`tegh unwrap` for the project in `state`, in process, through the CLI seam."""
    return unwrap_command(
        _parse_args(["unwrap", *flags, "--project", str(state["project"])]),
        prompt=prompt,
    )


def site_files(
    tmp_path: Path, *, local, project_scope, user, harness_config: bool = True
) -> tuple[Path, Path, Path]:
    """A harness home and project holding the given `mcpServers` blocks.

    `None` means the scope has no block at all, which is the usual state of two
    of the three. `harness_config=False` leaves `.claude.json` out altogether:
    the home of a harness that has never written its config.
    """
    project = tmp_path / "widget"
    project.mkdir()
    document: dict = {"projects": {str(project): {"lastSessionId": "abc"}}}
    if local is not None:
        document["projects"][str(project)]["mcpServers"] = local
    if user is not None:
        document["mcpServers"] = user
    claude_json = tmp_path / ".claude.json"
    if harness_config:
        claude_json.write_text(
            json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    project_mcp = project / ".mcp.json"
    if project_scope is not None:
        project_mcp.write_text(
            json.dumps({"mcpServers": project_scope}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return project, claude_json, project_mcp


def interposed(
    tmp_path: Path,
    monkeypatch,
    *,
    local=None,
    project_scope=None,
    user=None,
    config_mode: int | None = None,
    harness_config: bool = True,
) -> dict:
    """The state a wrap leaves behind, reached without the admission ceremony.

    An unwrap reads three things: the backup, the credential map and the sites.
    This writes all three with the calls `tegh wrap` itself makes, in its order
    (`cli._classify_config_values` then `cli._interpose`), and treats EVERY
    literal `env` value as a credential the human relocated. What it skips is
    the snapshot, the review and the grants, none of which an unwrap consults,
    so a case that needs three scopes and four credentials costs milliseconds
    and no MCP server. `test_unwrap.py`'s `wrapped` fixture is the real wrap.

    `config_mode` sets the permission bits of each config file BEFORE the wrap
    touches it. Returns the paths, the store, each config's pre-wrap bytes
    (`None` for a file that did not exist) and the relocated values.
    """
    project, claude_json, project_mcp = site_files(
        tmp_path, local=local, project_scope=project_scope, user=user,
        harness_config=harness_config,
    )
    if config_mode is not None:
        for config in (claude_json, project_mcp):
            if config.exists():
                config.chmod(config_mode)
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (claude_json, project_mcp)
    }
    tegh_home = tmp_path / "tegh"
    monkeypatch.setenv("TEGH_HOME", str(tegh_home))
    store = TeghStore(home=tegh_home)
    sites, gateway_site = claude_code.config_sites(
        project, claude_json_path=claude_json, project_mcp_path=project_mcp
    )

    relocated: dict[str, str] = {}
    cleared: dict[str, str] = {}
    secrets: list[str] = []
    for site in sites:
        block = interpose.read_block(site)
        for found in configvalues.inventory(block, scope=site.scope.value):
            relocated[found.coordinate] = found.server_id
            cleared[found.coordinate] = found.value_sha256
        for server_id, entry in block.items():
            values = {k: v for k, v in entry.get("env", {}).items() if isinstance(v, str)}
            if values:
                store.write_secret_leaf(project, server_id, values)
                secrets.extend(values.values())

    backup = interpose.apply_interposition(
        interpose.plan_interposition(
            sites=sites,
            gateway_site=gateway_site,
            # As a wrap writes it. An unwrap finds the wrap's entry by the
            # command it runs, so a stand-in would be kept as the user's server.
            gateway_entry=claude_code.gateway_entry(
                project, launcher=["tegh"], home=tegh_home
            ),
            cleared_config=cleared,
        ),
        wrapped_at="2026-10-04T00:00:00+00:00",
        project=project,
        harness="claude-code",
        relocated=relocated,
    )
    backup_path = store.backup_path(project)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(backup.to_json(), encoding="utf-8")
    return {
        "root": tmp_path, "project": project, "claude_json": claude_json,
        "project_mcp": project_mcp, "store": store, "originals": originals,
        "secrets": secrets, "tegh_home": tegh_home,
    }
