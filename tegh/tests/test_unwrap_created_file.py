"""`tegh unwrap` and a harness config file the wrap had to create.

The gateway entry goes into `~/.claude.json`. A harness that has never written
that file has none, so the wrap creates it, and the backup records that it did.
Three claims:

1. **A file the wrap created is removed when nothing else is in it**, and the
   plan says so before the question. Anything added to it since the wrap keeps
   the file, holding just that, and the plan has a line for every write to it.
2. **A file the wrap did not create is never removed.** That covers a file that
   was there before the wrap however little it held, a backup written before
   this was recorded, and a `.mcp.json` created since the wrap.
3. **The removal is agreed to and finished like every other step.** A file that
   changes while the question waits stops the unwrap, a re-run after an
   interruption does what is left, and a file that is not as the plan found it
   is never removed.

Every case starts from `wrapping.interposed`, as in `test_unwrap_recovery.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tegh import interpose, unwrap
from tegh.tests.wrapping import fingerprint, interposed, unwrap_cli

_NOTES = {"notes": {"command": "notes-server"}}
_BAR = {"command": "bar-server"}

#: The line a removal gets, after the path.
_THE_FILE = "(the file itself, which the wrap created and nothing else has been added to)"
#: The line the empty objects above the wrap's entry get, in a file that stays.
_THE_ENTRY = "(the empty projects entry the wrap created for this project)"


def _document(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _edit(path: Path, change) -> None:
    """Rewrite a config the way a harness does: parse, change, write the same style."""
    document = _document(path) or {}
    change(document)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")


def _action_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.startswith("  ")]


def _created(tmp_path, monkeypatch) -> dict:
    """A wrap of a project whose one server is in `.mcp.json`, in a home with no config."""
    state = interposed(tmp_path, monkeypatch, project_scope=_NOTES, harness_config=False)
    assert state["originals"][state["claude_json"]] is None
    assert state["claude_json"].exists(), "the wrap did not create the harness config"
    return state


def _nothing(document: dict, project: str) -> None:
    pass


def _a_local_server(document: dict, project: str) -> None:
    document["projects"][project]["mcpServers"]["bar"] = _BAR


def _a_user_server(document: dict, project: str) -> None:
    document["mcpServers"] = {"bar": _BAR}


def _harness_state(document: dict, project: str) -> None:
    document["numStartups"] = 3


def _project_state(document: dict, project: str) -> None:
    document["projects"][project]["lastSessionId"] = "since-the-wrap"


def _both_states(document: dict, project: str) -> None:
    _harness_state(document, project)
    _project_state(document, project)


def _the_block_taken_out(document: dict, project: str) -> None:
    """The wrap's block removed by hand, with a key the harness wrote beside it."""
    _harness_state(document, project)
    del document["projects"][project]["mcpServers"]


def _the_entry_taken_out(document: dict, project: str) -> None:
    """Everything the wrap wrote removed by hand: there is no entry to be empty."""
    _harness_state(document, project)
    del document["projects"]


def _nothing_left_to_do(document: dict, project: str) -> None:
    _project_state(document, project)
    del document["projects"][project]["mcpServers"]


_SESSION = {"projects": {"{project}": {"lastSessionId": "since-the-wrap"}}}


