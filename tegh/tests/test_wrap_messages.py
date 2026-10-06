"""What `tegh wrap` prints is followed word for word, so it has to work that way.

Three things a message can get wrong without any file being wrong:

- a command it names leaves out the tegh home the wrap was given, so the
  command looks somewhere else and refuses;
- a ceremony's failure arrives as a Python traceback folded into a sentence;
- a Ctrl-C outside a wrap's own span arrives as a traceback of tegh's.

Each is driven through the real command, and each printed command is run.
"""

from __future__ import annotations

import json
import re
import shlex
import sys

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import cli  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.lockfile import lock_paths  # noqa: E402
from tegh.tests.wrapping import TOY, scripted, store_of, wrap_argv  # noqa: E402
from tegh.transaction import WrapTransaction  # noqa: E402

# ---------------------------------------------------------------------------
# A wrap given `--home`
# ---------------------------------------------------------------------------


def _printed_command(out: str, starts: str) -> list[str]:
    """The one command in `out` that starts with `starts`, as an argv after `tegh`."""
    found = re.findall(rf"({re.escape(starts)}[^`\n]*)", out)
    assert len(found) == 1, (starts, found)
    return shlex.split(found[0])[1:]


def _ledger_and_one_dead_server(harness: dict, *, dead: bool) -> None:
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    document["projects"][str(harness["project"])]["mcpServers"]["down"] = {
        "command": sys.executable,
        "args": ["-c", "import sys; sys.exit(3)"] if dead else ["-m", TOY],
    }
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def test_every_command_a_wrap_given_a_home_prints_can_be_run_as_printed(
    harness, capsys, monkeypatch
) -> None:
    """`--home` on the wrap, and no TEGH_HOME: the printed commands must carry it.

    Without it each of them looks in `~/.tegh`, finds no wrap there, and
    refuses; and the wrap they were supposed to lead back to refuses as well,
    because the project is still wrapped. Here the advice for a server that
    could not be reached is followed to the end, and the other printed
    commands are run as they stand.

    The home has a space in its name, so each printed command also has to
    quote it: a shell would otherwise hand `--home` half a path.
    """
    elsewhere = harness["home"].parent / "tegh home"
    monkeypatch.delenv("TEGH_HOME")
    monkeypatch.setenv("HOME", str(harness["home"]))  # so `~/.tegh` is not a real one
    at_home = ["--home", str(elsewhere)]
    assert main(["init", *at_home]) == 0
    _ledger_and_one_dead_server(harness, dead=True)
    capsys.readouterr()

    assert main([*wrap_argv(harness, "--admit-all"), *at_home]) == 0
    out = capsys.readouterr().out

    assert not (harness["home"] / ".tegh").exists()
    advice, closing = out.split("See what the broker records: ")
    advised = _printed_command(advice, "tegh unwrap --project")
    audit = _printed_command(closing, "tegh audit --verify")
    restore = _printed_command(closing, "tegh unwrap --project")
    assert advised == restore and advised[-2:] == at_home
    assert main(audit) == 0
    assert "no wrap backup" not in capsys.readouterr().err

    # A second wrap is refused, and the unwrap that refusal names is the same one.
    assert main([*wrap_argv(harness, "--admit-all"), *at_home]) == 2
    assert _printed_command(capsys.readouterr().err, "tegh unwrap --project") == advised

    # The advice, in its order: unwrap as printed, fix the server, wrap again.
    assert main([*advised, "--yes"]) == 0
    _ledger_and_one_dead_server(harness, dead=False)
    assert main([*wrap_argv(harness, "--admit-all"), *at_home]) == 0
    lock = json.loads(lock_paths(harness["project"])[0].read_text(encoding="utf-8"))
    assert sorted(server["server_id"] for server in lock["servers"]) == ["down", "ledger"]
    assert not (harness["home"] / ".tegh").exists()


def test_a_wrap_given_no_home_prints_commands_without_one(harness, capsys) -> None:
    assert main(["init"]) == 0
    capsys.readouterr()

    assert main(wrap_argv(harness, "--admit-all")) == 0

    out = capsys.readouterr().out
    assert f"Restore with: tegh unwrap --project {harness['project']}\n" in out
    assert "--home" not in out


# ---------------------------------------------------------------------------
# A ceremony that fails for a real reason
# ---------------------------------------------------------------------------


