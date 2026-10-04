"""`tegh unwrap`: says what it will change, asks, and leaves no credential behind.

Three claims, each with a way to be false that this file exercises:

1. **A relocated credential ends in one place.** After an unwrap it is in the
   harness config and in no file tegh keeps. Checked by scanning every file
   under the test's tree for the value, since "the store no longer lists it" is
   a statement about one file.
2. **Nothing changes without a yes.** A "no", an end-of-file, and a stdin or a
   stdout that is not a terminal all leave every byte where it was. The
   non-terminal and end-of-file cases run `tegh` as a real subprocess with a
   real pipe, a real closed descriptor or a real pseudo-terminal; only the
   plain "n" answers use the prompt seam.
3. **The plan and the report name only what happens.** A wrap records every
   site a harness has, so the report used to list a `.mcp.json` that never
   existed. The plan and the report are compared with each other and with the
   disk.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import interpose, unwrap  # noqa: E402
from tegh.harnesses import claude_code  # noqa: E402
from tegh.store import TeghStore, TeghStoreError  # noqa: E402
from tegh.tests.wrapping import LEDGER_FIELD as _FIELD  # noqa: E402
from tegh.tests.wrapping import LEDGER_SECRET as _SECRET  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    REPO,
    files_holding,
    fingerprint,
    scripted,
    site_files,
    wrap_answering,
)
from tegh.tests.wrapping import unwrap_cli as _unwrap  # noqa: E402

#: An action line of the plan or the report: two spaces, a verb, the rest.
_ACTION_LINE = re.compile(r"^  (restored?|put back|removed?)\s{2,}(.+)$")
_TENSE = {"restore": "restored", "put back": "put back", "remove": "removed"}


def _actions(text: str) -> list[tuple[str, str]]:
    return [
        (match.group(1), match.group(2))
        for line in text.splitlines()
        if (match := _ACTION_LINE.match(line))
    ]


def _child_env(wrapped: dict) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO),
        "TEGH_HOME": str(wrapped["tegh_home"]),
    }


def _unwrap_argv(wrapped: dict) -> list[str]:
    return [sys.executable, "-m", "tegh.cli", "unwrap", "--project", str(wrapped["project"])]


# ---------------------------------------------------------------------------
# The credential ends in one place
# ---------------------------------------------------------------------------


def test_unwrap_removes_the_relocated_credential_from_the_store(wrapped, capsys) -> None:
    store, project = wrapped["store"], wrapped["project"]
    secrets_path = store.secrets_path(project)
    assert files_holding(wrapped["root"], _SECRET) == [secrets_path]

    assert _unwrap(wrapped, "--yes") == 0
    captured = capsys.readouterr()

    assert wrapped["claude_json"].read_bytes() == wrapped["before"]
    assert files_holding(wrapped["root"], _SECRET) == [wrapped["claude_json"]], (
        "a copy of the relocated credential outlived the unwrap"
    )
    # The file stays, empty and owner-only: the state `ensure_secrets_file`
    # creates, and what the gateway's environment expects to find.
    assert store.read_secrets(project) == {}
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    assert not store.backup_path(project).exists()

    assert ("removed", f"credential ledger env.{_FIELD}, from tegh's store at {secrets_path}") in (
        _actions(captured.out)
    ), captured.out
    assert _SECRET not in captured.out + captured.err


def test_rewrap_after_unwrap_relocates_the_credential_again(wrapped, capsys) -> None:
    assert _unwrap(wrapped, "--yes") == 0
    assert wrap_answering(wrapped, scripted([""])) == 0

    store, project = wrapped["store"], wrapped["project"]
    assert files_holding(wrapped["root"], _SECRET) == [store.secrets_path(project)]
    assert json.loads(store.read_secrets(project)["ledger"]) == {_FIELD: _SECRET}

    assert _unwrap(wrapped, "--yes") == 0
    assert wrapped["claude_json"].read_bytes() == wrapped["before"]
    assert files_holding(wrapped["root"], _SECRET) == [wrapped["claude_json"]]
    assert _SECRET not in "".join(capsys.readouterr())


# ---------------------------------------------------------------------------
# Nothing changes without a yes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["n", "N", "no", "not sure"])
def test_anything_but_yes_changes_nothing(wrapped, capsys, answer: str) -> None:
    before = fingerprint(wrapped["root"])

    rc = _unwrap(wrapped, prompt=scripted([answer]))
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert fingerprint(wrapped["root"]) == before
    assert captured.err.strip() == "NOT UNWRAPPED: nothing was changed."
    # The plan was shown before the question, and no report followed it.
    assert "restore" in [verb for verb, _ in _actions(captured.out)]
    assert "unwrapped" not in captured.out
    assert _SECRET not in captured.out + captured.err


@pytest.mark.parametrize("answer", ["", "y", "Y", "yes"])
def test_a_yes_at_the_prompt_unwraps(wrapped, capsys, answer: str) -> None:
    assert _unwrap(wrapped, prompt=scripted([answer])) == 0
    assert files_holding(wrapped["root"], _SECRET) == [wrapped["claude_json"]]


@pytest.mark.parametrize(
    "stdin",
    [
        pytest.param({"stdin": subprocess.DEVNULL}, id="null-stdin"),
        pytest.param({"input": ""}, id="empty-pipe"),
        # A piped "y" is not a person at a terminal. `--yes` is how a script
        # agrees, and the pipe must not become a second way to.
        pytest.param({"input": "y\n"}, id="piped-yes"),
    ],
)
def test_a_stdin_that_is_not_a_terminal_refuses_and_names_the_flag(wrapped, stdin) -> None:
    before = fingerprint(wrapped["root"])

    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        _unwrap_argv(wrapped), env=_child_env(wrapped), cwd=REPO,
        capture_output=True, text=True, check=False, timeout=60, **stdin,
    )

    assert done.returncode == unwrap.EXIT_NOT_UNWRAPPED, done.stderr
    assert fingerprint(wrapped["root"]) == before
    assert "Traceback" not in done.stderr
    assert "--yes" in done.stderr
    assert "Nothing was changed" in done.stderr
    assert len(done.stderr.strip().splitlines()) == 1, done.stderr
    assert _SECRET not in done.stdout + done.stderr


def test_a_closed_stdin_refuses_without_a_traceback(wrapped) -> None:
    """`tegh unwrap <&-`: descriptor 0 is CLOSED, which is not the same as empty.

    Python then starts with `sys.stdin` as None, so asking it whether it is a
    terminal is an AttributeError unless the check expects that. The shell does
    the closing, because that is where a person meets this.
    """
    before = fingerprint(wrapped["root"])

    done = subprocess.run(  # noqa: S603 - fixed argv; the shell only closes fd 0
        ["/bin/sh", "-c", 'exec "$@" <&-', "sh", *_unwrap_argv(wrapped)],
        env=_child_env(wrapped), cwd=REPO,
        capture_output=True, text=True, check=False, timeout=60,
    )

    assert done.returncode == unwrap.EXIT_NOT_UNWRAPPED, done.stderr
    assert fingerprint(wrapped["root"]) == before
    assert "Traceback" not in done.stderr
    # stdout is this test's capture pipe, so both ends are named.
    assert done.stderr.startswith("REFUSED: stdin and stdout are not terminals"), done.stderr
    assert "--yes" in done.stderr
    assert len(done.stderr.strip().splitlines()) == 1, done.stderr
    assert _SECRET not in done.stdout + done.stderr


def _on_a_terminal(
    wrapped: dict, typed: bytes, *, stdout_is_terminal: bool = True
) -> tuple[int, str, str]:
    """Run `tegh unwrap` with a pseudo-terminal on stdin. Returns `(status, out, err)`.

    stdout is the same terminal unless `stdout_is_terminal` is False, in which
    case it is a pipe: what `tegh unwrap > out.txt` gives a person who is
    sitting at the keyboard.
    """
    pty = pytest.importorskip("pty", reason="needs a pseudo-terminal")
    master, slave = pty.openpty()
    shown: list[bytes] = []

    def _drain() -> None:
        # Until the last holder of the terminal exits: end-of-file on macOS,
        # EIO on Linux.
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                return
            if not chunk:
                return
            shown.append(chunk)

    reader = threading.Thread(target=_drain, daemon=True)
    try:
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            _unwrap_argv(wrapped), env=_child_env(wrapped), cwd=REPO, stdin=slave,
            stdout=slave if stdout_is_terminal else subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        reader.start()
        os.write(master, typed)
        piped, err = child.communicate(timeout=60)
        reader.join(timeout=10)
    finally:
        os.close(master)
    out = b"".join(shown) if stdout_is_terminal else piped
    return child.returncode, out.decode("utf-8"), err.decode("utf-8")


@pytest.mark.parametrize(
    ("typed", "status", "says"),
    [
        pytest.param(b"n\n", unwrap.EXIT_NOT_UNWRAPPED, "nothing was changed", id="n"),
        # Ctrl-D on an empty line: a real end-of-file on a real terminal.
        pytest.param(b"\x04", unwrap.EXIT_NOT_UNWRAPPED, "--yes", id="end-of-file"),
        pytest.param(b"y\n", unwrap.EXIT_OK, "", id="y"),
        # Bare Enter takes the capitalised default, which is yes.
        pytest.param(b"\n", unwrap.EXIT_OK, "", id="enter"),
    ],
)
def test_the_prompt_on_a_real_terminal(wrapped, typed: bytes, status: int, says: str) -> None:
    before = fingerprint(wrapped["root"])

    returncode, out, err = _on_a_terminal(wrapped, typed)

    assert returncode == status, err
    assert unwrap.QUESTION in out
    assert "Traceback" not in err
    assert says in err
    assert _SECRET not in out + err
    if status == unwrap.EXIT_OK:
        assert files_holding(wrapped["root"], _SECRET) == [wrapped["claude_json"]]
    else:
        assert fingerprint(wrapped["root"]) == before


@pytest.mark.parametrize("typed", [b"\n", b"y\n"], ids=["enter", "y"])
def test_a_stdout_that_is_not_a_terminal_refuses_even_with_a_person_typing(
    wrapped, typed: bytes
) -> None:
    """`tegh unwrap > out.txt`: the plan and the question went into the file.

    The person at the keyboard saw nothing, so their Enter is not agreement to
    anything. The question is never asked, and the flag is named.
    """
    before = fingerprint(wrapped["root"])

    returncode, out, err = _on_a_terminal(wrapped, typed, stdout_is_terminal=False)

    assert returncode == unwrap.EXIT_NOT_UNWRAPPED, err
    assert fingerprint(wrapped["root"]) == before
    assert unwrap.QUESTION not in out
    assert err.startswith("REFUSED: stdout is not a terminal"), err
    assert "--yes" in err and "Nothing was changed" in err
    assert len(err.strip().splitlines()) == 1, err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# The order fails toward keeping the credential
# ---------------------------------------------------------------------------


def _write_refused(wrapped: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness home cannot be written: the restore raises."""
    wrapped["home"].chmod(0o500)