@pytest.mark.parametrize(
    ("since", "left", "entry_goes"),
    [
        pytest.param(_nothing, None, False, id="nothing-added"),
        pytest.param(
            _a_local_server, {"projects": {"{project}": {"mcpServers": {"bar": _BAR}}}}, False,
            id="a-server-beside-the-gateway",
        ),
        # The objects the wrap made on the way to its entry go with it.
        pytest.param(
            _a_user_server, {"mcpServers": {"bar": _BAR}}, True, id="a-user-scope-server"
        ),
        pytest.param(_harness_state, {"numStartups": 3}, True, id="a-key-the-harness-wrote"),
        pytest.param(_project_state, _SESSION, False, id="state-under-the-project"),
        # The empty entry is then the only thing the unwrap changes in the file.
        pytest.param(
            _the_block_taken_out, {"numStartups": 3}, True, id="the-block-taken-out-by-hand"
        ),
        pytest.param(
            _the_entry_taken_out, {"numStartups": 3}, False, id="the-entry-taken-out-by-hand"
        ),
        pytest.param(_nothing_left_to_do, _SESSION, False, id="nothing-left-to-do-in-it"),
    ],
)
def test_a_file_the_wrap_created_is_removed_only_when_nothing_else_is_in_it(
    tmp_path, monkeypatch, capsys, since, left, entry_goes: bool
) -> None:
    state = _created(tmp_path, monkeypatch)
    claude_json, project = state["claude_json"], str(state["project"])
    _edit(claude_json, lambda document: since(document, project))
    before = claude_json.read_bytes()
    removal = f"remove {claude_json} {_THE_FILE}"
    entry = f"remove local:{claude_json} {_THE_ENTRY}"

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err
    plan_text, report_text = capsys.readouterr().out.split("It leaves untouched:")
    plan, report = _action_lines(plan_text), _action_lines(report_text)

    expected = json.loads(json.dumps(left).replace("{project}", project))
    assert _document(claude_json) == expected
    # Said in the plan when it happens, in the report in the past tense, and in
    # neither when it does not.
    for line, happens in ((removal, left is None), (entry, entry_goes)):
        assert (line in plan) == happens, plan_text
        assert (line.replace("remove ", "removed ", 1) in report) == happens, report_text
    # One list, read twice: the file is written exactly when a line names it.
    written = not claude_json.exists() or claude_json.read_bytes() != before
    assert any(str(claude_json) in line for line in plan) == written, plan_text
    assert state["project_mcp"].read_bytes() == state["originals"][state["project_mcp"]]
    assert not state["store"].backup_path(state["project"]).exists()


def _written_by_0_1_1(state: dict) -> None:
    """Take out what 0.1.1 did not record: whether each file existed."""
    backup_path = state["store"].backup_path(state["project"])
    recorded = json.loads(backup_path.read_text(encoding="utf-8"))
    existed = {site["scope"]: site.pop("file_existed") for site in recorded["sites"]}
    assert existed == {"local": False, "project": True, "user": False}
    backup_path.write_text(json.dumps(recorded, indent=2), encoding="utf-8")


def _old_backup(tmp_path, monkeypatch) -> dict:
    state = _created(tmp_path, monkeypatch)
    _written_by_0_1_1(state)
    return state


def _empty_before_the_wrap(tmp_path, monkeypatch) -> dict:
    """The file was there, holding `{}`. The wrap wrote into it and did not create it."""
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
    return interposed(tmp_path, monkeypatch, project_scope=_NOTES, harness_config=False)


def _a_project_file_made_since(tmp_path, monkeypatch) -> dict:
    """`.mcp.json` did not exist at the wrap, and the wrap never creates that one."""
    state = interposed(tmp_path, monkeypatch, local=_NOTES)
    state["project_mcp"].write_text("{}", encoding="utf-8")
    return state


@pytest.mark.parametrize(
    ("arrange", "which"),
    [
        pytest.param(_old_backup, "claude_json", id="a-backup-written-by-0.1.1"),
        pytest.param(_empty_before_the_wrap, "claude_json", id="a-file-there-before-the-wrap"),
        pytest.param(_a_project_file_made_since, "project_mcp", id="a-project-file-made-since"),
    ],
)
def test_a_file_the_wrap_did_not_create_is_never_removed(
    tmp_path, monkeypatch, capsys, arrange, which: str
) -> None:
    state = arrange(tmp_path, monkeypatch)
    path = state[which]

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err
    out = capsys.readouterr().out

    assert path.exists()
    assert _THE_FILE not in out
    # What is left is what 0.1.1 leaves: the block gone, the file in place.
    left = _document(path)
    assert left in ({}, {"projects": {str(state["project"]): {}}}), left


