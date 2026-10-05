"""`tegh wrap` when it has to stop: what it says, and what it leaves behind.

A way a wrap used to go wrong without saying so, checked against the bytes on
disk and not against what the command printed:

1. **A second wrap of a wrapped project.** The project's only MCP server is by
   then tegh's own gateway, so the wrap reviewed that, was shown no tools, and
   replaced the signed lock with one pinning none. It now refuses before the
   review and names `tegh unwrap`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import launch  # noqa: E402
from tegh.cli import main  # noqa: E402
from tegh.harnesses import claude_code  # noqa: E402
from tegh.launch import gateway_argv  # noqa: E402
from tegh.lockfile import lock_paths  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    TOY,
    fingerprint,
    store_of,
    unwrap_cli,
    wrap_admit_all,
    written_gateway_entry,
)

_PROJECT = Path("/work/widget")
_HOME = Path("/home/x/.tegh")
_AS_WRITTEN = gateway_argv(_PROJECT, launcher=["/venv/bin/tegh"], home=_HOME)
#: The module launcher as the released 0.1.1 spelled it.
_LAUNCHER_0_1_1 = [sys.executable, "-m", "tegh.cli"]
#: As the command spells it, which is resolved and so not always `str(_HOME)`.
_HOME_WRITTEN = _AS_WRITTEN[-1]


# ---------------------------------------------------------------------------
# A second wrap of a wrapped project
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "home"),
    [
        pytest.param(_AS_WRITTEN, _HOME_WRITTEN, id="console-script"),
        pytest.param(
            gateway_argv(_PROJECT, launcher=launch.python_module_argv("tegh.cli"), home=_HOME),
            _HOME_WRITTEN,
            id="module-launcher",
        ),
        # The same launcher as 0.1.1 wrote it, which a project wrapped by that
        # release still holds.
        pytest.param(
            gateway_argv(_PROJECT, launcher=_LAUNCHER_0_1_1, home=_HOME),
            _HOME_WRITTEN,
            id="module-launcher-written-by-0.1.1",
        ),
        pytest.param(
            gateway_argv(Path("/work/other"), launcher=["/venv/bin/tegh"], home=_HOME),
            None,
            id="another-projects-gateway",
        ),
        # A server of the user's own that happens to be called a gateway.
        pytest.param(["npx", "-y", "gateway"], None, id="the-word-alone"),
        pytest.param(_AS_WRITTEN[1:], None, id="no-launcher"),
        pytest.param([*_AS_WRITTEN, "--verbose"], None, id="something-after-it"),
        pytest.param([], None, id="empty"),
    ],
)
def test_the_gateway_command_is_read_back_as_it_was_written(argv, home) -> None:
    assert launch.gateway_home_in(argv, project=_PROJECT) == home


def _as_left(harness: dict) -> None:
    """The config exactly as the first wrap left it."""


def _rename_gateway(harness: dict) -> None:
    """Give the gateway entry another name, as a user tidying their config might."""
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    block["broker"] = block.pop("tegh")
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _lose_the_backup(harness: dict) -> None:
    """What is left when the tegh home's record of the wrap is gone."""
    store_of(harness).backup_path(harness["project"]).unlink()


def _shadow_the_gateway(harness: dict) -> None:
    """The gateway entry at user scope, under a local server of the same name.

    Local scope wins, so the harness would load the user's server and discovery
    reports the gateway as shadowed. The project is wrapped all the same.
    """
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    document["mcpServers"] = {"tegh": block.pop("tegh")}
    block["tegh"] = {"command": sys.executable, "args": ["-m", TOY]}
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _reopen_a_gap(harness: dict) -> None:
    """Give discovery a blocking finding: claude.ai connectors no longer disabled."""
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    del document["disableClaudeAiConnectors"]
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


_UNWRAP_NAMED = "tegh unwrap --project {project} --home {tegh_home}"


