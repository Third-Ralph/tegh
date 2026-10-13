"""`tegh upgrade`: a project wrapped by tegh 0.3.0, brought to this version with its tape.

A tegh home as 0.3.0 left it cannot be checked in (it holds keys), so the
decisive case builds one. It wraps the toy ledger with this tegh, with the
reserved `harness-tool` coordinates taken out of the table for the wrap, so the
store holds grants for the two MCP tools and nothing else, as 0.3.0's did.
0.3.0's grants are stamped under the hash of an envelope with `allowlists` in
it, which the platform no longer loads, so no wrap now can stamp one. The
nearest a test can come is an envelope the upgrade will not write again: the
wrap's carries `max_query_bytes`, which bounds nothing here because no admitted
tool names an egress argument, and the rewrite below takes it out again. (A
`tainted_harness_hold` would do it too, but the platform refuses one that lists
a class no `harness-tool` entry declares.) A call goes through the gateway and
lands on the tape. Then the manifest, the hook entries and the wrap
backup are rewritten into the shapes 0.3.0 wrote: `allowlists` in the envelope,
no `tainted_harness_hold`, ToolOps dumped before the platform added
`causes_context`, one `PostToolUse` entry, and a backup recording that one.

From there the gateway refuses with tegh's sentence and not the platform's,
`tegh upgrade` says what it will do, does it, and the same call executes again
on a tape that verifies across the upgrade.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tegh import cli, hooksite, upgrade
from tegh.cli import _parse_args, _synthesize_manifest, _write_manifest, main, wrap_command
from tegh.harnesses import claude_code
from tegh.interpose import _render
from tegh.store import TeghStore
from tegh.tests.wrapping import (
    LEDGER_FIELD,
    LEDGER_SECRET,
    REPO,
    give_ledger,
    scripted,
    store_of,
)

pytest.importorskip("mcp", reason="the gateway and the toy server use the MCP SDK")

from safe_agents.broker.api import GatewayClient, result_text  # noqa: E402
from safe_agents.broker.schemas import ToolOp  # noqa: E402
from safe_agents.broker.schemas.harness_tool import HARNESS_TOOL  # noqa: E402

from tegh import hook  # noqa: E402
from tegh.launch import gateway_argv, harness_spawn_env, tegh_launcher  # noqa: E402
from tegh.tests.test_hook import pre_event  # noqa: E402

#: The credential question, answered "credential", then each toy tool corrected
#: to a reversible external write and admitted (`test_hook_e2e.py`), so an
#: untainted call to it executes.
ANSWERS = ["", *(("e", "", "y", "", "y") * 2)]
TOY_TOOLS = ("get_entry", "list_entries")
WRITE_TOOL = "ledger__get_entry"
WRITE_ARGS = {"entry_id": "L-001"}
EXECUTED = ("ledger", "get_entry", "allow", "executed")
#: The envelope bound the wrap's grants are stamped under and the upgrade does
#: not write again. See the module docstring.
#: A `[re-seed] OK` line: the class, its level, the new hash and the old one.
REATTESTED = re.compile(
    r"\[re-seed\] OK +(\S+): re-attested at level '([a-z-]+)' under (\S+) \(was (\S+)\)"
)
#: The level the platform's `seed` gives every grant it issues
#: (`safe_agents/broker/prototype/broker_server.py::_make_grant`).
SEEDED_LEVEL = "on-loop"
INERT_BOUND = "max_query_bytes"
INERT_BOUND_VALUE = 65536


# ---------------------------------------------------------------------------
# Building 0.3.0's state
# ---------------------------------------------------------------------------


def _wrap(harness: dict, monkeypatch, capfd, *, with_harness_coordinates: bool) -> None:
    assert main(["init"]) == 0
    give_ledger(harness, env={LEDGER_FIELD: LEDGER_SECRET})
    synthesize = cli._synthesize_manifest

    def with_an_inert_bound(*args, **kwargs):
        payload = synthesize(*args, **kwargs)
        payload["envelope"][INERT_BOUND] = INERT_BOUND_VALUE
        return payload

    with monkeypatch.context() as patched:
        if not with_harness_coordinates:
            patched.setattr(cli, "HARNESS_TOOL_OPS", ())
        patched.setattr(cli, "_synthesize_manifest", with_an_inert_bound)
        status = wrap_command(
            _parse_args(
                [
                    "wrap", "claude",
                    "--project", str(harness["project"]),
                    "--harness-home", str(harness["home"]),
                ]
            ),
            prompt=scripted(list(ANSWERS)),
        )
    out, _ = capfd.readouterr()
    assert status == 0, out


def _call_through_the_gateway(harness: dict) -> dict:
    store = store_of(harness)
    command = gateway_argv(harness["project"], launcher=tegh_launcher(), home=store.home)
    client = GatewayClient(command, env=harness_spawn_env(), timeout=120)
    try:
        client.initialize(client_name="tegh-upgrade-test")
        return client.call_tool(WRITE_TOOL, WRITE_ARGS)
    finally:
        client.close()


def _as_0_3_0(harness: dict) -> str:
    """Rewrite the manifest, the hook entries and the backup into 0.3.0's shapes.

    Returns the manifest text as 0.3.0 would have left it.
    """
    store = store_of(harness)
    project = harness["project"]
    path = store.manifest_path(project)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert not [op for op in manifest["tool_ops"] if op["tool"] == HARNESS_TOOL]
    assert manifest["envelope"].pop(INERT_BOUND) == INERT_BOUND_VALUE
    manifest["envelope"]["allowlists"] = {"tools": list(manifest["grant_classes"])}
    manifest["envelope"].pop("tainted_harness_hold")
    for op in manifest["tool_ops"]:
        op.pop("causes_context")
    old = yaml.safe_dump(manifest, sort_keys=True)
    path.write_text(old, encoding="utf-8")

    site = claude_code.hook_site(project).path
    settings = json.loads(site.read_text(encoding="utf-8"))
    del settings["hooks"][hooksite.PRE_TOOL_USE]
    site.write_text(_render(settings) + "\n", encoding="utf-8")

    backup_path = store.backup_path(project)
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    recorded = backup["hook"]
    post = recorded.pop("events")[hooksite.POST_TOOL_USE]
    recorded.update(
        event=hooksite.POST_TOOL_USE, event_existed=post["existed"], added=post["added"]
    )
    backup_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
    return old


def _written(store: TeghStore, project: Path) -> dict[str, bytes]:
    """Every file in the project's tegh directory (store, tape, manifest), by content."""
    return {
        path.name: path.read_bytes()
        for path in sorted(store.project_dir(project).iterdir())
        if path.is_file()
    }


