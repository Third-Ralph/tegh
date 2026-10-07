"""The hook, end to end: a real wrap, a real gateway, a real hook process, a real tape.

The decisive case. A project is wrapped for real, which installs the
`PostToolUse` hook in its local settings. The gateway is started over stdio with
the command the wrap wrote, the way Claude Code starts it, and opens the
tool-event mouth beside it. Then the command the wrap wrote into the HOOK entry
is run through a shell, as Claude Code runs it, with a `PostToolUse` event for a
`Read` of a file under the home directory and outside the project. The agent's
next external write through the same gateway is held, and the tape says why, in
order: the observed read, then the held write.

The control is the same write with no event, which executes, and a read INSIDE
the project, which the wrap's manifest trusts: it is recorded and taints
nothing, so the write after it executes too.

No Claude Code runs here. The event stands in for the one it would send, built
from the field list in `docs/references/harnesses/claude-code.md` §3
(`events/post_tool_use_read.json`), and the stdio client is the base's.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the gateway and the toy server use the MCP SDK")

from safe_agents.broker.api import GatewayClient, result_text  # noqa: E402

from tegh import hook  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.harnesses import claude_code  # noqa: E402
from tegh.launch import gateway_argv, harness_spawn_env, tegh_launcher  # noqa: E402
from tegh.tests.test_hook import recorded_event  # noqa: E402
from tegh.tests.wrapping import REPO, scripted, store_of  # noqa: E402

#: Each toy tool, corrected to a REVERSIBLE external write: edit, keep effect
#: `write`, reversible yes, keep egress_arg, admit. Untainted, the broker lets
#: such a write execute; in a tainted turn it holds it. That difference is the
#: whole observable effect of a report, so it is what the tool must be.
ADMIT_AS_REVERSIBLE_WRITE = ("e", "", "y", "", "y")
TOY_TOOL_COUNT = 2

WRITE_TOOL = "ledger__get_entry"
WRITE_ARGS = {"entry_id": "L-001"}
_WAIT_SECONDS = 60


@pytest.fixture
def wrapped(harness: dict, capfd) -> dict:
    assert main(["init"]) == 0
    status = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
            ]
        ),
        prompt=scripted(list(ADMIT_AS_REVERSIBLE_WRITE) * TOY_TOOL_COUNT),
    )
    out, _ = capfd.readouterr()
    assert status == 0, out
    assert "hook      hooks:" in out
    return harness


def _installed_hook_command(project: Path) -> str:
    settings = json.loads(claude_code.hook_site(project).path.read_text(encoding="utf-8"))
    (group,) = settings["hooks"]["PostToolUse"]
    (entry,) = group["hooks"]
    return entry["command"]


def _wait_for_address(path: Path, client: GatewayClient) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="ascii").endswith("\n"):
            return
        if client.proc.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail("the gateway never wrote its tool-event mouth address:\n" + client.stderr_text)


def _run_hook(command: str, event: dict, home: Path) -> subprocess.CompletedProcess:
    """The installed hook command, through a shell, as Claude Code runs a command hook."""
    return subprocess.run(  # noqa: S603 - the command the wrap wrote, run as the harness runs it
        ["/bin/sh", "-c", command],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
        cwd=REPO,
        check=False,
        timeout=60,
    )


def _tape(capfd, project: Path) -> list[dict]:
    """`tegh audit --json`: the supported reader, run as a child, so read at the fd."""
    capfd.readouterr()
    assert main(["audit", "--json", "--project", str(project)]) == 0
    return json.loads(capfd.readouterr().out)["records"]


def _verified(capfd, project: Path) -> str:
    capfd.readouterr()
    assert main(["audit", "--verify", "--project", str(project)]) == 0
    return capfd.readouterr().out


@pytest.mark.parametrize(
    "reported",
    [
        pytest.param("outside", id="read under home, outside the project: held"),
        pytest.param(None, id="control: nothing reported: executes"),
        pytest.param("project", id="read inside the project, trusted: executes"),
    ],
)
def test_a_reported_built_in_read_holds_the_next_external_write(wrapped, capfd, reported):
    project, home = wrapped["project"], wrapped["home"]
    store = store_of(wrapped)
    command = gateway_argv(project, launcher=tegh_launcher(), home=store.home)

    if reported == "outside":
        target = home / "notes" / "outside-the-project.md"
    else:
        target = project / "src" / "main.py"
    event = recorded_event(cwd=str(project), tool_input={"file_path": str(target)})

    client = GatewayClient(command, env=harness_spawn_env(), timeout=120)
    try:
        client.initialize(client_name="tegh-hook-e2e")
        _wait_for_address(store.event_mouth_addr_path(project), client)
        if reported is not None:
            ran = _run_hook(_installed_hook_command(project), event, home)
            assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")
        result = client.call_tool(WRITE_TOOL, WRITE_ARGS)
    finally:
        client.close()

    tape = _tape(capfd, project)
    verdicts = [(r["tool"], r["op"], r["decision"], r["outcome"]) for r in tape]
    assert "CHAIN CONSISTENT" in _verified(capfd, project)

    if reported is None:
        assert result["isError"] is False, result_text(result)
        assert verdicts == [("ledger", "get_entry", "allow", "executed")]
        return

    observed = ("harness-tool", "file-read", "abstain", "observed")
    if reported == "project":
        assert result["isError"] is False, result_text(result)
        assert verdicts == [observed, ("ledger", "get_entry", "allow", "executed")]
        return

    assert result["isError"] is True
    assert "held for approval" in result_text(result)
    assert verdicts == [observed, ("ledger", "get_entry", "require_approval", "held")]
    seen, held = tape
    assert held["reason"] == "tainted external write"
    # The record carries the subject's digest and no spelling of it.
    expected = hook.report_of(event, project=project, home=home)
    assert expected["locality"] == "home"
    assert str(target) not in json.dumps(seen)
    assert seen["resultDigest"] == expected["result_digest"]


def test_a_wrap_with_no_hooks_installs_none(harness, capsys):
    assert main(["init"]) == 0
    status = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
                "--admit-all", "--no-hooks",
            ]
        ),
        prompt=scripted([]),
    )
    out, _ = capsys.readouterr()
    assert status == 0, out
    assert "--no-hooks" in out
    assert not (harness["project"] / ".claude").exists()


def test_unwrap_takes_the_hook_out_and_the_directory_the_wrap_made(harness, capsys):
    """The project had no `.claude/` before the wrap, and has none after the unwrap."""
    assert main(["init"]) == 0
    assert main(
        [
            "wrap", "claude", "--project", str(harness["project"]),
            "--harness-home", str(harness["home"]), "--admit-all",
        ]
    ) == 0
    site = claude_code.hook_site(harness["project"])
    assert site.path.exists()
    capsys.readouterr()

    assert main(["unwrap", "--yes", "--project", str(harness["project"])]) == 0
    out = capsys.readouterr().out
    assert "tegh's PostToolUse hook entry" in out
    assert not site.path.exists()
    assert not site.path.parent.exists()


def test_status_and_diff_say_whether_built_in_calls_are_observed(harness, capsys):
    assert main(["init"]) == 0
    assert main(
        [
            "wrap", "claude", "--project", str(harness["project"]),
            "--harness-home", str(harness["home"]), "--admit-all",
        ]
    ) == 0
    capsys.readouterr()
    for command in ("status", "diff"):
        main([command, "--project", str(harness["project"])])
        out = capsys.readouterr().out
        assert "built-in tools: OBSERVED, not gated" in out, (command, out)

    site = claude_code.hook_site(harness["project"])
    site.path.write_text("{}\n", encoding="utf-8")
    main(["status", "--project", str(harness["project"])])
    assert "built-in tools: NOT observed" in capsys.readouterr().out