@pytest.mark.parametrize(
    ("disturb", "flags", "way_out"),
    [
        pytest.param(_as_left, (), _UNWRAP_NAMED, id="as-left"),
        # Recognised by the command it runs, so the name does not hide it.
        pytest.param(_rename_gateway, (), _UNWRAP_NAMED, id="gateway-renamed"),
        # Nor does a scope the harness would not load it from.
        pytest.param(_shadow_the_gateway, (), _UNWRAP_NAMED, id="gateway-shadowed"),
        # An unwrap would refuse for want of a backup, so it is not offered.
        pytest.param(_lose_the_backup, (), "remove that entry from the config by hand", id="no-backup"),
        # "Already wrapped" is said before the findings are weighed. With the
        # gaps waived, a check that came second would let the wrap through to
        # replace the lock; without, the person would be told to close a gap
        # and never that the project is wrapped.
        pytest.param(_reopen_a_gap, ("--accept-gaps",), _UNWRAP_NAMED, id="gap-accepted"),
        pytest.param(_reopen_a_gap, (), _UNWRAP_NAMED, id="gap-not-accepted"),
    ],
)
def test_a_second_wrap_refuses_and_leaves_the_lock_alone(
    harness, capsys, disturb, flags, way_out: str
) -> None:
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    disturb(harness)
    root = harness["home"].parent
    lock_path, signature_path = lock_paths(harness["project"])
    lock, signature = lock_path.read_bytes(), signature_path.read_bytes()
    assert b'"tool_name"' in lock, "the first wrap pinned nothing, so this proves nothing"
    before = fingerprint(root)
    capsys.readouterr()

    rc = wrap_admit_all(harness, *flags)
    captured = capsys.readouterr()

    if disturb is _reopen_a_gap:
        assert "\n  !! " in captured.out, "no blocking finding, so the order is not tested"
    assert rc == 2
    assert lock_path.read_bytes() == lock
    assert signature_path.read_bytes() == signature
    assert fingerprint(root) == before
    assert "REVIEW" not in captured.out, "the refusal came after the review started"
    refusal = captured.err.strip()
    assert refusal.startswith(f"REFUSED: {harness['project']} is already wrapped"), refusal
    assert "`tegh unwrap" in refusal
    assert way_out.format(**harness) in refusal
    assert f"{harness['claude_json']})" in refusal, "the config is not named as a file"
    assert len(refusal.splitlines()) == 1, refusal


def _config_there_before(harness: dict) -> list[str]:
    """The fixture as it is: the server at local scope, in a config that exists."""
    return ["ledger"]


def _config_the_wrap_creates(harness: dict) -> list[str]:
    """A home with no `.claude.json`: the server is the project's, in `.mcp.json`.

    The wrap then has to create the file its gateway entry goes into. What the
    harness config said is said by the user's settings file instead.
    """
    block = json.loads(harness["claude_json"].read_text(encoding="utf-8"))["projects"][
        str(harness["project"])
    ]["mcpServers"]
    harness["claude_json"].unlink()
    (harness["project"] / ".mcp.json").write_text(
        json.dumps({"mcpServers": block}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    settings = harness["home"] / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"enableAllProjectMcpServers": True, "disableClaudeAiConnectors": True}),
        encoding="utf-8",
    )
    return []


_NAMED_UNWRAP = re.compile(r"`tegh unwrap --project (\S+) --home (\S+)`")


@pytest.mark.parametrize("start", [_config_there_before, _config_the_wrap_creates])
@pytest.mark.parametrize("disturb", [_as_left, _rename_gateway, _shadow_the_gateway])
def test_the_unwrap_a_refused_wrap_names_lets_the_next_wrap_through(
    harness, capsys, start, disturb
) -> None:
    """The advice is followed to its end, and must not lead back to the refusal.

    A gateway entry that was renamed, or moved to another scope, used to
    survive the unwrap as a server "added since the wrap", beside the servers
    the unwrap restored. The next wrap then refused again, with the backup
    gone and nothing left to unwrap from. The command run here is the one the
    refusal prints, read out of it.
    """
    local_before = start(harness)
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    assert harness["claude_json"].exists()
    disturb(harness)
    capsys.readouterr()
    assert wrap_admit_all(harness) == 2
    named = _NAMED_UNWRAP.search(capsys.readouterr().err)
    assert named, "the refusal names no unwrap command"

    assert main(["unwrap", "--project", named[1], "--home", named[2], "--yes"]) == 0
    plan = capsys.readouterr().out

    # The shadowing case leaves a server of the user's own called `tegh` where
    # the gateway entry was. It runs something else, so it is kept and said.
    theirs = ["tegh"] if disturb is _shadow_the_gateway else []
    gateway_was = {_as_left: None, _rename_gateway: "broker", _shadow_the_gateway: "tegh"}[disturb]
    removal = f"({gateway_was}), which runs tegh's gateway for this project"
    assert (removal in plan) == (gateway_was is not None), plan
    assert ("(tegh), added since the wrap" in plan) == bool(theirs), plan
    if start is _config_the_wrap_creates and not theirs:
        # The wrap created the file, and nothing but the wrap's entry was in it.
        assert not harness["claude_json"].exists()
    else:
        document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
        block = document["projects"][str(harness["project"])]["mcpServers"]
        assert sorted(block) == sorted([*local_before, *theirs])
        assert "mcpServers" not in document
    assert wrap_admit_all(harness) == 0, capsys.readouterr().err
    assert b'"tool_name"' in lock_paths(harness["project"])[0].read_bytes()


