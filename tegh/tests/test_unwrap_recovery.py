"""`tegh unwrap` when the config is not as the wrap left it, or the unwrap stops.

`test_unwrap.py` covers the straight path and the prompt. This file is the rest:

1. **Only what the wrap put there comes out.** A server added to a wrapped scope
   since the wrap is kept and named; one that collides with a server being
   restored is refused; one that appears while the question waits stops the
   unwrap [ruling: maintainer, 2026-10-04].
2. **A restore that stops part-way says where the credential now is.** Some
   sites restored and one not means the value is in a config AND in the store.
3. **A re-run after an interrupted unwrap finishes, and asks for nothing.**
4. **A refusal is one line.** A damaged backup or an unreadable file is not a
   traceback, and no message carries a value.

Most cases start from `wrapping.interposed`, which writes the state a wrap
leaves without running the admission ceremony an unwrap never reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tegh import interpose, unwrap
from tegh.store import TeghStore
from tegh.tests.wrapping import files_holding, fingerprint, interposed, scripted, unwrap_cli

_KEY_ONE = "not-a-real-credential-one"
_KEY_TWO = "not-a-real-credential-two"
_LEDGER = {"ledger": {"command": "ledger-server", "env": {"LEDGER_API_KEY": _KEY_ONE}}}
_VENDOR = {"vendor": {"command": "vendor-server", "env": {"VENDOR_TOKEN": _KEY_TWO}}}
_NOTES = {"notes": {"command": "notes-server"}}
_BAR = {"command": "bar-server"}


def _block_at(state: dict, scope: str) -> dict | None:
    """The `mcpServers` block one scope holds now; None when it has none."""
    if scope == "project":
        if not state["project_mcp"].exists():
            return None
        return json.loads(state["project_mcp"].read_text(encoding="utf-8")).get("mcpServers")
    document = json.loads(state["claude_json"].read_text(encoding="utf-8"))
    if scope == "local":
        document = document["projects"][str(state["project"])]
    return document.get("mcpServers")


def _add_server(state: dict, scope: str, name: str, entry: dict) -> None:
    """What `claude mcp add` does to one scope: one more entry, the rest untouched."""
    path = state["project_mcp"] if scope == "project" else state["claude_json"]
    document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    holder = document["projects"][str(state["project"])] if scope == "local" else document
    holder.setdefault("mcpServers", {})[name] = entry
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")


def _label(state: dict, scope: str) -> str:
    path = state["project_mcp"] if scope == "project" else state["claude_json"]
    return f"{scope}:{path}"


def _action_lines(text: str) -> list[str]:
    """The plan's or report's action lines, with the column padding collapsed."""
    return [" ".join(line.split()) for line in text.splitlines() if line.startswith("  ")]


def _assert_unwrapped(state: dict) -> None:
    """Every credential is in a harness config and in nothing tegh keeps."""
    store, project = state["store"], state["project"]
    configs = {state["claude_json"], state["project_mcp"]}
    assert store.read_secrets(project) == {}
    assert not store.backup_path(project).exists()
    for secret in state["secrets"]:
        holders = files_holding(state["root"], secret)
        assert holders and set(holders) <= configs, (secret, holders)


def _assert_no_value_printed(state: dict, captured) -> None:
    for secret in state["secrets"]:
        assert secret not in captured.out + captured.err


# ---------------------------------------------------------------------------
# Credentials at more than one coordinate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blocks", "put_back"),
    [
        pytest.param(
            {"local": _LEDGER, "project_scope": _VENDOR},
            [("local", "ledger env.LEDGER_API_KEY"), ("project", "vendor env.VENDOR_TOKEN")],
            id="two-sites",
        ),
        pytest.param(
            {"local": {"ledger": {"command": "x", "env": {"KEY": _KEY_ONE, "TOKEN": _KEY_TWO}}}},
            [("local", "ledger env.KEY"), ("local", "ledger env.TOKEN")],
            id="two-fields-in-one-leaf",
        ),
        pytest.param(
            {"local": {**_LEDGER, **_VENDOR}},
            [("local", "ledger env.LEDGER_API_KEY"), ("local", "vendor env.VENDOR_TOKEN")],
            id="two-servers-in-one-scope",
        ),
        pytest.param(
            {"local": _LEDGER, "user": _VENDOR},
            [("local", "ledger env.LEDGER_API_KEY"), ("user", "vendor env.VENDOR_TOKEN")],
            id="user-and-local-scope-in-one-file",
        ),
    ],
)
def test_every_relocated_credential_goes_back_to_its_own_coordinate(
    tmp_path, monkeypatch, capsys, blocks: dict, put_back: list
) -> None:
    state = interposed(tmp_path, monkeypatch, **blocks)
    secrets_path = state["store"].secrets_path(state["project"])
    for secret in state["secrets"]:
        assert files_holding(tmp_path, secret) == [secrets_path]

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    captured = capsys.readouterr()

    for path, original in state["originals"].items():
        assert (path.read_bytes() if path.exists() else None) == original
    _assert_unwrapped(state)
    _assert_no_value_printed(state, captured)
    plan_text = captured.out.split("It leaves untouched:")[0]
    for scope, name in put_back:
        assert f"put back credential {name}, into {_label(state, scope)}" in _action_lines(plan_text)
        assert f"remove credential {name}, from tegh's store at {secrets_path}" in (
            _action_lines(plan_text)
        )


# ---------------------------------------------------------------------------
# Only what the wrap put there comes out
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blocks", "added_at", "planned", "left"),
    [
        pytest.param(
            # The scope had no block before the wrap and the wrap gave it none.
            # A `.mcp.json` created since is not "the block the wrap added".
            {"local": _NOTES}, "project",
            ["restore {local} (notes)", "keep {project} (bar), added since the wrap"],
            {"local": _NOTES, "project": {"bar": _BAR}},
            id="a-block-created-since-the-wrap",
        ),
        pytest.param(
            # `claude mcp add bar`, run while wrapped: bar sits beside the gateway.
            {"local": _NOTES}, "local",
            ["restore {local} (notes)", "keep {local} (bar), added since the wrap"],
            {"local": {**_NOTES, "bar": _BAR}, "project": None},
            id="an-entry-beside-the-gateway",
        ),
        pytest.param(
            # Beside the gateway in a scope that had no block: the gateway entry
            # goes, the block stays, and neither line says the block is removed.
            {"project_scope": _NOTES}, "local",
            ["remove {local} (the tegh entry the wrap added)",
             "keep {local} (bar), added since the wrap", "restore {project} (notes)"],
            {"local": {"bar": _BAR}, "project": _NOTES},
            id="beside-the-gateway-where-no-block-was",
        ),
    ],
)
def test_a_server_added_since_the_wrap_is_kept_and_named(
    tmp_path, monkeypatch, capsys, blocks: dict, added_at: str, planned: list, left: dict
) -> None:
    state = interposed(tmp_path, monkeypatch, **blocks)
    _add_server(state, added_at, "bar", _BAR)
    labels = {scope: _label(state, scope) for scope in ("local", "project")}
    backup = f"remove tegh's wrap backup at {state['store'].backup_path(state['project'])}"

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err
    plan_text, report_text = capsys.readouterr().out.split("It leaves untouched:")

    assert _action_lines(plan_text) == [line.format(**labels) for line in planned] + [backup]
    assert "the mcpServers block the wrap added" not in plan_text
    kept = [line for line in _action_lines(report_text) if line.startswith("kept ")]
    assert kept == [f"kept {labels[added_at]} (bar), added since the wrap"]
    for scope, block in left.items():
        assert _block_at(state, scope) == block, scope
    # Done means done: the backup is gone, so there is nothing to run again.
    assert not state["store"].backup_path(state["project"]).exists()


def test_an_added_server_under_a_restored_servers_name_is_refused(
    tmp_path, monkeypatch, capsys
) -> None:
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    _add_server(state, "local", "ledger", {"command": "some-other-ledger"})
    before = fingerprint(tmp_path)

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_REFUSED
    captured = capsys.readouterr()

    assert fingerprint(tmp_path) == before
    assert captured.out == "", "a plan was shown for an unwrap that cannot be planned"
    assert len(captured.err.strip().splitlines()) == 1, captured.err
    assert captured.err.startswith(f"REFUSED: {_label(state, 'local')} now holds a server named ledger")
    assert "Rename or remove one of the two" in captured.err
    assert "Nothing was changed" in captured.err
    _assert_no_value_printed(state, captured)


def test_a_server_added_while_the_question_waits_stops_the_unwrap(
    tmp_path, monkeypatch, capsys
) -> None:
    """The plan a person agreed to did not name it, so their yes does not cover it."""
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    during: dict = {}

    def _the_harness_writes_then_the_person_agrees(question: str) -> str:
        _add_server(state, "local", "late", _BAR)
        during.update(fingerprint(tmp_path))
        return "y"

    rc = unwrap_cli(state, prompt=_the_harness_writes_then_the_person_agrees)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert fingerprint(tmp_path) == during, "the unwrap wrote after the config changed"
    assert "changed while tegh unwrap was waiting" in captured.err
    assert "the file now holds late" in captured.err
    assert "the harness config was not restored" in captured.err
    assert "unwrapped" not in captured.out
    _assert_no_value_printed(state, captured)

    # The next plan names it, and the unwrap that follows keeps it.
    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    assert "(late), added since the wrap" in capsys.readouterr().out
    assert _block_at(state, "local") == {**_LEDGER, "late": _BAR}
    _assert_unwrapped(state)


def test_a_backup_that_recorded_no_gateway_still_loses_only_the_gateway(
    tmp_path, monkeypatch, capsys
) -> None:
    """A backup written before the gateway entry was recorded in it."""
    state = interposed(tmp_path, monkeypatch, local=_NOTES)
    backup_path = state["store"].backup_path(state["project"])
    recorded = json.loads(backup_path.read_text(encoding="utf-8"))
    assert recorded.pop("gateway") == {"name": "tegh", "site": _label(state, "local")}
    backup_path.write_text(json.dumps(recorded, indent=2), encoding="utf-8")
    _add_server(state, "local", "bar", _BAR)

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err
    assert _block_at(state, "local") == {**_NOTES, "bar": _BAR}


# ---------------------------------------------------------------------------
# The gateway entry is the one that runs the gateway, whatever it is called
# ---------------------------------------------------------------------------


def _local_block(document: dict, state: dict) -> dict:
    return document["projects"][str(state["project"])]["mcpServers"]


def _renamed(document: dict, state: dict) -> None:
    block = _local_block(document, state)
    block["broker"] = block.pop("tegh")


def _renamed_with_a_server_beside_it(document: dict, state: dict) -> None:
    _renamed(document, state)
    _local_block(document, state)["bar"] = _BAR


def _moved_to_user_scope(document: dict, state: dict) -> None:
    document["mcpServers"] = {"tegh": _local_block(document, state).pop("tegh")}


def _name_taken_by_a_server(document: dict, state: dict) -> None:
    """The gateway entry replaced by a server of the user's own, under its name."""
    _local_block(document, state)["tegh"] = _BAR


