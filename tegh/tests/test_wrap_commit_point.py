"""`tegh wrap` after its commit point: nothing is put back, and every stop is safe.

Before the commit point a wrap writes files and proposals, and a stop puts the
files back (`test_wrap_transaction.py`). After it the wrap issues the grants
and ratifies each proposal, and none of that can be taken back. So a wrap
stopped there is not rolled back. It reports, and the rule is about what it
leaves:

- the project is wrapped;
- each tool is admitted and served, or a call to it does not execute, and the
  line says which, by name;
- nothing is served under a definition the tegh.lock on disk does not pin;
- the commands the line gives are run here exactly as printed, and end in a
  project with every tool admitted.

Every boundary between two ratifications is stopped at both ways: by the
ratification itself failing, and by a signal. The failure is real. The test
rejects the wrap's own proposal with the base's `admit-reject` while the wrap
is held still in front of ratifying it, so the ceremony refuses for the reason
it would refuse a proposal somebody else had rejected. The wrap is a real
process held at a named step from outside (`stopping.run_gated`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass
from typing import Optional

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import cli  # noqa: E402
from tegh.cli import _parse_args, main, unwrap_command, wrap_command  # noqa: E402
from tegh.launch import python_module_argv  # noqa: E402
from tegh.tests.stopping import (  # noqa: E402
    AS_READS,
    ENTRY,
    GET_ENTRY,
    LIST_ENTRIES,
    PAGE,
    REFUSED,
    EXECUTES,
    executes_under,
    lock_bytes,
    outcomes,
    pinned,
    run_gated,
)
from tegh.tests.test_wrap_transaction import _failing, _Scene  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    ADMIT_AS_READ,
    TOY_TOOL_COUNT,
    V1,
    V2,
    scripted,
    serve_versioned,
    store_of,
    tape_records,
)

_TOOLS = [GET_ENTRY, LIST_ENTRIES]
_PAST_THE_POINT = "after the point where a wrap can still be put back"
#: What the line says of the tools it then names. True of a read, which is
#: refused, and of a held write, which is held and refused on approval.
_NOT_SERVED = (
    "NOT admitted and not served, so a call to one does not execute (it is "
    "refused, or held for approval and refused when approved): "
)
_EVERY_TOOL = {
    (GET_ENTRY, V1): executes_under(V1),
    (GET_ENTRY, V2): REFUSED,
    (LIST_ENTRIES, ""): EXECUTES,
}


def _reject_its_proposal(harness: dict):
    """Make the ratification the wrap is about to run fail, for a real reason."""

    def _reject(_wrap: subprocess.Popen, argv: list[str]) -> None:
        coordinate = argv[argv.index("admit-ratify") + 1 :]
        rejected = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*python_module_argv("safe_agents.broker.mcp.commands"), "admit-reject", *coordinate],
            env=store_of(harness).ceremony_env(role="checker", project=harness["project"]),
            capture_output=True, text=True, check=False,
        )
        assert rejected.returncode == 0, rejected.stdout + rejected.stderr

    return _reject


@dataclass(frozen=True)
class _Stop:
    id: str
    #: The step the wrap is held in front of while it is stopped.
    gate: str
    #: A signal to send there; None rejects the proposal about to be ratified.
    send: Optional[int]
    #: How many tools are admitted and served afterwards, in wrap order.
    served: int
    label: str
    status: int
    flags: tuple[str, ...] = ()


#: A signal is held after the commit point and looked at before each step, so
#: one sent in front of a ceremony stops the wrap after that ceremony.
_STOPS = [
    _Stop("the first ratification fails", "before-admit-ratify-0", None, 0, "FAILED", 1),
    _Stop("the second ratification fails", "before-admit-ratify-1", None, 1, "FAILED", 1),
    _Stop("sigterm before the first", "before-seed-0", signal.SIGTERM, 0, "INTERRUPTED", 143),
    _Stop("sighup between the two", "before-admit-ratify-0", signal.SIGHUP, 1, "INTERRUPTED", 129),
    _Stop(
        "ctrl-c between the two, --no-rewrite", "before-admit-ratify-0", signal.SIGINT, 1,
        "INTERRUPTED", 130, ("--no-rewrite",),
    ),
]


def _assert_part_admitted(harness: dict, capsys, said: str, stop: _Stop) -> None:
    project = harness["project"]
    served, waiting = _TOOLS[: stop.served], _TOOLS[stop.served :]
    assert len(said.splitlines()) == 1 and "Traceback" not in said, said
    assert said.startswith(f"{stop.label}: ") and _PAST_THE_POINT in said, said
    assert f"only {len(served)} of 2 tool(s) were admitted" in said
    assert f"Admitted and served: {', '.join(served) or 'none'}." in said
    assert f"{_NOT_SERVED}{', '.join(waiting)}." in said
    # What it proposed and never ratified is withdrawn. The one the test
    # rejected to make a ratification fail is already gone, and cannot be.
    became = (
        "expire within 1 hour(s) (1 could not be withdrawn)"
        if stop.send is None and stop.gate
        else "were withdrawn"
    )
    assert (
        f"The {len(waiting)} admission proposal(s) it had made and not ratified {became}; "
        "a proposal makes no tool callable."
    ) in said

    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    servers = list(document["projects"][str(project)]["mcpServers"])
    interposed = "--no-rewrite" not in stop.flags
    assert servers == (["tegh"] if interposed else ["ledger"])
    assert store_of(harness).backup_path(project).exists() == interposed

    # The lock on disk pins both tools at what was reviewed, and a call
    # reaches exactly the tools the line says are served, at that definition.
    assert pinned(lock_bytes(harness)) == {V1}
    assert outcomes(harness, capsys) == {
        (GET_ENTRY, V1): executes_under(V1) if GET_ENTRY in served else REFUSED,
        (GET_ENTRY, V2): REFUSED,
        (LIST_ENTRIES, ""): EXECUTES if LIST_ENTRIES in served else REFUSED,
    }


def _follow_the_line(harness: dict, capsys, said: str, *, interposed: bool) -> None:
    """Run each command the line prints, as printed, with a person's answers."""
    printed = [shlex.split(command)[1:] for command in re.findall(r"`(tegh [^`]*)`", said)]
    reads = scripted([*ADMIT_AS_READ * TOY_TOOL_COUNT])
    if interposed:
        # "To undo it, run A; to finish, run that and then B."
        unwrap_argv, wrap_again = printed
        assert unwrap_argv[0] == "unwrap" and wrap_again[0] == "wrap", printed
        assert main(wrap_again) == 2, "a wrap alone was enough: the project was not wrapped"
        assert "is already wrapped" in capsys.readouterr().err
        assert unwrap_command(_parse_args(unwrap_argv), prompt=lambda _question: "y") == 0
    else:
        (wrap_again,) = printed
    assert ("--no-rewrite" in wrap_again) == (not interposed), wrap_again
    assert wrap_command(_parse_args(wrap_again), prompt=reads) == 0
    assert "admitted  ledger/list_entries" in capsys.readouterr().out
    assert outcomes(harness, capsys) == _EVERY_TOOL