def _tape(capfd, project: Path) -> list[tuple]:
    capfd.readouterr()
    assert main(["audit", "--json", "--project", str(project)]) == 0
    records = json.loads(capfd.readouterr().out)["records"]
    return [(r["tool"], r["op"], r["decision"], r["outcome"]) for r in records]


def _verified(capfd, project: Path) -> str:
    capfd.readouterr()
    assert main(["audit", "--verify", "--project", str(project)]) == 0
    return capfd.readouterr().out


def _gateway_process(harness: dict) -> subprocess.CompletedProcess:
    """`tegh gateway` as the harness starts it, but as a child: a gateway that started
    would replace this process if run in it."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable, "-m", "tegh.entry", "gateway",
            "--project", str(harness["project"]),
            "--home", str(harness["tegh_home"]),
        ],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
        timeout=60,
    )


@pytest.fixture
def wrapped_by_0_3_0(harness: dict, monkeypatch, capfd) -> dict:
    _wrap(harness, monkeypatch, capfd, with_harness_coordinates=False)
    before = _call_through_the_gateway(harness)
    assert before["isError"] is False, result_text(before)
    store = store_of(harness)
    consumer = [f"ledger.{name}" for name in TOY_TOOLS]
    old = _as_0_3_0(harness)
    return {**harness, "store": store, "old": old, "consumer": consumer}


# ---------------------------------------------------------------------------
# The decisive case
# ---------------------------------------------------------------------------


def test_a_0_3_0_wrap_is_upgraded_through_the_ceremony_keeping_its_tape(
    wrapped_by_0_3_0, capfd
):
    harness = wrapped_by_0_3_0
    store, project = harness["store"], harness["project"]
    named = store.named_as(project)

    # Refused with tegh's sentence, which names the command that fixes it.
    refused = _gateway_process(harness)
    assert refused.returncode == 2, refused.stderr
    assert f"Run `tegh upgrade {named}`" in refused.stderr
    assert "its audit tape is kept" in refused.stderr

    capfd.readouterr()
    assert main(["upgrade", "--project", str(project), "--yes"]) == 0
    out, err = capfd.readouterr()
    assert err == ""

    # The plan: three things out of date, the ceremony, the hook.
    assert "the envelope carries `allowlists`" in out
    assert "16 of the 16 `harness-tool` coordinates" in out
    assert "declares no `tainted_harness_hold`" in out
    assert "seed      16 new grant class(es)" in out
    assert "re-seed   2 existing grant(s)" in out
    assert "add tegh's PreToolUse entry to" in out
    # The ceremony, in the platform's own lines.
    assert out.count("[seed] OK") == 16
    assert out.count("[seed] SKIP") == len(harness["consumer"])
    assert out.count("[re-seed] OK") == len(harness["consumer"])
    assert out.count("[re-seed] SKIP") == 16
    assert "[re-seed] FAIL" not in out and "REFUSED" not in out

    # The copy is the manifest as 0.3.0 left it, and the marker is gone.
    copy = upgrade.copy_path_for(store.manifest_path(project))
    assert copy.read_text(encoding="utf-8") == harness["old"]
    assert not upgrade.pending_path(store, project).exists()

    # Each consumer grant was re-attested at the level the wrap's `seed` gave it,
    # under a hash other than the one it was stamped with. Read off the
    # platform's own line: tegh's import boundary (`test_lock.py`) keeps the
    # grant store out of reach, tests included, and the platform's store refuses
    # a reattestation that moves the level.
    reattested = {found[0]: found[1:] for found in REATTESTED.findall(out)}
    assert sorted(reattested) == sorted(harness["consumer"])
    for level, new_hash, old_hash in reattested.values():
        assert level == SEEDED_LEVEL and new_hash != old_hash

    # The pre-call hook is in place, recorded in the backup, and answered.
    settings = json.loads(claude_code.hook_site(project).path.read_text(encoding="utf-8"))
    (group,) = settings["hooks"][hooksite.PRE_TOOL_USE]
    (entry,) = group["hooks"]
    assert entry["onFailure"] == hooksite.ON_FAILURE_BLOCK
    (post,) = settings["hooks"][hooksite.POST_TOOL_USE]
    assert entry["command"] == post["hooks"][0]["command"]
    backup = json.loads(store.backup_path(project).read_text(encoding="utf-8"))
    assert backup["hook"]["events"][hooksite.PRE_TOOL_USE] == {"existed": False, "added": True}

    # The same call executes again, and the tape verifies across the upgrade.
    client = GatewayClient(
        gateway_argv(project, launcher=tegh_launcher(), home=store.home),
        env=harness_spawn_env(),
        timeout=120,
    )
    try:
        client.initialize(client_name="tegh-upgrade-test")
        result = client.call_tool(WRITE_TOOL, WRITE_ARGS)
        decided = subprocess.run(  # noqa: S603 - the command the upgrade wrote
            ["/bin/sh", "-c", entry["command"]],
            input=json.dumps(
                pre_event(
                    cwd=str(project),
                    tool_name="Read",
                    tool_input={"file_path": str(project / "README.md")},
                )
            ),
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(harness["home"])},
            cwd=REPO,
            check=False,
            timeout=60,
        )
    finally:
        client.close()
    assert result["isError"] is False, result_text(result)
    assert (decided.returncode, decided.stdout.strip()) == (0, hook.allow_output())
    tape = _tape(capfd, project)
    assert tape == [EXECUTED, EXECUTED, (HARNESS_TOOL, "file-read/project", "allow", "permitted")]
    assert "CHAIN CONSISTENT" in _verified(capfd, project)

    # Idempotent: re-seed checks again, skips all 18, and nothing is written.
    before = _written(store, project)
    assert main(["upgrade", "--project", str(project), "--yes"]) == 0
    again, _ = capfd.readouterr()
    assert "\nnothing to upgrade: " in again
    assert again.count("[re-seed] SKIP") == 16 + len(harness["consumer"])
    assert "[re-seed] OK" not in again and "[seed]" not in again
    assert _written(store, project) == before

    # And an unwrap takes out both hook entries, the one the upgrade added too.
    assert main(["unwrap", "--project", str(project), "--yes"]) == 0
    assert not claude_code.hook_site(project).path.exists()


def test_the_other_commands_name_the_upgrade(wrapped_by_0_3_0, capfd):
    harness = wrapped_by_0_3_0
    store, project = harness["store"], harness["project"]
    capfd.readouterr()

    assert main(["approve", "intent-x", "--project", str(project), "--yes"]) == 2
    _, err = capfd.readouterr()
    assert f"Run `tegh upgrade {store.named_as(project)}`" in err

    assert main(["call", WRITE_TOOL, "--project", str(project)]) == 2
    _, err = capfd.readouterr()
    assert f"Run `tegh upgrade {store.named_as(project)}`" in err

    assert main(["diff", "--project", str(project)]) == 2
    _, err = capfd.readouterr()
    assert f"Run `tegh upgrade {store.named_as(project)}`" in err

    assert main(["status", "--project", str(project)]) == 0
    out, _ = capfd.readouterr()
    assert "NOT SERVED:" in out and "tegh upgrade" in out
    assert "run `tegh upgrade` to add the PreToolUse hook" in out

    assert main(
        ["posture", "--json", "--project", str(project), "--harness-home", str(harness["home"])]
    ) == 0
    report = json.loads(capfd.readouterr().out)
    (line,) = [
        line for line in report["store"] if "tegh upgrade" in (line.get("detail") or "")
    ]
    assert line["holds"] == "unknown"
    (built_ins,) = [line for line in report["gaps"] if "are OBSERVED" in line["claim"]]
    assert "Run `tegh upgrade` for this project to add the PreToolUse hook" in built_ins["detail"]


@pytest.mark.parametrize("failing", ["seed", "re-seed"])
def test_a_failed_ceremony_step_says_what_was_written_and_a_second_run_finishes(
    wrapped_by_0_3_0, capfd, monkeypatch, failing
):
    """The second run finds the manifest current and the marker set, so it runs
    `seed` as well as `re-seed`: with `seed` the step that failed, the sixteen
    new classes have no grants until it does."""
    harness = wrapped_by_0_3_0
    store, project = harness["store"], harness["project"]
    real = upgrade._ceremony

    def fail_one(store_, project_, command):
        if command == failing:
            return subprocess.CompletedProcess([command], 1, "", f"[{command}] FAIL stand-in\n")
        return real(store_, project_, command)

    capfd.readouterr()
    with monkeypatch.context() as patched:
        patched.setattr(upgrade, "_ceremony", fail_one)
        assert main(["upgrade", "--project", str(project), "--yes"]) == 1
    out, err = capfd.readouterr()
    assert f"UPGRADE NOT FINISHED: `{failing}` failed" in err
    assert "is this version's, and the one it replaced is at" in err
    assert f"Run `tegh upgrade {store.named_as(project)}` again" in err
    assert upgrade.pending_path(store, project).exists()

    assert main(["upgrade", "--project", str(project), "--yes"]) == 0
    out, _ = capfd.readouterr()
    assert "already this version's" in out
    assert out.count("[seed] OK") == (16 if failing == "seed" else 0)
    assert out.count("[re-seed] OK") == len(harness["consumer"])
    assert not upgrade.pending_path(store, project).exists()


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self, server_id, transport="stdio", command=None, args=(), cwd=None, url=None):
        self.server_id, self.transport = server_id, transport
        self.command, self.args, self.cwd, self.url = command, args, cwd, url


def _op(server: str, tool: str, **facts) -> ToolOp:
    return ToolOp(tool=server, op=tool, **{"effect": "read", "external": True, **facts})


_RICH = {
    "servers": [
        _Server("notes", command="/usr/bin/notes", args=("--root", "/srv/notes"), cwd="/srv"),
        _Server("search", transport="http", url="https://search.example/mcp"),
        _Server("idle", command="/usr/bin/idle"),
    ],
    "tools_by_server": {
        "notes": [
            _op("notes", "read_note", egress_arg="query"),
            _op("notes", "write_note", effect="write", reversible=True),
        ],
        "search": [_op("search", "query", egress_arg="q")],
    },
    "env_by_server": {"notes": {"NOTES_FILE": "/srv/notes/db.json"}},
    "credentials_by_server": {"notes": {"NOTES_TOKEN": "x"}, "search": {"API_KEY": "y"}},
}


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param({}, id="every value"),
        pytest.param({"named": {("notes", "read_note")}}, id="namespace names one tool"),
        pytest.param({"named": set()}, id="namespace names none yet"),
        pytest.param({"hold_on_taint": ("shell", "web-fetch")}, id="hold listed"),
        pytest.param({"trusted_read_sources": ()}, id="no trusted read"),
        pytest.param({"credentials_by_server": {}}, id="no credential"),
    ],
)
def test_a_manifest_this_tegh_wrote_is_a_fixed_point(tmp_path: Path, variant: dict):
    project = tmp_path / "project"
    payload = _synthesize_manifest(
        **{**_RICH, **variant}, polarity="act", project=project, daily_cap=7
    )
    path = tmp_path / "manifest.yaml"
    _write_manifest(path, payload)
    text = path.read_text(encoding="utf-8")

    stored = upgrade.read_stored(path)
    regenerated = upgrade.regenerate(stored, project)
    assert yaml.safe_dump(regenerated, sort_keys=True) == text
    assert upgrade.out_of_date(stored, regenerated) == []
    assert upgrade.unexplained_differences(stored, regenerated) == []


def test_a_value_regeneration_would_change_is_refused_by_name(tmp_path: Path):
    project = tmp_path / "project"
    payload = _synthesize_manifest(
        **_RICH, polarity="abstain", project=project, daily_cap=5
    )
    payload["envelope"]["max_query_bytes"] = 64
    payload["mcp_servers"]["notes"]["timeout"] = 30
    stored = yaml.safe_load(yaml.safe_dump(payload))
    regenerated = upgrade.regenerate(stored, project)
    assert upgrade.unexplained_differences(stored, regenerated) == [
        "envelope.max_query_bytes",
        "mcp_servers",
    ]


@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        pytest.param({"allowlists": {"tools": ["a.b"]}}, True, id="with tools"),
        pytest.param({"allowlists": None}, True, id="null: presence is the test"),
        pytest.param({"allowlists": {}}, True, id="empty"),
        pytest.param({"polarity": "act"}, False, id="absent"),
    ],
)
def test_the_detector_reads_presence(tmp_path: Path, envelope: dict, expected: bool):
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"envelope": envelope}), encoding="utf-8")
    assert upgrade.carries_retired_key(path) is expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("envelope: [", id="does not parse"),
        pytest.param("- allowlists\n", id="not a mapping"),
        pytest.param("envelope: allowlists\n", id="envelope not a mapping"),
    ],
)
def test_the_detector_leaves_what_it_cannot_read_to_the_platform(tmp_path: Path, text: str):
    path = tmp_path / "manifest.yaml"
    path.write_text(text, encoding="utf-8")
    assert upgrade.carries_retired_key(path) is False


@pytest.mark.parametrize(
    ("text", "said"),
    [
        pytest.param("envelope: [", "does not parse as YAML", id="does not parse"),
        pytest.param("- 1\n", "is not a YAML mapping", id="not a mapping"),
        pytest.param(
            "principal: {}\nenvelope: {}\n",
            "names no `tool_ops`, `grant_classes`, `mcp_servers`",
            id="keys missing",
        ),
    ],
)
def test_a_stored_manifest_it_cannot_regenerate_is_refused(tmp_path: Path, text, said):
    path = tmp_path / "manifest.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(upgrade.UpgradeRefused, match=said):
        upgrade.read_stored(path)


@pytest.mark.parametrize(
    ("drop", "said"),
    [
        pytest.param(
            ("tainted_harness_hold",),
            ["allowlists", "harness-tool", "tainted_harness_hold"],
            id="as 0.3.0 wrote it",
        ),
        pytest.param(
            ("tainted_harness_hold", "trusted_read_sources"),
            ["allowlists", "harness-tool", "tainted_harness_hold", "trusts no built-in read"],
            id="as 0.2.0 wrote it",
        ),
    ],
)
def test_an_earlier_manifest_regenerates_to_the_current_one(tmp_path: Path, drop, said):
    project = tmp_path / "project"
    current = _synthesize_manifest(**_RICH, polarity="act", project=project, daily_cap=7)
    stored = yaml.safe_load(yaml.safe_dump(current))
    stored["envelope"]["allowlists"] = {"tools": stored["grant_classes"]}
    for key in drop:
        stored["envelope"].pop(key)
    stored["tool_ops"] = [op for op in stored["tool_ops"] if op["tool"] != HARNESS_TOOL]
    stored["grant_classes"] = [
        name for name in stored["grant_classes"] if not name.startswith(HARNESS_TOOL)
    ]
    regenerated = upgrade.regenerate(stored, project)
    assert upgrade.unexplained_differences(stored, regenerated) == []
    assert yaml.safe_dump(regenerated, sort_keys=True) == yaml.safe_dump(current, sort_keys=True)
    findings = upgrade.out_of_date(stored, regenerated)
    assert len(findings) == len(said)
    for finding, word in zip(findings, said):
        assert word in finding


def test_tegh_upgrade_refuses_a_manifest_it_would_change_and_writes_nothing(
    tmp_path: Path, capsys
):
    home, project = tmp_path / "tegh", tmp_path / "project"
    project.mkdir()
    assert main(["init", "--home", str(home)]) == 0
    store = TeghStore(home=home)
    payload = _synthesize_manifest(**_RICH, polarity="abstain", project=project, daily_cap=5)
    payload["envelope"]["allowlists"] = {"tools": payload["grant_classes"]}
    payload["envelope"]["max_query_bytes"] = 64
    _write_manifest(store.manifest_path(project), payload)
    store.mark_layout(project)
    before = sorted(path.name for path in store.project_dir(project).iterdir())
    manifest_bytes = store.manifest_path(project).read_bytes()
    capsys.readouterr()

    assert main(["upgrade", "--project", str(project), "--home", str(home), "--yes"]) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("REFUSED: ") and "at envelope.max_query_bytes," in err
    assert store.manifest_path(project).read_bytes() == manifest_bytes
    assert sorted(path.name for path in store.project_dir(project).iterdir()) == before


_HOOK_COMMAND = "/usr/bin/tegh hook --project /p --home /h"


def _hook_state(tmp_path: Path, *, recorded: dict, in_file: tuple[str, ...]) -> TeghStore:
    """A tegh home whose wrap backup records `recorded` and whose settings hold `in_file`."""
    home, project = tmp_path / "tegh", tmp_path / "project"
    store = TeghStore(home=home)
    settings = project / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        _render({"hooks": {event: [hooksite.hook_entry(_HOOK_COMMAND, event)] for event in in_file}})
        + "\n",
        encoding="utf-8",
    )
    backup = {
        "project": str(project), "harness": "claude-code", "wrapped_at": "2026-10-01T00:00:00+00:00",
        "sites": [],
        "hook": {
            "path": str(settings), "command": _HOOK_COMMAND, "file_existed": False,
            "hooks_existed": False, "dir_existed": False, **recorded,
        },
    }
    store.backup_path(project).parent.mkdir(parents=True)
    store.backup_path(project).write_text(json.dumps(backup), encoding="utf-8")
    return store


_AS_0_3_0 = {"event": hooksite.POST_TOOL_USE, "event_existed": False, "added": True}
_BOTH = {"events": {
    hooksite.PRE_TOOL_USE: {"existed": False, "added": True},
    hooksite.POST_TOOL_USE: {"existed": False, "added": True},
}}


@pytest.mark.parametrize(
    ("recorded", "in_file", "adds", "records"),
    [
        pytest.param(_AS_0_3_0, (hooksite.POST_TOOL_USE,), True, True, id="as 0.3.0 left it"),
        pytest.param(_BOTH, (hooksite.POST_TOOL_USE,), True, False, id="recorded, not added yet"),
        pytest.param(_AS_0_3_0, hooksite.HOOK_EVENTS, False, True, id="added, not recorded"),
        pytest.param(_BOTH, hooksite.HOOK_EVENTS, None, None, id="both done"),
        pytest.param(_AS_0_3_0, (), None, None, id="PostToolUse entry removed"),
    ],
)
def test_the_hook_step_finishes_whichever_half_is_missing(
    tmp_path: Path, recorded, in_file, adds, records
):
    store = _hook_state(tmp_path, recorded=recorded, in_file=in_file)
    found = upgrade.plan_hook_upgrade(store, tmp_path / "project")
    if adds is None:
        assert found.upgrade is None
        assert (found.note is not None) == (in_file == ())
        return
    assert bool(found.upgrade.install.adds) is adds
    assert (found.upgrade.backup is not None) is records
    upgrade.apply_hook_upgrade(found.upgrade)
    again = upgrade.plan_hook_upgrade(store, tmp_path / "project")
    assert again.upgrade is None and again.note is None


def test_a_current_manifest_under_a_moved_envelope_hash_is_re_attested(
    harness: dict, monkeypatch, capfd
):
    """A platform pin move: every grant under another hash, the manifest current.

    The wrap's envelope carries an inert bound and the stored file then loses
    it, which leaves the file exactly as this tegh writes it and moves the hash
    in force away from the one every grant was stamped under. That is what a
    platform release that changes the envelope's dump does to a project.
    """
    _wrap(harness, monkeypatch, capfd, with_harness_coordinates=True)
    store, project = store_of(harness), harness["project"]
    path = store.manifest_path(project)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert manifest["envelope"].pop(INERT_BOUND) == INERT_BOUND_VALUE
    _write_manifest(path, manifest)
    stored = upgrade.read_stored(path)
    assert upgrade.out_of_date(stored, upgrade.regenerate(stored, project)) == []

    quarantined = _call_through_the_gateway(harness)
    assert quarantined["isError"] is True, result_text(quarantined)

    capfd.readouterr()
    assert main(["upgrade", "--project", str(project), "--yes"]) == 0
    out, err = capfd.readouterr()
    assert err == ""
    assert "is current; nothing to rewrite" in out and "  seed  " not in out
    granted = 16 + len(TOY_TOOLS)
    assert out.count("[re-seed] OK") == granted
    assert f"manifest current; {granted} grants re-attested, 0 already under the in-force hash" in out
    assert _call_through_the_gateway(harness)["isError"] is False

    before = _written(store, project)
    assert {"store.db", "audit.jsonl", "manifest.yaml"} <= set(before)
    assert main(["upgrade", "--project", str(project), "--yes"]) == 0
    again, _ = capfd.readouterr()
    assert "\nnothing to upgrade: " in again and "[re-seed] OK" not in again
    assert _written(store, project) == before


def test_wrapping_a_project_an_earlier_tegh_wrapped_names_the_upgrade(wrapped_by_0_3_0, capfd):
    harness = wrapped_by_0_3_0
    store, project = harness["store"], harness["project"]
    capfd.readouterr()
    assert main(
        ["wrap", "claude", "--project", str(project), "--harness-home", str(harness["home"])]
    ) == 2
    _, err = capfd.readouterr()
    assert "is already wrapped, by an earlier tegh" in err
    assert f"`tegh upgrade {store.named_as(project)}`" in err
    assert "tegh unwrap" not in err