def _edit_config(state: dict, change) -> None:
    document = json.loads(state["claude_json"].read_text(encoding="utf-8"))
    change(document, state)
    state["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


_RUNS_THE_GATEWAY = "which runs tegh's gateway for this project"


@pytest.mark.parametrize(
    ("blocks", "since", "planned", "left"),
    [
        pytest.param(
            {"local": _NOTES}, _renamed,
            ["restore {local} (notes)", "remove {local} (broker), " + _RUNS_THE_GATEWAY],
            {"local": _NOTES, "user": None},
            id="renamed",
        ),
        pytest.param(
            {"project_scope": _NOTES}, _renamed,
            ["remove {local} (broker), " + _RUNS_THE_GATEWAY, "restore {project} (notes)"],
            {"local": None, "user": None},
            id="renamed-where-no-block-was",
        ),
        pytest.param(
            # The entry the wrap added is named by what it is called now.
            {"project_scope": _NOTES}, _renamed_with_a_server_beside_it,
            ["remove {local} (broker), " + _RUNS_THE_GATEWAY,
             "keep {local} (bar), added since the wrap", "restore {project} (notes)"],
            {"local": {"bar": _BAR}, "user": None},
            id="renamed-beside-a-server-added-since",
        ),
        pytest.param(
            {"local": _NOTES}, _moved_to_user_scope,
            ["restore {local} (notes)", "remove {user} (tegh), " + _RUNS_THE_GATEWAY],
            {"local": _NOTES, "user": None},
            id="moved-to-another-scope",
        ),
        pytest.param(
            # Called what the wrap called its entry, and running something
            # else: the user's, and not tegh's to remove.
            {"local": _NOTES}, _name_taken_by_a_server,
            ["restore {local} (notes)", "keep {local} (tegh), added since the wrap"],
            {"local": {**_NOTES, "tegh": _BAR}, "user": None},
            id="a-server-only-called-tegh",
        ),
    ],
)
def test_the_gateway_entry_is_found_by_what_it_runs(
    tmp_path, monkeypatch, capsys, blocks: dict, since, planned: list, left: dict
) -> None:
    state = interposed(tmp_path, monkeypatch, **blocks)
    _edit_config(state, since)
    labels = {scope: _label(state, scope) for scope in ("local", "project", "user")}
    backup = f"remove tegh's wrap backup at {state['store'].backup_path(state['project'])}"

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err
    plan_text, report_text = capsys.readouterr().out.split("It leaves untouched:")

    assert _action_lines(plan_text) == [line.format(**labels) for line in planned] + [backup]
    done = [line.replace("remove ", "removed ", 1) for line in _action_lines(plan_text)]
    assert [line for line in _action_lines(report_text) if line.startswith("removed ")] == [
        line for line in done if line.startswith("removed ")
    ]
    for scope, block in left.items():
        assert _block_at(state, scope) == block, scope
    assert not state["store"].backup_path(state["project"]).exists()


def test_a_gateway_entry_that_appears_while_the_question_waits_stops_the_unwrap(
    tmp_path, monkeypatch, capsys
) -> None:
    """Its removal is not in the plan the person agreed to."""
    state = interposed(tmp_path, monkeypatch, local=_NOTES)
    during: dict = {}

    def _a_copy_is_made_then_the_person_agrees(question: str) -> str:
        _add_server(state, "local", "broker", _block_at(state, "local")["tegh"])
        during.update(fingerprint(tmp_path))
        return "y"

    rc = unwrap_cli(state, prompt=_a_copy_is_made_then_the_person_agrees)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert fingerprint(tmp_path) == during, "the unwrap wrote after the config changed"
    assert (
        f"{_label(state, 'local')} changed while tegh unwrap was waiting: it now "
        "holds broker, running tegh's gateway for this project, which the plan did "
        "not show being removed"
    ) in captured.err

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    assert f"(broker), {_RUNS_THE_GATEWAY}" in capsys.readouterr().out
    assert _block_at(state, "local") == _NOTES


# ---------------------------------------------------------------------------
# A restore that stops part-way
# ---------------------------------------------------------------------------


def _write_block_patched(monkeypatch: pytest.MonkeyPatch, replacement) -> None:
    """Stand `replacement(real, site, block, **kwargs)` in for `interpose._write_block`."""
    real = interpose._write_block
    monkeypatch.setattr(
        interpose, "_write_block",
        lambda site, block, **kwargs: replacement(real, site, block, **kwargs),
    )


def _assert_half_done(state: dict, err: str) -> None:
    """The local scope is restored, the project scope is not, and the message says so."""
    store, project = state["store"], state["project"]
    secrets_path = store.secrets_path(project)
    assert "only partly restored" in err
    assert f"  restored      {_label(state, 'local')}" in err.splitlines()
    assert f"  not restored  {_label(state, 'project')}" in err.splitlines()
    assert f"The store copy was kept: every credential of this wrap is still in tegh's store at {secrets_path}." in err
    assert (
        "This one is now in the harness config as well: credential ledger "
        f"env.LEDGER_API_KEY, in {_label(state, 'local')}."
    ) in err
    assert "credential vendor env.VENDOR_TOKEN, in" not in err
    assert "Run `tegh unwrap` again" in err and "finishes the job" in err
    # And every one of those sentences is true of the disk.
    assert files_holding(state["root"], _KEY_ONE) == sorted([state["claude_json"], secrets_path])
    assert files_holding(state["root"], _KEY_TWO) == [secrets_path]
    assert set(store.read_secrets(project)) == {"ledger", "vendor"}
    assert store.backup_path(project).exists()


def test_a_partial_restore_says_which_site_now_holds_the_credential_too(
    tmp_path, monkeypatch, capsys
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only directory")
    state = interposed(tmp_path, monkeypatch, local=_LEDGER, project_scope=_VENDOR)

    state["project"].chmod(0o500)  # `.mcp.json` can be read and cannot be replaced
    try:
        rc = unwrap_cli(state, "--yes")
    finally:
        state["project"].chmod(0o700)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert captured.err.startswith("FAILED: the harness config was only partly restored: ")
    _assert_half_done(state, captured.err)
    assert "unwrapped" not in captured.out
    assert not list(tmp_path.rglob("*.tegh-tmp"))
    _assert_no_value_printed(state, captured)

    # The re-run finds the local scope done, and does the rest.
    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    rerun = capsys.readouterr().out.split("It leaves untouched:")[0]
    assert not [line for line in _action_lines(rerun) if _label(state, "local") in line]
    assert f"restore {_label(state, 'project')} (vendor)" in _action_lines(rerun)
    for path, original in state["originals"].items():
        assert path.read_bytes() == original
    _assert_unwrapped(state)


def test_ctrl_c_during_the_restore_reports_how_far_it_got(tmp_path, monkeypatch, capsys) -> None:
    state = interposed(tmp_path, monkeypatch, local=_LEDGER, project_scope=_VENDOR)
    written: list = []

    def _interrupted_on_the_second_site(real, site, block, **kwargs):
        if written:
            raise KeyboardInterrupt
        written.append(site)
        return real(site, block, **kwargs)

    with monkeypatch.context() as patched:
        _write_block_patched(patched, _interrupted_on_the_second_site)
        rc = unwrap_cli(state, "--yes")
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_INTERRUPTED == 130
    assert captured.err.startswith("INTERRUPTED: tegh unwrap was stopped before it finished.")
    assert "Traceback" not in captured.err
    _assert_half_done(state, captured.err)
    _assert_no_value_printed(state, captured)

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    _assert_unwrapped(state)


def test_a_write_that_lands_with_other_content_is_caught_by_the_read_back(
    tmp_path, monkeypatch, capsys
) -> None:
    """The write returns and the file is not what was meant: here, the kept entry is gone.

    The read-back compares with the MERGED block, so a restore that put the
    pre-wrap servers back and lost the added one does not verify.
    """
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    _add_server(state, "local", "bar", _BAR)

    def _drops_the_kept_entry(real, site, block, **kwargs):
        return real(site, {k: v for k, v in block.items() if k != "bar"}, **kwargs)

    _write_block_patched(monkeypatch, _drops_the_kept_entry)
    rc = unwrap_cli(state, "--yes")
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert "does not hold the restored block" in captured.err
    assert "the harness config was not restored" in captured.err
    assert json.loads(state["store"].read_secrets(state["project"])["ledger"]) == {
        "LEDGER_API_KEY": _KEY_ONE
    }
    assert state["store"].backup_path(state["project"]).exists()
    _assert_no_value_printed(state, captured)


def test_a_removal_that_returns_without_removing_is_caught_by_the_reread(
    tmp_path, monkeypatch, capsys
) -> None:
    """`remove_secret_fields` came back and the store still holds the value."""
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    secrets_path = state["store"].secrets_path(state["project"])

    with monkeypatch.context() as patched:
        patched.setattr(TeghStore, "remove_secret_fields", lambda self, *args, **kwargs: None)
        rc = unwrap_cli(state, "--yes")
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert "still present after the write: credential ledger env.LEDGER_API_KEY" in captured.err
    assert f"{secrets_path} still holds a copy of" in captured.err
    assert state["store"].backup_path(state["project"]).exists(), "the backup a re-run needs is gone"
    assert "unwrapped" not in captured.out
    _assert_no_value_printed(state, captured)


# ---------------------------------------------------------------------------
# A re-run after an interrupted unwrap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocks",
    [
        pytest.param({"local": _LEDGER}, id="one-credential"),
        pytest.param({"local": _LEDGER, "user": _VENDOR}, id="two-scopes"),
    ],
)
def test_a_rerun_after_the_store_removal_finishes_without_asking_for_the_value(
    tmp_path, monkeypatch, capsys, blocks: dict
) -> None:
    """The process died after the store removal and before the backup went.

    The config holds the credential and the store does not. The backup's
    reference resolves nowhere in the store, and the re-run must not read that
    as a lost credential.
    """
    state = interposed(tmp_path, monkeypatch, **blocks)
    backup_path = state["store"].backup_path(state["project"])
    kept_backup = backup_path.read_bytes()
    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    backup_path.write_bytes(kept_backup)
    capsys.readouterr()
    restored = state["claude_json"].read_bytes()

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK
    captured = capsys.readouterr()

    assert captured.err == ""
    plan_text, report_text = captured.out.split("It leaves untouched:")
    assert _action_lines(plan_text) == [f"remove tegh's wrap backup at {backup_path}"]
    assert _action_lines(report_text)[-1] == f"removed tegh's wrap backup at {backup_path}"
    assert plan_text.count("Already restored: ") == len(state["secrets"])
    assert (
        f"Already restored: {_label(state, 'local')} holds a value for credential "
        "ledger env.LEDGER_API_KEY and tegh's store no longer does"
    ) in plan_text
    assert state["claude_json"].read_bytes() == restored == state["originals"][state["claude_json"]]
    _assert_unwrapped(state)
    _assert_no_value_printed(state, captured)


def test_a_credential_in_neither_place_sends_nobody_to_write_it_into_tegh(
    tmp_path, monkeypatch, capsys
) -> None:
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    secrets_path = state["store"].secrets_path(state["project"])
    secrets_path.write_text("{}", encoding="utf-8")
    before = fingerprint(tmp_path)

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_REFUSED
    captured = capsys.readouterr()

    assert fingerprint(tmp_path) == before
    assert len(captured.err.strip().splitlines()) == 1, captured.err
    assert "ledger env.LEDGER_API_KEY" in captured.err
    assert f"{_label(state, 'local')} does not hold it either" in captured.err
    assert "back to that harness config by hand" in captured.err
    # The only place it may send a value is the harness config.
    for wrong in ("put the value back in that file", "edit the backup", "to hold the literal"):
        assert wrong not in captured.err


# ---------------------------------------------------------------------------
# A refusal is one line
# ---------------------------------------------------------------------------


def _truncate(path: Path) -> None:
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])