@pytest.mark.parametrize("stop", _STOPS, ids=lambda stop: stop.id.replace(" ", "-"))
def test_a_wrap_stopped_after_its_commit_point_is_safe_and_can_be_finished(
    harness, capsys, tmp_path_factory, stop: _Stop
) -> None:
    serve_versioned(harness, V1)
    assert main(["init"]) == 0
    capsys.readouterr()

    done = run_gated(
        harness, tmp_path_factory.mktemp("notes"), *stop.flags, typed=AS_READS,
        gate=stop.gate, send=stop.send or 0,
        at_gate=None if stop.send else _reject_its_proposal(harness),
    )

    said = done.stderr.strip()
    assert done.returncode == stop.status, said
    if stop.send is None:
        assert f"the admission ceremony for {_TOOLS[stop.served]} failed: REFUSED: " in said
        assert "'rejected', not pending" in said
    _assert_part_admitted(harness, capsys, said, stop)
    _follow_the_line(harness, capsys, said, interposed="--no-rewrite" not in stop.flags)


def test_a_held_write_that_was_not_admitted_is_held_and_refused_when_approved(
    harness, capfd, tmp_path_factory
) -> None:
    """Both tools confirmed as the server proposes them, as held writes; one admitted.

    The broker holds a write before the connector is asked whether the manifest
    names the tool. So a call to the tool that was not admitted is not refused:
    it is held, word for word as a call to the admitted one is, and the line
    must not say otherwise. Approving the two held calls is where they part.
    The admitted tool executes; the other is refused, and never ran.

    capfd, because `tegh approve` is a process of its own.
    """
    serve_versioned(harness, V1)
    assert main(["init"]) == 0
    project = str(harness["project"])

    done = run_gated(
        harness, tmp_path_factory.mktemp("notes"), "--admit-all", typed="",
        gate="before-admit-ratify-0", send=signal.SIGTERM,
    )

    said = done.stderr.strip()
    assert done.returncode == 128 + signal.SIGTERM, said
    assert f"Admitted and served: {GET_ENTRY}. {_NOT_SERVED}{LIST_ENTRIES}." in said

    for tool, args, served in (("get_entry", ENTRY, True), ("list_entries", PAGE, False)):
        capfd.readouterr()
        assert main(["call", f"ledger__{tool}", "--args", args, "--project", project]) == 1
        answered = capfd.readouterr()
        held = re.search(
            rf"ledger\.{tool} is held for approval \(intent (intent-[0-9a-f]+)\); it has NOT executed",
            answered.out + answered.err,
        )
        assert held, answered.out + answered.err
        released = main(["approve", held.group(1), "--yes", "--project", project])
        assert (released == 0) == served, capfd.readouterr().err
        assert ("RELEASED" in capfd.readouterr().out) == served

    assert [(record["op"], record["outcome"]) for record in tape_records(harness)] == [
        ("get_entry", "held"),
        ("get_entry", "executed"),
        ("list_entries", "held"),
        ("list_entries", "failed"),
    ]


