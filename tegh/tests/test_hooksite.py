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
        pytest.param({"hooks": {"Stop": []}}, id="no PostToolUse"),
        pytest.param({"hooks": {"PostToolUse": []}}, id="empty PostToolUse"),
        pytest.param(EXISTING, id="existing hooks"),
    ],
)
@pytest.mark.parametrize("newline", [True, False], ids=["newline", "no newline"])
def test_add_then_remove_is_byte_identical(site, before, newline):
    site.path.parent.mkdir()
    original = _render(before, newline)
    site.path.write_text(original, encoding="utf-8")

    backup = _wrap(site)
    after = json.loads(site.path.read_text(encoding="utf-8"))
    groups = after["hooks"]["PostToolUse"]
    assert groups[-1] == hooksite.hook_entry(COMMAND)
    assert groups[:-1] == before.get("hooks", {}).get("PostToolUse", [])
    # Everything else in the file is as it was.
    rest = {k: v for k, v in after.items() if k != "hooks"}
    assert rest == {k: v for k, v in before.items() if k != "hooks"}

    _unwrap(backup)
    assert site.path.read_text(encoding="utf-8") == original


def test_the_entry_is_post_tool_use_only_and_observes_every_tool(site):
    """What the wrap adds runs after a call and registers for no other event."""
    _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    assert list(document["hooks"]) == ["PostToolUse"]
    (group,) = document["hooks"]["PostToolUse"]
    assert group == {
        "matcher": "",
        "hooks": [{"type": "command", "command": COMMAND, "timeout": 10}],
    }


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


def test_wrap_unwrap_wrap_leaves_one_entry(site):
    _unwrap(_wrap(site))
    _wrap(site)
    document = json.loads(site.path.read_text(encoding="utf-8"))
    assert len(document["hooks"]["PostToolUse"]) == 1


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
        path="/p", event="PostToolUse", command=COMMAND, added=True, file_existed=True,
        hooks_existed=True, event_existed=True, dir_existed=True,
    ).to_dict()
    assert HookBackup.from_dict(good).command == COMMAND
    with pytest.raises(TypeError):
        HookBackup.from_dict({**good, "added": "yes"})
    with pytest.raises(KeyError):
        HookBackup.from_dict({k: v for k, v in good.items() if k != "command"})