def test_a_ceremony_that_crashes_is_one_line_and_its_traceback_is_shown_apart(
    harness, capsys
) -> None:
    """A store database that cannot be written, which no ceremony expects.

    The base's command dies with a traceback. The line a stopped wrap prints
    carries the error that traceback ends in and says where the rest is; the
    rest is on stdout, indented, with everything else the wrap printed.
    """
    assert main(["init"]) == 0
    assert main(wrap_argv(harness, "--admit-all", "--no-rewrite")) == 0
    database = store_of(harness).db_path
    database.chmod(0o444)
    capsys.readouterr()
    try:
        status = wrap_command(_parse_args(wrap_argv(harness, "--admit-all")), prompt=scripted([]))
    finally:
        database.chmod(0o644)
    said = capsys.readouterr()

    failure = said.err.strip()
    assert status == 1 and failure.startswith("FAILED: "), failure
    assert len(failure.splitlines()) == 1, failure
    assert "Traceback" not in failure and 'File "' not in failure, failure
    assert "readonly database (all it said is printed above)" in failure
    assert "are as they were before this command" in failure
    shown = said.out.split("failed. It said:")[1]
    assert "     Traceback (most recent call last):" in shown
    assert "readonly database" in shown


# ---------------------------------------------------------------------------
# A process the rollback could not stop
# ---------------------------------------------------------------------------


def test_a_process_that_could_not_be_stopped_is_named_in_the_one_line(
    harness, capsys, monkeypatch
) -> None:
    """`test_transaction_signals.py` shows the rollback surviving it; this is the report."""
    assert main(["init"]) == 0
    monkeypatch.setattr(
        WrapTransaction, "stop_children", lambda self: self.not_stopped.extend([4242, 4243])
    )

    def _nobody_answers(_question: str) -> str:
        raise EOFError

    status = wrap_command(_parse_args(wrap_argv(harness)), prompt=_nobody_answers)

    failure = capsys.readouterr().err.strip()
    assert status == 2 and len(failure.splitlines()) == 1, failure
    assert (
        "tegh could not stop every process this wrap started, and these may still "
        "be running: pid 4242, 4243." in failure
    )


# ---------------------------------------------------------------------------
# Ctrl-C outside a wrap's span
# ---------------------------------------------------------------------------

_COMMANDS = {
    "init": (cli, "init_command", ["init"]),
    "wrap": (cli, "wrap_command", ["wrap", "claude"]),
    "gateway": (cli, "gateway_command", ["gateway"]),
    "call": (None, "call_command", ["call", "ledger__get_entry"]),
    "approve": (cli, "approve_command", ["approve", "intent-0"]),
    "audit": (cli, "audit_command", ["audit"]),
    "unwrap": (cli, "unwrap_command", ["unwrap"]),
    "status": (cli, "status_command", ["status"]),
    "posture": (cli, "posture_command", ["posture"]),
    "diff": (cli, "diff_command", ["diff"]),
}


@pytest.mark.parametrize("command", sorted(_COMMANDS))
def test_ctrl_c_in_any_command_is_one_line_and_the_interrupted_status(
    command: str, capsys, monkeypatch
) -> None:
    """Wherever it lands outside a wrap's or an unwrap's own handling.

    Each command is replaced by the KeyboardInterrupt it would raise from
    wherever it happened to be, so this holds `main` to the rule and says
    nothing about any one command.
    """
    module, name, argv = _COMMANDS[command]
    if module is None:
        from tegh import call as module  # noqa: PLC0415 - `main` imports it late too

    def _interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, name, _interrupted)

    status = main(argv)

    said = capsys.readouterr()
    assert status == 130
    assert said.err == f"\nINTERRUPTED: tegh {command} was stopped.\n"
    assert said.out == ""


# ---------------------------------------------------------------------------
# A credential map half-written
# ---------------------------------------------------------------------------


def test_a_ctrl_c_as_the_credential_map_is_renamed_leaves_no_copy_behind(
    harness, monkeypatch
) -> None:
    """The temporary file holds the values, and no rollback knows its name.

    The interrupt is raised from the rename itself, the last step of the
    write: the temporary file must be gone and the map must not exist.
    """
    from pathlib import Path  # noqa: PLC0415

    assert main(["init"]) == 0
    store, project = store_of(harness), harness["project"]

    def _interrupted(self, target):
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", _interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.write_secret_leaf(project, "ledger", {"LEDGER_API_KEY": "not-a-real-credential"})
    monkeypatch.undo()

    assert list(store.secrets_path(project).parent.iterdir()) == []