def _write_does_not_land(wrapped: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write returns and the file is unchanged: only the read-back sees it."""
    monkeypatch.setattr(interpose, "_write_block", lambda *args, **kwargs: None)


@pytest.mark.parametrize("break_restore", [_write_refused, _write_does_not_land])
def test_a_failed_restore_leaves_the_store_copy(
    wrapped, capsys, monkeypatch, break_restore
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only directory")
    store, project = wrapped["store"], wrapped["project"]
    before = fingerprint(wrapped["root"])

    break_restore(wrapped, monkeypatch)
    try:
        rc = _unwrap(wrapped, "--yes")
    finally:
        wrapped["home"].chmod(0o700)
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert fingerprint(wrapped["root"]) == before, "a failed restore still changed something"
    assert json.loads(store.read_secrets(project)["ledger"]) == {_FIELD: _SECRET}
    assert "the harness config was not restored" in captured.err
    assert f"still in tegh's store at {store.secrets_path(project)}" in captured.err
    assert "unwrapped" not in captured.out
    assert _SECRET not in captured.out + captured.err


def test_a_failed_removal_names_the_file_and_a_rerun_finishes(
    wrapped, capsys, monkeypatch
) -> None:
    store, project = wrapped["store"], wrapped["project"]
    secrets_path = store.secrets_path(project)

    def _refuse(self, *args, **kwargs):
        raise TeghStoreError("the store is read-only")

    with monkeypatch.context() as patched:
        patched.setattr(TeghStore, "remove_secret_fields", _refuse)
        rc = _unwrap(wrapped, "--yes")
    captured = capsys.readouterr()

    assert rc == unwrap.EXIT_NOT_UNWRAPPED
    assert wrapped["claude_json"].read_bytes() == wrapped["before"]
    assert f"{secrets_path} still holds a copy of credential ledger env.{_FIELD}" in captured.err
    assert store.backup_path(project).exists(), "the backup a rerun needs was consumed"
    assert _SECRET not in captured.out + captured.err

    # The rerun has one thing left to do, and says only that.
    assert _unwrap(wrapped, "--yes") == 0
    report = capsys.readouterr().out
    assert files_holding(wrapped["root"], _SECRET) == [wrapped["claude_json"]]
    assert {verb for verb, _ in _actions(report)} == {"remove", "removed"}, report


# ---------------------------------------------------------------------------
# The plan and the report name only what happens
# ---------------------------------------------------------------------------


def test_the_plan_and_the_report_agree_and_name_no_file_that_never_existed(
    wrapped, capsys
) -> None:
    project_mcp = wrapped["project"] / ".mcp.json"
    assert not project_mcp.exists()
    existed = {str(path) for path in wrapped["root"].rglob("*") if path.is_file()}

    assert _unwrap(wrapped, "--yes") == 0
    out = capsys.readouterr().out
    plan_text, report_text = out.split("It leaves untouched:")

    planned, reported = _actions(plan_text), _actions(report_text)
    assert planned, out
    assert [(_TENSE[verb], what) for verb, what in planned] == reported

    assert not project_mcp.exists()
    assert ".mcp.json" not in out, "a scope with nothing to restore was reported"
    assert "user:" not in out, "the user scope held no block and was reported"
    # One line per site written, and every path an action names was a real file.
    assert [what for verb, what in reported if verb == "restored"] == [
        f"local:{wrapped['claude_json']}  (ledger)"
    ]
    named = set(re.findall(r"(/\S+?)(?:\s|$)", "\n".join(what for _, what in reported)))
    assert named and named <= existed, named - existed


_NOTES = {"notes": {"command": "notes-server"}}
_FETCH = {"fetch": {"command": "fetch-server"}}


@pytest.mark.parametrize(
    ("local", "project_scope", "user", "expected"),
    [
        pytest.param(_NOTES, None, None, [("restore", "local", "(notes)")], id="local-only"),
        pytest.param(
            None, _NOTES, None,
            # The local scope had no block. The wrap added one for the gateway,
            # so what the unwrap does there is a removal, and it says so.
            [("remove", "local", "(the mcpServers block the wrap added)"),
             ("restore", "project", "(notes)")],
            id="project-only",
        ),
        pytest.param(
            _NOTES, None, _FETCH,
            [("restore", "local", "(notes)"), ("restore", "user", "(fetch)")],
            id="local-and-user",
        ),
    ],
)
def test_only_sites_the_unwrap_changes_are_listed(
    tmp_path, local, project_scope, user, expected
) -> None:
    project, claude_json, project_mcp = site_files(
        tmp_path, local=local, project_scope=project_scope, user=user
    )
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (claude_json, project_mcp)
    }
    sites, gateway_site = claude_code.config_sites(
        project, claude_json_path=claude_json, project_mcp_path=project_mcp
    )
    backup = interpose.apply_interposition(
        interpose.plan_interposition(
            sites=sites, gateway_site=gateway_site, gateway_entry={"command": "tegh"}
        ),
        wrapped_at="t", project=project, harness="claude-code",
    )

    steps = interpose.plan_restore(backup)
    plan = unwrap.UnwrapPlan(
        project=project, backup=backup, backup_path=tmp_path / "wrap-backup.json",
        secrets_path=tmp_path / "secrets.json", audit_path=tmp_path / "audit.jsonl",
        sites=steps, credentials=[],
    )
    listed = [
        (action.planned, *action.what.split("  ", 1)) for action in plan.actions[:-1]
    ]
    paths = {"local": claude_json, "project": project_mcp, "user": claude_json}
    assert listed == [
        (verb, f"{scope}:{paths[scope]}", detail) for verb, scope, detail in expected
    ]
    assert plan.actions[-1].what.startswith("tegh's wrap backup at ")

    interpose.apply_restore(steps)
    for path, original in originals.items():
        assert (path.read_bytes() if path.exists() else None) == original
    # Done means done: a second plan over the same backup has nothing to list.
    assert not [step for step in interpose.plan_restore(backup) if step.changes]


# ---------------------------------------------------------------------------
# The store's half
# ---------------------------------------------------------------------------


def _leaf(**fields: str) -> str:
    return json.dumps(fields, sort_keys=True)


@pytest.mark.parametrize(
    ("stored", "remove", "left"),
    [
        pytest.param(
            {"ledger": {"KEY": "a"}}, {"ledger": ["KEY"]}, {}, id="last-field-drops-the-leaf"
        ),
        pytest.param(
            {"ledger": {"KEY": "a", "OTHER": "b"}}, {"ledger": ["KEY"]},
            {"ledger": _leaf(OTHER="b")}, id="other-fields-stay",
        ),
        pytest.param(
            {"ledger": {"KEY": "a"}, "vendor": {"TOKEN": "c"}}, {"ledger": ["KEY"]},
            {"vendor": _leaf(TOKEN="c")}, id="other-leaves-stay",
        ),
        pytest.param(
            {"vendor": {"TOKEN": "c"}}, {"ledger": ["KEY"], "vendor": ["ABSENT"]},
            {"vendor": _leaf(TOKEN="c")}, id="already-absent-is-not-an-error",
        ),
    ],
)
def test_remove_secret_fields(tmp_path, stored, remove, left: dict) -> None:
    store = TeghStore(home=tmp_path / "tegh")
    project = tmp_path / "widget"
    for leaf, fields in stored.items():
        store.write_secret_leaf(project, leaf, fields)

    store.remove_secret_fields(project, remove)

    path = store.secrets_path(project)
    assert store.read_secrets(project) == left
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.with_name(path.name + ".tegh-tmp").exists()


def test_remove_secret_fields_refuses_a_leaf_it_cannot_read(tmp_path) -> None:
    store = TeghStore(home=tmp_path / "tegh")
    project = tmp_path / "widget"
    path = store.ensure_secrets_file(project)
    path.write_text(json.dumps({"ledger": "an-opaque-string"}), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(TeghStoreError, match="ledger"):
        store.remove_secret_fields(project, {"ledger": ["KEY"]})
    assert path.read_bytes() == before