def test_the_hand_edit_a_refused_wrap_names_lets_the_next_wrap_through(harness, capsys) -> None:
    """With no backup there is no unwrap to name, and the other way out works too."""
    before = harness["claude_json"].read_bytes()
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    _lose_the_backup(harness)
    capsys.readouterr()
    assert wrap_admit_all(harness) == 2
    refusal = capsys.readouterr().err
    assert "`tegh unwrap --project" not in refusal, "an unwrap that would refuse is named"
    assert "by hand and put your own servers back in its place" in refusal

    # Both halves of the advice. The entry alone leaves a project with no
    # server, which is not a wrap.
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    del document["projects"][str(harness["project"])]["mcpServers"]["tegh"]
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    assert wrap_admit_all(harness) == 1
    assert "Nothing to wrap" in capsys.readouterr().out
    harness["claude_json"].write_bytes(before)

    assert wrap_admit_all(harness) == 0, capsys.readouterr().err
    assert store_of(harness).backup_path(harness["project"]).exists()


def test_a_server_of_the_users_called_tegh_is_displaced_listed_and_restored(
    harness, capsys
) -> None:
    """`tegh` is only what a wrap calls its entry. A server may have the name first."""
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    block["tegh"] = dict(block["ledger"])
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    before = harness["claude_json"].read_bytes()
    assert main(["init"]) == 0
    capsys.readouterr()

    assert wrap_admit_all(harness) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert f"displaced local:{harness['claude_json']}: ledger, tegh\n" in out
    written = written_gateway_entry(harness)
    assert launch.gateway_home_in(
        [written["command"], *written["args"]], project=harness["project"]
    ), "the entry called tegh is not the gateway after the wrap"

    assert unwrap_cli(harness, "--yes") == 0
    plan = capsys.readouterr().out
    assert f"local:{harness['claude_json']}  (ledger, tegh)" in plan
    assert harness["claude_json"].read_bytes() == before


def _as_0_1_1_left_it(harness: dict) -> None:
    """Turn this version's wrap into the one the released 0.1.1 wrote.

    Two differences: its module launcher had no `-P`, and its backup did not
    record whether each config file existed.
    """
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    block["tegh"] = claude_code.gateway_entry(
        harness["project"], launcher=_LAUNCHER_0_1_1, home=harness["tegh_home"]
    )
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    backup_path = store_of(harness).backup_path(harness["project"])
    recorded = json.loads(backup_path.read_text(encoding="utf-8"))
    for site in recorded["sites"]:
        del site["file_existed"]
    backup_path.write_text(json.dumps(recorded, indent=2), encoding="utf-8")


def test_a_project_wrapped_by_0_1_1_is_recognised_and_unwrapped(harness, capsys) -> None:
    before = harness["claude_json"].read_bytes()
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    _as_0_1_1_left_it(harness)
    capsys.readouterr()

    assert wrap_admit_all(harness) == 2
    assert "is already wrapped" in capsys.readouterr().err
    assert unwrap_cli(harness, "--yes") == 0
    assert harness["claude_json"].read_bytes() == before
    assert wrap_admit_all(harness) == 0, capsys.readouterr().err