def test_a_signal_after_the_last_ratification_changes_nothing(
    harness, capsys, tmp_path_factory
) -> None:
    """Held in front of the last ratification and signalled: there is nothing left to stop."""
    serve_versioned(harness, V1)
    assert main(["init"]) == 0
    capsys.readouterr()

    done = run_gated(
        harness, tmp_path_factory.mktemp("notes"), typed=AS_READS,
        gate="before-admit-ratify-1", send=signal.SIGTERM,
    )

    assert done.returncode == 0 and done.stderr == "", done.stderr
    assert "INTERPOSED" in done.stdout
    assert outcomes(harness, capsys) == _EVERY_TOOL


def test_grants_that_cannot_be_issued_leave_a_wrapped_project_serving_nothing(
    harness, capsys, monkeypatch
) -> None:
    """The first step after the commit point fails. In process: nothing real fails a seed.

    The grants are not files and nothing takes one back, so they are issued
    after the commit point, and a seed that fails is reported like any other
    stop there: wrapped, no tool admitted, and the way on.
    """
    serve_versioned(harness, V1)
    assert main(["init"]) == 0
    with monkeypatch.context() as patched:
        scene = _Scene(harness, capsys, patched, harness["home"])
        _failing(scene, "seed")
        done = scene.in_process(answers=[*ADMIT_AS_READ * TOY_TOOL_COUNT])

    said = done.stderr.strip()
    assert done.returncode == 1, said
    assert "issuing grants for this project's admitted tools failed: ERROR: made to fail" in said
    assert "admitted  " not in done.stdout
    stop = _Stop("", "", None, 0, "FAILED", 1)
    _assert_part_admitted(harness, capsys, said, stop)
    _follow_the_line(harness, capsys, said, interposed=True)


@pytest.mark.parametrize("stops", ["before the commit point", "after the commit point"])
def test_a_signal_while_a_stopped_wrap_reports_does_not_take_the_reports_place(
    harness, capsys, monkeypatch, stops: str
) -> None:
    """The line is printed inside the hold that the putting back was done in.

    A Ctrl-C lands as the report begins, which is the moment after the last
    file went back. Acted on there, it would leave `main`'s one generic line
    where the line naming what happened should be.
    """
    serve_versioned(harness, V1)
    assert main(["init"]) == 0
    say = cli._say

    def _signalled_first(line: str) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        say(line)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with monkeypatch.context() as patched:
            scene = _Scene(harness, capsys, patched, harness["home"])
            _failing(scene, "admit-propose" if stops.startswith("before") else "admit-ratify")
            patched.setattr(cli, "_say", _signalled_first)
            try:
                done = scene.in_process(answers=[*ADMIT_AS_READ * TOY_TOOL_COUNT])
            except KeyboardInterrupt:
                pytest.fail("the signal was acted on in place of the report")
    finally:
        signal.signal(signal.SIGINT, previous)

    said = done.stderr.strip()
    assert done.returncode == 1 and said.startswith("FAILED: "), said
    assert (_PAST_THE_POINT in said) == stops.startswith("after")
    assert len(said.splitlines()) == 1


def test_the_manifest_is_replaced_whole_or_not_at_all(tmp_path, monkeypatch) -> None:
    """After the commit point the manifest is rewritten once per tool, with no rollback.

    A write cut short in place could leave a shorter manifest that still
    parses. So it is written beside the real one and renamed over it: stopped
    at the rename, the real one is untouched and nothing is left beside it.
    """
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("names: one tool\n", encoding="utf-8")
    manifest.chmod(0o600)

    def _stopped(self, target):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(type(manifest), "replace", _stopped)
        with pytest.raises(KeyboardInterrupt):
            cli._write_manifest(manifest, {"names": "two tools"})
    assert manifest.read_text(encoding="utf-8") == "names: one tool\n"
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.yaml"]

    cli._write_manifest(manifest, {"names": "two tools"})
    assert manifest.read_text(encoding="utf-8") == "names: two tools\n"
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in tmp_path.iterdir()] == ["manifest.yaml"]