def _no_harness_state(document: dict, project: str) -> None:
    del document["numStartups"]


def _no_project_state(document: dict, project: str) -> None:
    del document["projects"][project]["lastSessionId"]


@pytest.mark.parametrize(
    ("before", "during", "says", "stays"),
    [
        pytest.param(
            _nothing, _harness_state,
            "the plan showed tegh removing this file, which the wrap created, and it "
            "now holds something else",
            True,
            id="something-is-written-into-it",
        ),
        pytest.param(
            _harness_state, _no_harness_state,
            "the plan showed tegh keeping this file, and it now holds nothing but "
            "what the wrap put there",
            False,
            id="the-rest-of-it-goes",
        ),
        pytest.param(
            _harness_state, _project_state,
            "the plan showed tegh removing an empty entry the wrap created in this "
            "file, and it now holds something in that entry, or no such entry",
            True,
            id="the-empty-entry-gains-something",
        ),
        pytest.param(
            _both_states, _no_project_state,
            "the plan showed tegh leaving the rest of this file as it is, and it now "
            "holds an empty entry the wrap created",
            True,
            id="the-entry-is-emptied",
        ),
    ],
)
def test_a_created_file_that_changes_while_the_question_waits_stops_the_unwrap(
    tmp_path, monkeypatch, capsys, before, during, says: str, stays: bool
) -> None:
    """The yes was to a plan, and what the plan did to the file is not what would happen."""
    state = _created(tmp_path, monkeypatch)
    claude_json, project = state["claude_json"], str(state["project"])
    _edit(claude_json, lambda document: before(document, project))
    seen: dict = {}

    def _the_harness_writes_then_the_person_agrees(question: str) -> str:
        _edit(claude_json, lambda document: during(document, project))
        seen.update(fingerprint(tmp_path))
        return "y"

    rc = unwrap_cli(state, prompt=_the_harness_writes_then_the_person_agrees)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert fingerprint(tmp_path) == seen, "the unwrap wrote after the file changed"
    assert f"{claude_json} changed while tegh unwrap was waiting: {says}" in captured.err
    assert "unwrapped" not in captured.out

    # The next plan is of what is there now, and the unwrap that follows does it.
    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    assert claude_json.exists() == stays


def test_a_file_that_goes_while_the_question_waits_is_where_the_plan_put_it(
    tmp_path, monkeypatch, capsys
) -> None:
    state = _created(tmp_path, monkeypatch)
    claude_json = state["claude_json"]

    def _the_file_goes_then_the_person_agrees(question: str) -> str:
        claude_json.unlink()
        return "y"

    rc = unwrap_cli(state, prompt=_the_file_goes_then_the_person_agrees)

    assert rc == unwrap.EXIT_OK, capsys.readouterr().err
    assert not claude_json.exists()
    assert state["project_mcp"].read_bytes() == state["originals"][state["project_mcp"]]
    assert not state["store"].backup_path(state["project"]).exists()


@pytest.mark.parametrize(
    ("since", "counted", "line", "left"),
    [
        pytest.param(
            _nothing, "not restored", "remove {path} " + _THE_FILE, None, id="the-file"
        ),
        # The site holds its block, so it counts as restored; the entry is left.
        pytest.param(
            _harness_state, "restored", "remove local:{path} " + _THE_ENTRY,
            {"numStartups": 3},
            id="the-empty-entry",
        ),
    ],
)
def test_a_rerun_after_ctrl_c_does_what_was_left_to_the_file(
    tmp_path, monkeypatch, capsys, since, counted: str, line: str, left
) -> None:
    state = _created(tmp_path, monkeypatch)
    claude_json, project = state["claude_json"], str(state["project"])
    _edit(claude_json, lambda document: since(document, project))

    def _interrupted(group) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(interpose, "_settle_created_file", _interrupted)
        rc = unwrap_cli(state, "--yes")
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_INTERRUPTED
    assert "Traceback" not in captured.err
    # The sites were written and the file was not finished. A file still there
    # that the plan removed is counted as not restored, and either way the
    # backup a re-run needs is kept.
    assert _document(claude_json) == {**(left or {}), "projects": {project: {}}}
    assert f"{counted} local:{claude_json}" in _action_lines(captured.err)
    assert state["store"].backup_path(state["project"]).exists()

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    plan_text = capsys.readouterr().out.split("It leaves untouched:")[0]
    assert _action_lines(plan_text) == [
        line.format(path=claude_json),
        f"remove tegh's wrap backup at {state['store'].backup_path(state['project'])}",
    ]
    assert _document(claude_json) == left
    assert state["project_mcp"].read_bytes() == state["originals"][state["project_mcp"]]


