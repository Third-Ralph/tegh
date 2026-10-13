"""The hook entry a wrap adds to `.claude/settings.local.json`, and the unwrap that takes it out.

Driven through the calls a wrap and an unwrap make (`hooksite.plan_hook`,
`backup_of_hook`, `write_hook`, the backup's JSON, `plan_hook_restore`,
`apply_hook_restore`), on files in `tmp_path`. No ceremony and no server: the
real wrap that installs the hook is `test_hook_e2e.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tegh import hooksite
from tegh.harnesses import claude_code
from tegh.hooksite import HookBackup, HookSite
from tegh.interpose import ConfigFormatUnreproducible, InterposeError

COMMAND = "/venv/bin/tegh hook --project /p --home /h"
PRE, POST = "PreToolUse", "PostToolUse"


def _render(document: dict, newline: bool = True) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + ("\n" if newline else "")


def _wrap(site: HookSite, command: str = COMMAND) -> HookBackup:
    """What a wrap does at the site, with the backup through its JSON form."""
    install = hooksite.plan_hook(site, command)
    backup = hooksite.backup_of_hook(install)
    hooksite.write_hook(install)
    return HookBackup.from_dict(json.loads(json.dumps(backup.to_dict())))


def _unwrap(backup: HookBackup) -> hooksite.HookRestore:
    step = hooksite.plan_hook_restore(backup)
    if step.changes:
        hooksite.apply_hook_restore(step)
    return step


@pytest.fixture
def site(tmp_path: Path) -> HookSite:
    project = tmp_path / "widget"
    project.mkdir()
    return claude_code.hook_site(project)


EXISTING_PRE = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard-bash"}]}
        ],
    },
}

EXISTING = {
    "permissions": {"allow": ["Bash(git status)"]},
    "hooks": {
        "PostToolUse": [
            {"matcher": "Write", "hooks": [{"type": "command", "command": "prettier"}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}


@pytest.mark.parametrize(
    "before",
    [
        pytest.param({}, id="empty object"),
        pytest.param({"permissions": {"allow": []}}, id="no hooks key"),
        pytest.param({"hooks": {}}, id="empty hooks"),
        pytest.param({"hooks": {"Stop": []}}, id="no tool events"),
        pytest.param({"hooks": {"PostToolUse": []}}, id="empty PostToolUse"),
        pytest.param({"hooks": {"PreToolUse": [], "PostToolUse": []}}, id="both empty"),
        pytest.param(EXISTING, id="existing hooks"),
        pytest.param(EXISTING_PRE, id="existing PreToolUse hook"),
    ],
)
@pytest.mark.parametrize("newline", [True, False], ids=["newline", "no newline"])
def test_add_then_remove_is_byte_identical(site, before, newline):
    site.path.parent.mkdir()
    original = _render(before, newline)
    site.path.write_text(original, encoding="utf-8")

    backup = _wrap(site)
    after = json.loads(site.path.read_text(encoding="utf-8"))
    for event in (PRE, POST):
        groups = after["hooks"][event]
        assert groups[-1] == hooksite.hook_entry(COMMAND, event)
        assert groups[:-1] == before.get("hooks", {}).get(event, [])
    # Everything else in the file is as it was.
    rest = {k: v for k, v in after.items() if k != "hooks"}
    assert rest == {k: v for k, v in before.items() if k != "hooks"}

    _unwrap(backup)
    assert site.path.read_text(encoding="utf-8") == original


def test_the_entries_are_pre_and_post_tool_use_and_only_the_pre_one_blocks_on_failure(site):
    """What the wrap adds: one command under two events, for every tool, and no other event.

    The `PreToolUse` handler carries `onFailure: block`, so a run of the hook
    that fails, times out or never starts refuses the call. The `PostToolUse`
    handler is the entry 0.3.0 wrote, byte for byte.
    """
    _wrap(site)
    assert site.path.read_text(encoding="utf-8") == (
        "{\n"
        '  "hooks": {\n'
        '    "PreToolUse": [\n'
        "      {\n"
        '        "matcher": "",\n'
        '        "hooks": [\n'
        "          {\n"
        '            "type": "command",\n'
        f'            "command": "{COMMAND}",\n'
        '            "timeout": 10,\n'
        '            "onFailure": "block"\n'
        "          }\n"
        "        ]\n"
        "      }\n"
        "    ],\n"
        '    "PostToolUse": [\n'
        "      {\n"
        '        "matcher": "",\n'
        '        "hooks": [\n'
        "          {\n"
        '            "type": "command",\n'
        f'            "command": "{COMMAND}",\n'
        '            "timeout": 10\n'
        "          }\n"
        "        ]\n"
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
    )


def test_absent_file_and_directory_come_back_absent(site):
    assert not site.path.parent.exists()
    backup = _wrap(site)
    assert backup.file_existed is False and backup.dir_existed is False
    assert site.path.read_text(encoding="utf-8").endswith("}\n")

    step = _unwrap(backup)
    assert step.removes_file and step.removes_dir
    assert not site.path.exists()
    assert not site.path.parent.exists()


def test_a_directory_that_was_there_stays(site):
    site.path.parent.mkdir()
    (site.path.parent / "commands").mkdir()
    _unwrap(_wrap(site))
    assert not site.path.exists()
    assert site.path.parent.is_dir()


def test_a_file_the_wrap_created_stays_when_something_else_was_added_to_it(site):
    backup = _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    document["permissions"] = {"allow": ["Read"]}
    site.path.write_text(_render(document), encoding="utf-8")

    step = _unwrap(backup)
    assert not step.removes_file
    assert json.loads(site.path.read_text(encoding="utf-8")) == {"permissions": {"allow": ["Read"]}}


def test_a_hook_added_since_the_wrap_is_kept(site):
    backup = _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    added = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit-bash"}]}
    document["hooks"]["PostToolUse"].append(added)
    site.path.write_text(_render(document), encoding="utf-8")

    _unwrap(backup)
    assert json.loads(site.path.read_text(encoding="utf-8")) == {
        "hooks": {"PostToolUse": [added]}
    }


def test_a_hook_put_beside_tegh_in_its_own_group_is_kept_and_the_plan_says_so(site):
    backup = _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    beside = {"type": "command", "command": "audit-everything"}
    document["hooks"]["PostToolUse"][0]["hooks"].append(beside)
    site.path.write_text(_render(document), encoding="utf-8")

    step = _unwrap(backup)
    assert step.shared
    assert json.loads(site.path.read_text(encoding="utf-8")) == {
        "hooks": {"PostToolUse": [{"matcher": "", "hooks": [beside]}]}
    }


def test_a_rewrap_adds_no_second_entry(site):
    """An identical hook already there is not added again, and the unwrap leaves it."""
    first = _wrap(site)
    assert first.added
    once = site.path.read_text(encoding="utf-8")

    second = _wrap(site)
    assert second.added is False
    assert site.path.read_text(encoding="utf-8") == once

    step = _unwrap(second)
    assert not step.changes
    assert site.path.read_text(encoding="utf-8") == once
    _unwrap(first)
    assert not site.path.exists()


def test_wrap_unwrap_wrap_leaves_one_entry_per_event(site):
    _unwrap(_wrap(site))
    _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    assert {event: len(groups) for event, groups in document["hooks"].items()} == {
        PRE: 1,
        POST: 1,
    }


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"hooks": {}}', id="compact"),
        pytest.param('{\n    "hooks": {}\n}\n', id="four-space indent"),
        pytest.param('{\n  "hooks": {}\n}\n\n', id="two newlines"),
        pytest.param('{\n  "a": "\\u00e9"\n}', id="escaped non-ascii"),
    ],
)
def test_a_file_that_does_not_round_trip_refuses_before_anything_is_written(site, raw):
    site.path.parent.mkdir()
    site.path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigFormatUnreproducible):
        hooksite.check_site(site)
    with pytest.raises(ConfigFormatUnreproducible):
        hooksite.plan_hook(site, COMMAND)
    assert site.path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "document",
    [
        pytest.param([], id="not an object"),
        pytest.param({"hooks": []}, id="hooks not an object"),
        pytest.param({"hooks": {"PostToolUse": {}}}, id="event not a list"),
        pytest.param({"hooks": {"PreToolUse": {}}}, id="PreToolUse not a list"),
    ],
)
def test_a_shape_this_adapter_does_not_model_refuses(site, document):
    site.path.parent.mkdir()
    site.path.write_text(_render(document), encoding="utf-8")
    with pytest.raises(InterposeError):
        hooksite.plan_hook(site, COMMAND)


def test_an_unwrap_of_a_reformatted_file_still_holding_the_hook_refuses_and_says_how(site):
    backup = _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    site.path.write_text(json.dumps(document), encoding="utf-8")  # compact: no round trip
    with pytest.raises(InterposeError, match="by hand"):
        hooksite.plan_hook_restore(backup)

    # Taken out by hand, the next unwrap has nothing to do in this file.
    site.path.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    assert not hooksite.plan_hook_restore(backup).changes


def test_a_backup_hook_record_with_a_wrong_type_is_refused():
    good = HookBackup(
        path="/p", command=COMMAND, file_existed=True, hooks_existed=True, dir_existed=True,
        events={
            PRE: hooksite.EventBackup(existed=False, added=True),
            POST: hooksite.EventBackup(existed=True, added=True),
        },
    ).to_dict()
    assert good["events"] == {
        PRE: {"existed": False, "added": True},
        POST: {"existed": True, "added": True},
    }
    assert HookBackup.from_dict(good).to_dict() == good
    for bad in (
        {**good, "dir_existed": "yes"},
        {**good, "events": {PRE: {"existed": False, "added": "yes"}}},
        {**good, "events": {PRE: True}},
        {**good, "events": []},
        {**good, "events": {}},
        {**good, "command": 7},
    ):
        with pytest.raises(TypeError):
            HookBackup.from_dict(bad)
    with pytest.raises(KeyError):
        HookBackup.from_dict({k: v for k, v in good.items() if k != "command"})
    with pytest.raises(KeyError):
        HookBackup.from_dict({**good, "events": {PRE: {"existed": False}}})


#: The `hook` record a 0.3.0 wrap wrote into its backup, for a settings file
#: the wrap created: one event, flat.
OLD_RECORD = {
    "event": "PostToolUse",
    "command": COMMAND,
    "added": True,
    "file_existed": False,
    "hooks_existed": False,
    "event_existed": False,
    "dir_existed": False,
}


def test_a_backup_from_0_3_0_reads_as_one_event_and_unwraps_cleanly(site):
    """A project wrapped by 0.3.0 has only the PostToolUse entry and a flat record."""
    site.path.parent.mkdir()
    site.path.write_text(
        _render({"hooks": {POST: [hooksite.hook_entry(COMMAND, POST)]}}), encoding="utf-8"
    )
    backup = HookBackup.from_dict({**OLD_RECORD, "path": str(site.path), "dir_existed": True})
    assert backup.events == {POST: hooksite.EventBackup(existed=False, added=True)}
    assert backup.site.events == (POST,)
    with pytest.raises(KeyError):
        HookBackup.from_dict({k: v for k, v in OLD_RECORD.items() if k != "event_existed"})
    with pytest.raises(TypeError):
        HookBackup.from_dict({**OLD_RECORD, "path": "/p", "added": "yes"})

    step = _unwrap(backup)
    assert step.events_found == (POST,)
    assert step.removes_file and not step.removes_dir
    assert not site.path.exists()
    assert site.path.parent.is_dir()


def test_a_0_3_0_wrap_gets_the_pre_entry_added_and_only_that_comes_out_again(site):
    """Re-wrapping a 0.3.0 project adds the PreToolUse entry and leaves the PostToolUse one."""
    site.path.parent.mkdir()
    original = _render({"hooks": {POST: [hooksite.hook_entry(COMMAND, POST)]}})
    site.path.write_text(original, encoding="utf-8")

    backup = _wrap(site)
    assert backup.events == {
        PRE: hooksite.EventBackup(existed=False, added=True),
        POST: hooksite.EventBackup(existed=True, added=False),
    }
    document = json.loads(site.path.read_text(encoding="utf-8"))
    assert document["hooks"] == {
        POST: [hooksite.hook_entry(COMMAND, POST)],
        PRE: [hooksite.hook_entry(COMMAND, PRE)],
    }

    step = _unwrap(backup)
    assert step.events_found == (PRE,)
    assert site.path.read_text(encoding="utf-8") == original


def test_the_unwrap_plan_names_every_event_it_takes_the_hook_out_of(site):
    step = hooksite.plan_hook_restore(_wrap(site))
    assert step.events_found == (PRE, POST)
    assert step.found and step.changes


def test_a_hook_put_beside_tegh_in_its_pre_tool_use_group_is_kept(site):
    backup = _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    beside = {"type": "command", "command": "guard-everything"}
    document["hooks"][PRE][0]["hooks"].append(beside)
    site.path.write_text(_render(document), encoding="utf-8")

    step = _unwrap(backup)
    assert step.shared
    assert json.loads(site.path.read_text(encoding="utf-8")) == {
        "hooks": {PRE: [{"matcher": "", "hooks": [beside]}]}
    }


def test_commands_by_event_reads_every_event_and_commands_at_reads_post_only(site):
    assert hooksite.commands_by_event(site) == {}
    assert hooksite.commands_at(site) == []
    site.path.parent.mkdir()
    site.path.write_text(_render(EXISTING_PRE), encoding="utf-8")
    assert hooksite.commands_by_event(site) == {PRE: ["guard-bash"], POST: []}
    assert hooksite.commands_at(site) == []

    _wrap(site)
    assert hooksite.commands_by_event(site) == {PRE: ["guard-bash", COMMAND], POST: [COMMAND]}
    assert hooksite.commands_at(site) == [COMMAND]

    site.path.write_text("{", encoding="utf-8")
    with pytest.raises(InterposeError):
        hooksite.commands_by_event(site)
