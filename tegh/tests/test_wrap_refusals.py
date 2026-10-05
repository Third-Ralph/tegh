"""`tegh wrap` when it has to stop: what it says, and what it leaves behind.

Three ways a wrap used to go wrong without saying so, each checked against the
bytes on disk and not against what the command printed:

1. **A second wrap of a wrapped project.** The project's only MCP server is by
   then tegh's own gateway, so the wrap reviewed that, was shown no tools, and
   replaced the signed lock with one pinning none. It now refuses before the
   review and names `tegh unwrap`.
2. **Input that ends at a question, or Ctrl-C at one.** A credential is moved
   into tegh's store before the per-tool review, so a wrap whose answers ran
   out died with the value in the harness config AND the store, and with the
   project's manifest emptied for the review. It now says so in words and puts
   the store back. Every one of these cases runs `tegh` as a real subprocess
   with a real empty pipe, null device, closed descriptor or signal; a patched
   `input` would prove the seam and not the command. These are two of the ways
   a wrap stops; `test_wrap_transaction.py` holds every one of them to the
   same rule, file by file.
3. **A harness config tegh cannot read.** Not UTF-8, not readable, or not JSON:
   one refusal naming the file, with or without `--accept-gaps`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import launch, transaction, unwrap  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.harnesses import claude_code  # noqa: E402
from tegh.launch import gateway_argv  # noqa: E402
from tegh.lockfile import lock_paths  # noqa: E402
from tegh.tests.wrapping import LEDGER_FIELD as _FIELD  # noqa: E402
from tegh.tests.wrapping import LEDGER_SECRET as _SECRET  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    ADMIT_AS_READ,
    NOT_UTF8_BYTE,
    TOY,
    TOY_TOOL_COUNT,
    files_holding,
    fingerprint,
    give_ledger,
    interrupt_wrap,
    not_utf8,
    run_wrap,
    scripted,
    store_of,
    truncate,
    unreadable,
    unwrap_cli,
    wrap_admit_all,
    wrap_argv,
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


# ---------------------------------------------------------------------------
# Input that ends at a question
# ---------------------------------------------------------------------------


def _wrap_state(harness: dict) -> dict[str, bytes | None]:
    """What a wrap that stopped at a question must not have changed, as bytes."""
    store, project = store_of(harness), harness["project"]
    lock_path, signature_path = lock_paths(project)
    paths = {
        "harness config": harness["claude_json"],
        "credential map": store.secrets_path(project),
        "manifest": store.manifest_path(project),
        "snapshot": store.snapshot_path(project, "ledger"),
        "tegh.lock": lock_path,
        "tegh.lock.sig": signature_path,
    }
    return {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    }


def _assert_stopped_with_nothing_moved(
    done, harness: dict, before: dict, *, says: str, status: int = 2, also_in: tuple = ()
) -> None:
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == status, done.stderr
    stopped = done.stderr.strip()
    assert stopped.startswith(says), stopped
    assert len(stopped.splitlines()) == 1, stopped
    assert _wrap_state(harness) == before
    assert files_holding(harness["home"].parent, _SECRET) == [harness["claude_json"], *also_in], (
        "the credential is somewhere it was not before the wrap"
    )
    assert "nowhere else" not in stopped
    assert _SECRET not in done.stdout + done.stderr


_ENDED = "REFUSED: input ended before"
_INTERRUPTED = "INTERRUPTED: tegh wrap was stopped before"
#: Said only when the credential map had been changed and was put back.
_MAP_UNDONE = "this wrap had moved into"


@pytest.mark.parametrize(
    ("flags", "stdin", "left_open"),
    [
        pytest.param((), {"stdin": subprocess.DEVNULL}, f"is {_FIELD} a CREDENTIAL?", id="null-stdin"),
        # `--admit-all` leaves the credential question as the only one, and the
        # end of input used to answer it: the value was moved and the config
        # rewritten with nobody having decided anything.
        pytest.param(
            ("--admit-all",), {"stdin": subprocess.DEVNULL}, f"is {_FIELD} a CREDENTIAL?",
            id="null-stdin-admit-all",
        ),
        pytest.param((), {"closed": True}, f"is {_FIELD} a CREDENTIAL?", id="closed-stdin"),
        # The credential is answered (Enter relocates it) and then the answers
        # run out, so the value is already in tegh's store when the wrap stops.
        pytest.param((), {"input": "\n"}, "[y] admit  [e] edit classification", id="ends-at-a-tool"),
        pytest.param((), {"input": "\ne\n"}, "effect [write]", id="ends-inside-an-edit"),
    ],
)
def test_input_that_ends_at_a_question_refuses_and_moves_nothing(
    harness, flags, stdin, left_open
) -> None:
    give_ledger(harness, env={_FIELD: _SECRET})
    assert main(["init"]) == 0
    before = _wrap_state(harness)
    assert {before[name] for name in ("credential map", "manifest", "tegh.lock")} == {None}

    done = run_wrap(harness, *flags, **stdin)

    _assert_stopped_with_nothing_moved(done, harness, before, says=_ENDED)
    assert left_open in done.stderr
    assert (_MAP_UNDONE in done.stderr) == ("input" in stdin)
    if "input" in stdin:
        assert "did not exist before this wrap and was removed again" in done.stderr
        assert not store_of(harness).snapshot_path(harness["project"], "ledger").parent.exists()


def test_a_wrap_that_moved_no_credential_does_not_say_it_undid_one(harness) -> None:
    """The empty credential map a wrap creates for its snapshot is not a moved value.

    Most projects hold no literal credential, so this is the refusal most people
    would read.
    """
    assert main(["init"]) == 0
    before = _wrap_state(harness)
    assert before["credential map"] is None

    done = run_wrap(harness, stdin=subprocess.DEVNULL)

    assert done.returncode == 2, done.stderr
    assert done.stderr.strip().startswith(_ENDED), done.stderr
    assert "[y] admit  [e] edit classification" in done.stderr
    assert _wrap_state(harness) == before
    assert _MAP_UNDONE not in done.stderr


def _a_leaf_of_another_server(wrapped: dict) -> tuple:
    """A leaf this wrap does not own, in a layout tegh would not write."""
    secrets_path = wrapped["store"].secrets_path(wrapped["project"])
    secrets_path.write_bytes(b'{"another-server":"{\\"TOKEN\\": \\"kept\\"}"}\n')
    return ()


def _the_value_already_there(wrapped: dict) -> tuple:
    """This server's leaf, already holding the value, as tegh itself writes it.

    An earlier wrap that stopped after relocating leaves exactly this, with the
    literal still in the harness config. Putting the map back is then putting a
    copy of the credential back, and the refusal must not say otherwise.
    """
    wrapped["store"].write_secret_leaf(wrapped["project"], "ledger", {_FIELD: _SECRET})
    return (wrapped["store"].secrets_path(wrapped["project"]),)


@pytest.mark.parametrize(
    ("leave", "undone"),
    [
        pytest.param(_a_leaf_of_another_server, True, id="another-servers-leaf"),
        pytest.param(_the_value_already_there, False, id="the-value-already-there"),
    ],
)
def test_a_refused_wrap_puts_back_the_store_it_found(wrapped, leave, undone: bool) -> None:
    """A project wrapped before: a signed lock, a manifest and a credential map exist.

    "As it was" is a statement about bytes and not about a parsed map, and
    about every file, including one that already held the credential.
    """
    assert unwrap_cli(wrapped, "--yes") == 0
    also_in = leave(wrapped)
    before = _wrap_state(wrapped)
    assert None not in before.values()

    done = run_wrap(wrapped, input="\n")

    _assert_stopped_with_nothing_moved(done, wrapped, before, says=_ENDED, also_in=also_in)
    assert (_MAP_UNDONE in done.stderr) == undone
    if undone:
        assert "put back, byte for byte" in done.stderr


def _at_the_credential_question(stdout: str) -> bool:
    return f"is {_FIELD} a CREDENTIAL?" in stdout


def _at_a_tool_question(stdout: str) -> bool:
    return "[y] admit  [e] edit classification" in stdout


@pytest.mark.parametrize(
    ("typed", "when", "moved"),
    [
        pytest.param("", _at_the_credential_question, False, id="at-the-credential-question"),
        # Enter relocates the credential, so it is in tegh's store when Ctrl-C lands.
        pytest.param("\n", _at_a_tool_question, True, id="at-a-tool-question"),
    ],
)
def test_ctrl_c_at_a_question_stops_in_words_and_moves_nothing(
    harness, typed: str, when, moved: bool
) -> None:
    give_ledger(harness, env={_FIELD: _SECRET})
    assert main(["init"]) == 0
    before = _wrap_state(harness)

    done = interrupt_wrap(harness, typed=typed, when=when)

    _assert_stopped_with_nothing_moved(
        done, harness, before, says=_INTERRUPTED, status=unwrap.EXIT_INTERRUPTED
    )
    assert (_MAP_UNDONE in done.stderr) == moved


def test_ctrl_c_while_a_server_is_being_asked_for_its_tools_moves_nothing(harness) -> None:
    """Between two questions the wrap is waiting on a server, not on a person.

    The server here never answers, so the wrap is still inside the snapshot,
    with the credential relocated and the manifest written, when the signal
    lands. It reads its stdin, so it ends when the wrap lets go of it.
    """
    give_ledger(
        harness, command=sys.executable, args=["-c", "import sys; sys.stdin.read()"],
        env={_FIELD: _SECRET},
    )
    assert main(["init"]) == 0
    before = _wrap_state(harness)
    manifest = store_of(harness).manifest_path(harness["project"])

    done = interrupt_wrap(harness, typed="\n", when=lambda _stdout: manifest.exists())

    _assert_stopped_with_nothing_moved(
        done, harness, before, says=_INTERRUPTED, status=unwrap.EXIT_INTERRUPTED
    )
    assert _MAP_UNDONE in done.stderr
    assert "REVIEW" not in done.stdout, "the wrap had left the snapshot before the signal"


_ADMITTED = "ledger__get_entry"
_ENTRY = '{"entry_id": "L-001"}'


def _call(harness: dict) -> int:
    return main(["call", _ADMITTED, "--args", _ENTRY, "--project", str(harness["project"])])


def test_a_refused_wrap_leaves_a_working_project_working(harness, capsys) -> None:
    """Admitted with `--no-rewrite`, so a second wrap is not an already-wrapped one.

    The review works from a manifest whose tool namespace is empty. Left behind
    by a wrap that then refused, that manifest is the one the gateway serves
    from, and the call below was answered `no manifest entry`.
    """
    assert main(["init"]) == 0
    wrapped = wrap_command(
        _parse_args(wrap_argv(harness, "--no-rewrite")),
        prompt=scripted(list(ADMIT_AS_READ) * TOY_TOOL_COUNT),
    )
    assert wrapped == 0
    assert _call(harness) == 0
    assert "opening balance" in capsys.readouterr().out
    before = _wrap_state(harness)
    assert before["manifest"] is not None and before["snapshot"] is not None

    done = run_wrap(harness, stdin=subprocess.DEVNULL)

    assert done.returncode == 2 and done.stderr.strip().startswith(_ENDED), done.stderr
    assert "[y] admit" in done.stderr, "the wrap stopped before the review, which proves less"
    assert _wrap_state(harness) == before
    assert _call(harness) == 0, capsys.readouterr().err
    assert "opening balance" in capsys.readouterr().out


def test_a_store_that_cannot_be_put_back_is_said_and_named(harness, capsys, monkeypatch) -> None:
    give_ledger(harness, env={_FIELD: _SECRET})
    assert main(["init"]) == 0
    store = store_of(harness)
    secrets_path = store.secrets_path(harness["project"])
    # A map that was there before, so putting it back is a write and not a removal.
    store.write_secret_leaf(harness["project"], "another-server", {"TOKEN": "kept"})
    replace = transaction._replace

    def _denied(path, content, mode) -> None:
        if path == secrets_path:
            raise PermissionError(13, "Permission denied", str(path))
        replace(path, content, mode)

    def _ends_at_the_first_tool(question: str) -> str:
        if "CREDENTIAL?" in question:
            return ""
        raise EOFError

    monkeypatch.setattr(transaction, "_replace", _denied)
    rc = wrap_command(_parse_args(wrap_argv(harness)), prompt=_ends_at_the_first_tool)
    failure = capsys.readouterr().err.strip()

    assert rc == 1
    assert failure.startswith("FAILED: input ended before"), failure
    assert f"{secrets_path} (Permission denied)" in failure
    assert f"{secrets_path} may still hold a copy" in failure
    assert len(failure.splitlines()) == 1, failure
    assert _SECRET not in failure
    # The files that could be put back were, whatever happened to the first.
    assert not store.manifest_path(harness["project"]).exists()


# ---------------------------------------------------------------------------
# A harness config tegh cannot read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [(), ("--accept-gaps",)], ids=["plain", "accept-gaps"])
@pytest.mark.parametrize(
    ("damage", "says"),
    [
        pytest.param(not_utf8, "is not UTF-8 text", id="not-utf8"),
        pytest.param(unreadable, "Permission denied", id="unreadable"),
        pytest.param(truncate, "is not valid JSON", id="truncated"),
    ],
)
@pytest.mark.parametrize("which", ["claude_json", "project_mcp"])
def test_a_config_tegh_cannot_read_is_a_refusal_naming_the_file(
    harness, capsys, which: str, damage, says: str, flags
) -> None:
    if damage is unreadable and os.geteuid() == 0:
        pytest.skip("root reads through a mode of 000")
    assert main(["init"]) == 0
    # A server at project scope, so there is something to wrap whichever file
    # is damaged, and so the damaged `.mcp.json` is one that exists.
    project_mcp = harness["project"] / ".mcp.json"
    project_mcp.write_text(
        json.dumps(
            {"mcpServers": {"notes": {"command": sys.executable, "args": ["-m", TOY]}}},
            indent=2,
        ),
        encoding="utf-8",
    )
    path = {"claude_json": harness["claude_json"], "project_mcp": project_mcp}[which]
    root = harness["home"].parent
    mode = path.stat().st_mode
    if damage is not unreadable:
        damage(path)
    before = fingerprint(root)
    if damage is unreadable:
        damage(path)
    capsys.readouterr()

    try:
        # A traceback would be an exception here, and fail the test as one.
        rc = main(wrap_argv(harness, "--admit-all", *flags))
    finally:
        path.chmod(mode)
    captured = capsys.readouterr()

    assert rc == 2
    assert fingerprint(root) == before
    refusal = captured.err.strip()
    assert refusal.startswith("REFUSED: "), refusal
    assert "REVIEW" not in captured.out
    assert NOT_UTF8_BYTE not in (captured.out + captured.err).lower(), (
        "a byte of the file was printed"
    )
    if flags:
        # With the gaps waived the wrap reaches the read it cannot do without.
        assert says in refusal
        assert str(path) in refusal, "the refusal does not name the file"
        assert len(refusal.splitlines()) == 1, refusal
    else:
        # Discovery's finding names the file, and the refusal points at it.
        assert str(path) in captured.out