def test_a_rerun_after_the_file_is_gone_has_no_line_for_it(
    tmp_path, monkeypatch, capsys
) -> None:
    """An unwrap that removed the file and died before it removed its backup."""
    state = _created(tmp_path, monkeypatch)
    backup_path = state["store"].backup_path(state["project"])
    backup = backup_path.read_bytes()
    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    backup_path.write_bytes(backup)
    capsys.readouterr()

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    plan_text = capsys.readouterr().out.split("It leaves untouched:")[0]

    assert _action_lines(plan_text) == [f"remove tegh's wrap backup at {backup_path}"]
    assert not state["claude_json"].exists()
    assert not backup_path.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes a file from a read-only directory")
def test_a_removal_that_fails_is_counted_as_not_restored(
    tmp_path, monkeypatch, capsys
) -> None:
    """The file's removal is the only work left in it, and the directory refuses it."""
    state = _created(tmp_path, monkeypatch)
    claude_json, project = state["claude_json"], str(state["project"])
    # The state after an unwrap that stopped between this file and `.mcp.json`.
    _edit(claude_json, lambda document: document["projects"][project].pop("mcpServers"))

    tmp_path.chmod(0o500)
    try:
        rc = unwrap_cli(state, "--yes")
    finally:
        tmp_path.chmod(0o700)
    progress = _action_lines(capsys.readouterr().err)

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert progress == [
        f"restored project:{state['project_mcp']}",
        f"not restored local:{claude_json}",
        f"not restored user:{claude_json}",
    ]
    assert state["store"].backup_path(state["project"]).exists()

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    assert not claude_json.exists()


@pytest.mark.parametrize(
    ("planned", "since", "left"),
    [
        # The plan removes the file, and it has gained a key: kept, holding it.
        pytest.param(_nothing, _harness_state, {"numStartups": 3}, id="planned-removed"),
        # The plan only takes the empty entry out, so the file is never removed.
        pytest.param(_harness_state, _no_harness_state, {}, id="planned-entry-removed"),
        # The plan has no line for the file, so it is not written at all.
        pytest.param(
            _project_state, _no_project_state, {"projects": {"{project}": {}}},
            id="planned-untouched",
        ),
    ],
)
def test_settling_a_file_that_changed_after_the_recheck_never_removes_it(
    tmp_path, monkeypatch, planned, since, left
) -> None:
    """`_settle_created_file` alone, on a file that is not as the plan found it.

    The recheck and the settle are a few lines apart, and the harness can
    write in between. Reached by calling the function, since nothing outside
    the unwrap can be made to write at that moment.
    """
    state = _created(tmp_path, monkeypatch)
    claude_json, project = state["claude_json"], str(state["project"])
    _edit(claude_json, lambda document: planned(document, project))
    steps = unwrap.build_plan(state["store"], state["project"]).sites
    for step in steps:
        if step.changes:
            interpose.write_site(step)
    _edit(claude_json, lambda document: since(document, project))

    for group in interpose._created_files(steps):
        interpose._settle_created_file(group)

    assert _document(claude_json) == json.loads(json.dumps(left).replace("{project}", project))