def _edit_backup(change):
    def _apply(path: Path) -> None:
        recorded = json.loads(path.read_text(encoding="utf-8"))
        change(recorded)
        path.write_text(json.dumps(recorded), encoding="utf-8")

    return _apply


def _unknown_scope(recorded: dict) -> None:
    recorded["sites"][0]["scope"] = "galactic"


def _reference_without_a_leaf(recorded: dict) -> None:
    reference = recorded["sites"][0]["block"]["ledger"]["env"]["LEDGER_API_KEY"]
    del reference[interpose.SECRET_REFERENCE_KEY]["leaf"]


def _unreadable(path: Path) -> None:
    path.chmod(0o000)


@pytest.mark.parametrize(
    ("which", "damage", "says"),
    [
        pytest.param("backup", _truncate, "is not valid JSON", id="truncated-backup"),
        pytest.param("backup", lambda p: p.write_text("{}"), "has no 'project' key", id="empty-backup"),
        pytest.param("backup", lambda p: p.write_text("[]"), "the shape tegh writes", id="backup-is-a-list"),
        pytest.param("backup", _edit_backup(_unknown_scope), "'galactic'", id="unknown-scope"),
        pytest.param(
            "backup", _edit_backup(_reference_without_a_leaf), "has no 'leaf' key",
            id="reference-without-a-leaf",
        ),
        pytest.param("backup", _unreadable, "Permission denied", id="unreadable-backup"),
        pytest.param("secrets", _unreadable, "Permission denied", id="unreadable-secrets"),
        pytest.param("site", _unreadable, "Permission denied", id="unreadable-site"),
        pytest.param("site", _truncate, "is not valid JSON", id="truncated-site"),
    ],
)
def test_a_file_tegh_cannot_use_is_a_one_line_refusal(
    tmp_path, monkeypatch, capsys, which: str, damage, says: str
) -> None:
    if damage is _unreadable and os.geteuid() == 0:
        pytest.skip("root reads through a mode of 000")
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    store, project = state["store"], state["project"]
    path = {
        "backup": store.backup_path(project),
        "secrets": store.secrets_path(project),
        "site": state["claude_json"],
    }[which]
    mode = path.stat().st_mode
    damage(path)
    before = fingerprint(tmp_path) if damage is not _unreadable else None

    try:
        # A traceback would be an exception here, and fail the test as one.
        rc = unwrap_cli(state, "--yes")
    finally:
        path.chmod(mode)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_REFUSED
    assert captured.out == ""
    assert captured.err.startswith("REFUSED: ")
    assert len(captured.err.strip().splitlines()) == 1, captured.err
    assert says in captured.err
    assert str(path) in captured.err, "the refusal does not name the file"
    _assert_no_value_printed(state, captured)
    if before is not None:
        assert fingerprint(tmp_path) == before
    assert files_holding(tmp_path, _KEY_ONE) == [store.secrets_path(project)]


def test_the_question_is_not_asked_when_the_plan_cannot_be_made(tmp_path, monkeypatch, capsys) -> None:
    """A refusal comes before the question, so nobody agrees to nothing."""
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    _truncate(state["store"].backup_path(state["project"]))

    assert unwrap_cli(state, prompt=scripted([])) == unwrap.EXIT_REFUSED
