"""`tegh wrap` before its commit point: it gets there, or every file is put back.

A wrap writes a dozen files across three places (the harness config, the
project, tegh's home) and can stop at any point between the first and the last.
Everything it does before its commit point is a file or a proposal, and the
rule this file holds it to has three parts:

1. **Rolled back.** Every way a wrap can stop short of the commit point is one
   row of `_STOPS`, driven through the real command, and after each one every
   file under the temp root has the bytes and the permission bits it had
   before. Two files may differ, and they are named with the reason in
   `_may_differ`: the store database and the audit tape, which are append-only
   on purpose. Nothing is excluded by directory.
2. **No authority.** The store database is one of those two, so the files
   coming back is not the whole of it. Every stop that comes after the first
   proposal runs on an already admitted project too, once more with the
   server now advertising a definition the lock does not pin, and after each
   one a call gets the answer it got before the wrap and `tegh diff` reports
   what it reported: a proposal makes nothing callable.
3. **Committed.** A wrap that finishes leaves a wrapped project that serves a
   call, including the wrap that follows a rolled-back one, whose proposals
   are still in the database.

Each stop runs on a project never wrapped and on one already admitted with
`--no-rewrite`, where a lock, a manifest, snapshots and a credential map
holding the value all exist beforehand and must come back as they were.

Twelve of the stops are a real `tegh` process with a real pipe or signal. Three
are not, and say so where they are defined: a proposal that fails, and an
error or a Ctrl-C after the config was rewritten. No subprocess reaches those
without a switch in product code, so they call `wrap_command` in this process
with one function replaced.

What a wrap does AFTER its commit point is `test_wrap_commit_point.py`, and
the stops nothing handles are `test_wrap_killed.py`.
"""

from __future__ import annotations

import json
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
import yaml

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import cli, interpose, transaction, unwrap  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.lockfile import lock_paths  # noqa: E402
from tegh.store import project_slug  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    ADMIT_AS_READ,
    LEDGER_FIELD,
    LEDGER_SECRET,
    STUBBORN_CHILD,
    TOY,
    TOY_TOOL_COUNT,
    V1,
    V2,
    as_fingerprinted,
    description_file,
    files_holding,
    fingerprint,
    give_ledger,
    interrupt_wrap,
    processes_naming,
    run_wrap,
    scripted,
    serve_versioned,
    store_of,
    unwrap_cli,
    wrap_argv,
)
from tegh.transaction import WrapTransaction  # noqa: E402

_CREDENTIAL = {LEDGER_FIELD: LEDGER_SECRET}
#: Enter at the credential question (relocate it), then both tools as reads.
_AS_READS = ["", *ADMIT_AS_READ * TOY_TOOL_COUNT]
_ENTRY = '{"entry_id": "L-001"}'
_PAGE = '{"limit": 1}'
#: Said only by a rollback that came after the first proposal.
_PROPOSALS_WITHDRAWN = "had made and not ratified were withdrawn; a proposal makes no tool callable"
_REFUSED_AT_THE_CONNECTOR = "refused at the connector"
_AS_THEY_WERE = "are as they were before this command"


def _may_differ(harness: dict) -> dict[str, str]:
    """The files a rolled-back wrap may leave changed, each with why."""
    slug = project_slug(harness["project"])
    return {
        "tegh/tegh.db": (
            "the store database: the proposals a stopped wrap made and withdrew "
            "stay in it as history, and a rollback does not rewrite it"
        ),
        f"tegh/projects/{slug}/audit.jsonl": (
            "the audit tape, which is a hash chain that only grows"
        ),
    }


def _call(harness: dict, tool: str, args: str) -> int:
    return main(["call", tool, "--args", args, "--project", str(harness["project"])])


# ---------------------------------------------------------------------------
# Where a wrap starts from
# ---------------------------------------------------------------------------


def _never_wrapped(harness: dict) -> None:
    give_ledger(harness, env=_CREDENTIAL)
    assert main(["init"]) == 0


def _admitted_before(harness: dict) -> None:
    """Committed once with `--no-rewrite`, so everything a wrap writes is there.

    The credential map already holds the value, the config still holds it too
    (that is what `--no-rewrite` leaves), and no gateway entry makes the next
    wrap an already-wrapped one.
    """
    _never_wrapped(harness)
    done = wrap_command(
        _parse_args(wrap_argv(harness, "--no-rewrite")), prompt=scripted(list(_AS_READS))
    )
    assert done == 0
    assert lock_paths(harness["project"])[0].exists()


def _admitted_then_changed(harness: dict) -> None:
    """`_admitted_before`, and then the server changes what `get_entry` says.

    The lock pins the first description and the store has admitted it, so a
    call is refused from here on. The next wrap reviews and admits the second.
    """
    serve_versioned(harness, V1)
    _admitted_before(harness)
    description_file(harness).write_text(V2, encoding="utf-8")


def _authority(harness: dict, capsys: pytest.CaptureFixture) -> dict[str, tuple]:
    """What a call to each tool gets, and what `tegh diff` reports."""
    capsys.readouterr()
    found: dict[str, tuple] = {}
    for tool, args in (("ledger__get_entry", _ENTRY), ("ledger__list_entries", _PAGE)):
        status = _call(harness, tool, args)
        said = capsys.readouterr()
        found[tool] = (status, _REFUSED_AT_THE_CONNECTOR in said.out + said.err, V2 in said.out)
    status = main(["diff", "--project", str(harness["project"])])
    found["diff"] = (status, capsys.readouterr().out.strip().splitlines()[-1])
    return found


# ---------------------------------------------------------------------------
# The ways a wrap stops
# ---------------------------------------------------------------------------


@dataclass
class _Scene:
    """One stop: the project, what it looked like, and what the wrap said."""

    harness: dict
    capsys: pytest.CaptureFixture
    monkeypatch: pytest.MonkeyPatch
    #: A directory that is not under the root, for what a case leaves lying.
    outside: Path
    before: dict[str, str] = field(default_factory=dict)
    held_before: list[Path] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return self.harness["home"].parent

    def begin(self) -> None:
        """Called by each stop at the last moment before its wrap starts."""
        self.before = fingerprint(self.root)
        self.held_before = files_holding(self.root, LEDGER_SECRET)
        self.capsys.readouterr()

    def config_becomes(self, raw: bytes) -> None:
        """The case itself rewrote the harness config while the wrap waited."""
        config = self.harness["claude_json"]
        self.before[str(config.relative_to(self.root))] = as_fingerprinted(
            raw, stat.S_IMODE(config.stat().st_mode)
        )
        self.held_before = [path for path in self.held_before if path != config]
        if LEDGER_SECRET.encode() in raw:
            self.held_before = sorted([*self.held_before, config])

    def in_process(self, *flags: str, answers: list[str]) -> subprocess.CompletedProcess:
        """`wrap_command` here, for the stops no subprocess reaches."""
        argv = wrap_argv(self.harness, *flags)
        status = wrap_command(_parse_args(argv), prompt=scripted(list(answers)))
        said = self.capsys.readouterr()
        return subprocess.CompletedProcess(argv, status, said.out, said.err)


def _at_a_tool_question(stdout: str) -> bool:
    return "[y] admit  [e] edit classification" in stdout


def _input_ends_at_the_credential_question(scene: _Scene):
    scene.begin()
    return run_wrap(scene.harness, stdin=subprocess.DEVNULL)


def _input_ends_at_a_tool_question(scene: _Scene):
    scene.begin()
    return run_wrap(scene.harness, input="\n")


def _ctrl_c_at_a_tool_question(scene: _Scene):
    scene.begin()
    return interrupt_wrap(scene.harness, typed="\n", when=_at_a_tool_question)


#: A server that says it is up and then answers nothing, not even the end of
#: its input: the one a wrap has to stop, because nothing else will.
_DEAF_SERVER = (
    "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('up'); time.sleep(120)"
)


def _ctrl_c_while_a_server_is_asked_for_its_tools(scene: _Scene):
    up = scene.outside / "server-is-up"
    give_ledger(
        scene.harness, command=sys.executable, args=["-c", _DEAF_SERVER, str(up)],
        env=_CREDENTIAL,
    )
    scene.begin()
    done = interrupt_wrap(scene.harness, typed="\n", when=lambda _stdout: up.exists())
    assert "REVIEW" not in done.stdout, "the wrap had left the snapshot before the signal"
    return done


def _signal_during_the_proposals(scene: _Scene, number: int):
    scene.begin()
    return interrupt_wrap(
        scene.harness, "--admit-all", typed="\n", unbuffered=True,
        # One proposal made, and the second under way.
        when=lambda stdout: "proposed  ledger/get_entry" in stdout,
        signal_it=lambda wrap: wrap.send_signal(number),
    )


def _ctrl_c_during_the_proposals(scene: _Scene):
    return _signal_during_the_proposals(scene, signal.SIGINT)


def _sigterm_during_the_proposals(scene: _Scene):
    return _signal_during_the_proposals(scene, signal.SIGTERM)


def _sighup_during_the_proposals(scene: _Scene):
    """What closing the terminal sends."""
    return _signal_during_the_proposals(scene, signal.SIGHUP)


def _no_server_answers(scene: _Scene):
    give_ledger(
        scene.harness, command=sys.executable, args=["-c", "import sys; sys.exit(3)"],
        env=_CREDENTIAL,
    )
    scene.begin()
    return run_wrap(scene.harness, input="\n")


def _no_tool_is_admitted(scene: _Scene):
    scene.begin()
    return run_wrap(scene.harness, input="\n" + "n\n" * TOY_TOOL_COUNT)


def _failing(scene: _Scene, word: str) -> None:
    """Make the ceremony whose argv holds `word` exit 1, and run every other one.

    In process: `tegh` starts its ceremonies as `sys.executable -m ...`, so
    there is no executable on PATH to stand in for one, and a switch for tests
    does not belong in product code. Everything after the failed exit status is
    the real command.
    """
    run = WrapTransaction.run

    def _run(self, argv, env):
        if word in argv:
            return subprocess.CompletedProcess(list(argv), 1, "", "ERROR: made to fail")
        return run(self, argv, env)

    scene.monkeypatch.setattr(WrapTransaction, "run", _run)


def _a_proposal_fails(scene: _Scene):
    _failing(scene, "admit-propose")
    scene.begin()
    return scene.in_process("--admit-all", answers=[""])


def _a_value_changes_under_the_review(scene: _Scene):
    """The interposition gate's refusal: what is there is not what was classified."""

    def _change_it() -> str:
        scene.config_becomes(give_ledger(scene.harness, env={LEDGER_FIELD: "another-value"}))
        return "y\n" * TOY_TOOL_COUNT

    scene.begin()
    return interrupt_wrap(
        scene.harness, typed="\n", when=_at_a_tool_question, then=_change_it
    )


def _a_credential_tegh_cannot_relocate(scene: _Scene):
    give_ledger(scene.harness, env=_CREDENTIAL, headers={"Authorization": "not-a-real-header"})
    scene.begin()
    return run_wrap(scene.harness, input="\n\n")


def _a_credential_on_a_shadowed_server(scene: _Scene):
    """Refused inside classification, AFTER the winning server's leaf was written."""
    config = scene.harness["claude_json"]
    document = json.loads(config.read_text(encoding="utf-8"))
    document["mcpServers"] = {
        "ledger": {
            "command": sys.executable, "args": ["-m", TOY],
            "env": {LEDGER_FIELD: "not-a-real-credential-at-user-scope"},
        }
    }
    config.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    scene.begin()
    return run_wrap(scene.harness, input="\n\n")


def _manifest_out_of_order(scene: _Scene, written: str) -> list[str]:
    """What is wrong with the manifest on disk at the moment something else is written.

    Nothing, when it classifies both tools as confirmed and names neither.
    The one a wrap writes for its review declares no classification at all,
    and names no tool either; so it is the classifications that tell the two
    apart, and "names no tool" alone would pass on the wrong one.
    """
    manifest = yaml.safe_load(
        store_of(scene.harness).manifest_path(scene.harness["project"]).read_text(encoding="utf-8")
    )
    wrong = []
    classified = sorted(f"{op['tool']}/{op['op']}" for op in manifest["tool_ops"])
    if classified != ["ledger/get_entry", "ledger/list_entries"]:
        wrong.append(
            f"{written} before the manifest that carries the confirmed "
            f"classifications: the one on disk classifies {classified or 'no tool'}"
        )
    named = sorted(
        f"{server_id}/{tool['tool_name']}"
        for server_id, server in manifest["mcp_servers"].items()
        for tool in server.get("tools", ())
    )
    if named:
        wrong.append(f"{written} beside a manifest that already names {named}")
    return wrong


def _after_the_config_is_rewritten(scene: _Scene, error: BaseException):
    """Stop a wrap with every file written and the commit point not yet reached.

    In process, because nothing but a crash lands between the last write and
    the line after it. It is the one stop with the lock, the backup and the
    rewritten config all on disk, so it is what shows those three put back.
    """
    write, write_lock = interpose.write_interposition, cli.write_lock
    # Collected and asserted once the wrap has ended: an assertion raised
    # inside a wrap is one more error for it to roll back and report.
    out_of_order: list[str] = []

    def _lock_after_the_manifest(*args, **kwargs) -> None:
        # A wrap killed with the lock on disk must already be beside the
        # confirmed manifest, the first of the files it writes.
        out_of_order.extend(_manifest_out_of_order(scene, "the lock was written"))
        write_lock(*args, **kwargs)

    def _write_then_stop(plan) -> None:
        # The order a killed wrap is recovered by: the lock and the backup are
        # on disk before the first byte of the config changes, beside a
        # manifest that names no tool.
        project = scene.harness["project"]
        out_of_order.extend(_manifest_out_of_order(scene, "the config was written"))
        assert lock_paths(project)[0].exists(), "the config was written before the lock"
        assert store_of(scene.harness).backup_path(project).exists(), (
            "the config was written before the backup of what it held"
        )
        assert b'"tegh"' not in scene.harness["claude_json"].read_bytes()
        write(plan)
        assert b'"tegh"' in scene.harness["claude_json"].read_bytes()
        raise error

    scene.monkeypatch.setattr(cli, "write_lock", _lock_after_the_manifest)
    scene.monkeypatch.setattr(interpose, "write_interposition", _write_then_stop)
    scene.begin()
    done = scene.in_process("--admit-all", answers=[""])
    assert not out_of_order, "; ".join(out_of_order)
    return done


def _an_error_after_the_config_is_rewritten(scene: _Scene):
    return _after_the_config_is_rewritten(scene, RuntimeError("the disk went away"))


def _ctrl_c_after_the_config_is_rewritten(scene: _Scene):
    return _after_the_config_is_rewritten(scene, KeyboardInterrupt())


@dataclass(frozen=True)
class _Stop:
    drive: Callable[[_Scene], subprocess.CompletedProcess]
    label: str
    says: str
    status: int
    #: Whether the wrap had made a proposal, and so left one in the database.
    proposing: bool = False


_STOPS = [
    _Stop(_input_ends_at_the_credential_question, "REFUSED", "input ended before", 2),
    _Stop(_input_ends_at_a_tool_question, "REFUSED", "input ended before", 2),
    _Stop(_ctrl_c_at_a_tool_question, "INTERRUPTED", "was stopped before", unwrap.EXIT_INTERRUPTED),
    _Stop(_ctrl_c_while_a_server_is_asked_for_its_tools, "INTERRUPTED", "was stopped before", 130),
    _Stop(_ctrl_c_during_the_proposals, "INTERRUPTED", "was stopped before", 130, True),
    _Stop(_sigterm_during_the_proposals, "INTERRUPTED", "was stopped before", 143, True),
    _Stop(_sighup_during_the_proposals, "INTERRUPTED", "was stopped before", 129, True),
    _Stop(_no_server_answers, "NOT WRAPPED", "no server could be reached", 1),
    _Stop(_no_tool_is_admitted, "NOT WRAPPED", "no tool was admitted", 1),
    _Stop(_a_proposal_fails, "FAILED", "proposal for ledger/get_entry failed: ERROR: made to fail", 1),
    _Stop(_a_value_changes_under_the_review, "REFUSED", "values that CHANGED during this wrap", 2, True),
    _Stop(_a_credential_tegh_cannot_relocate, "REFUSED", "cannot yet relocate", 2),
    _Stop(_a_credential_on_a_shadowed_server, "REFUSED", "user:ledger holds a value", 2),
    _Stop(_an_error_after_the_config_is_rewritten, "FAILED", "(RuntimeError: the disk went away)", 1, True),
    _Stop(_ctrl_c_after_the_config_is_rewritten, "INTERRUPTED", "was stopped before", 130, True),
]


def _stop_id(stop: _Stop) -> str:
    return stop.drive.__name__.strip("_")


@pytest.mark.parametrize(
    ("start", "stop"),
    [
        *(pytest.param(_never_wrapped, stop, id=f"{_stop_id(stop)}-first-wrap") for stop in _STOPS),
        *(
            pytest.param(_admitted_before, stop, id=f"{_stop_id(stop)}-admitted-before")
            for stop in _STOPS
        ),
        *(
            pytest.param(_admitted_then_changed, stop, id=f"{_stop_id(stop)}-admitted-then-changed")
            for stop in _STOPS
            if stop.proposing
        ),
    ],
)
def test_a_wrap_that_stops_puts_every_file_back(
    harness, capsys, monkeypatch, tmp_path_factory, start, stop: _Stop
) -> None:
    start(harness)
    # Asked only where an answer could have changed: a project admitted
    # before, and a stop that came after the first proposal.
    asks = start is not _never_wrapped and stop.proposing
    authority = _authority(harness, capsys) if asks else None
    if start is _admitted_then_changed:
        assert authority["ledger__get_entry"] == (1, True, False), authority
        assert authority["diff"] == (1, "DRIFT=1 WITHDRAWN=0 UNCHANGED=1 UNADMITTED=0")
    scene = _Scene(harness, capsys, monkeypatch, tmp_path_factory.mktemp("outside"))

    done = stop.drive(scene)

    assert scene.before, "the stop never called begin()"
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == stop.status, done.stderr
    said = done.stderr.strip()
    _assert_every_file_is_back(scene)
    if asks:
        assert _authority(harness, capsys) == authority, said
    assert said.startswith(f"{stop.label}: "), said
    assert stop.says in said
    assert len(said.splitlines()) == 1, said
    assert _AS_THEY_WERE in said
    assert "no tool was admitted" in said
    assert (_PROPOSALS_WITHDRAWN in said) == stop.proposing
    assert LEDGER_SECRET not in done.stdout + done.stderr


def _assert_every_file_is_back(scene: _Scene) -> None:
    may_differ = _may_differ(scene.harness)
    after = fingerprint(scene.root)
    changed = sorted(
        name
        for name in after.keys() | scene.before.keys()
        if after.get(name) != scene.before.get(name)
    )
    assert [name for name in changed if name not in may_differ] == []
    assert files_holding(scene.root, LEDGER_SECRET) == scene.held_before, (
        "the credential is somewhere it was not before the wrap"
    )
    assert processes_naming(scene.root, scene.outside) == [], (
        "a process this wrap started is still running"
    )


def test_a_stopped_wrap_does_not_keep_an_answer_it_was_given(
    harness, capsys, monkeypatch, tmp_path_factory
) -> None:
    """The record of config-value decisions goes back to its earlier bytes too.

    A project committed with one value classified as configuration, so the
    record holds a decision. The value then changes, the next wrap asks about
    it again and is told "configuration" again, and input ends at the first
    tool. The new answer was written to the record before the wrap stopped; a
    wrap that leaves nothing leaves the record saying what it said before.
    """
    give_ledger(harness, env={**_CREDENTIAL, "LEDGER_REGION": "eu-west-1"})
    assert main(["init"]) == 0
    committed = wrap_command(
        _parse_args(wrap_argv(harness, "--no-rewrite")),
        prompt=scripted(["", "n", *ADMIT_AS_READ * TOY_TOOL_COUNT]),
    )
    assert committed == 0
    record = store_of(harness).config_decisions_path(harness["project"])
    decided = record.read_bytes()
    assert b"LEDGER_REGION" in decided
    give_ledger(harness, env={**_CREDENTIAL, "LEDGER_REGION": "us-east-2"})
    scene = _Scene(harness, capsys, monkeypatch, tmp_path_factory.mktemp("outside"))
    scene.begin()

    done = run_wrap(harness, input="\nn\n")

    assert done.returncode == 2 and done.stderr.strip().startswith("REFUSED: input ended"), done.stderr
    assert "-> configuration  local:ledger.env.LEDGER_REGION" in done.stdout
    assert record.read_bytes() == decided
    _assert_every_file_is_back(scene)


def test_a_child_that_will_not_stop_is_killed_with_what_it_started(tmp_path, monkeypatch) -> None:
    """The backstop under the stop above, where the ceremony closed its own server.

    A ceremony that is wedged cannot be asked to do that. It is killed, and the
    server it started, which no signal to the ceremony reaches, is killed by
    its own pid.
    """
    monkeypatch.setattr(transaction, "_CHILD_GRACE_SECONDS", 1.0)
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", STUBBORN_CHILD, str(tmp_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        assert child.stdout.readline().strip() == "started"
        assert len(processes_naming(tmp_path)) == 2

        transaction._stop(child)

        assert child.poll() is not None
        assert processes_naming(tmp_path) == []
    finally:
        child.kill()


# ---------------------------------------------------------------------------
# A rollback tegh cannot finish
# ---------------------------------------------------------------------------


def test_a_config_that_cannot_be_put_back_keeps_what_restores_it(
    harness, capsys, monkeypatch
) -> None:
    """The one rollback that stops half-way on purpose.

    The config was rewritten, so the credential is out of it and in tegh's
    store, and the backup says where it went. If the config will not go back,
    undoing the store and the backup as well would leave the credential in no
    file at all. They stay, and `tegh unwrap` finishes the job from them.
    """
    _never_wrapped(harness)
    config, project = harness["claude_json"], harness["project"]
    original = config.read_bytes()
    store = store_of(harness)
    replace = transaction._replace

    def _denied(path, content, mode) -> None:
        if path == config:
            raise PermissionError(13, "Permission denied", str(path))
        replace(path, content, mode)

    with monkeypatch.context() as patched:
        scene = _Scene(harness, capsys, patched, Path("unused"))
        patched.setattr(transaction, "_replace", _denied)
        done = _an_error_after_the_config_is_rewritten(scene)

    failure = done.stderr.strip()
    assert done.returncode == 1
    assert failure.startswith("FAILED: tegh wrap stopped on an error"), failure
    assert f"{config} (Permission denied)" in failure
    assert f"`tegh unwrap --project {project} --home {harness['tegh_home']}`" in failure
    assert len(failure.splitlines()) == 1, failure
    assert _AS_THEY_WERE not in failure
    assert store.backup_path(project).exists()
    assert files_holding(scene.root, LEDGER_SECRET) == [store.secrets_path(project)]

    assert unwrap_cli(harness, "--yes") == 0
    assert config.read_bytes() == original
    assert files_holding(scene.root, LEDGER_SECRET) == [config]


# ---------------------------------------------------------------------------
# Committed
# ---------------------------------------------------------------------------


def test_a_wrap_that_finishes_is_wrapped(harness, capsys) -> None:
    _never_wrapped(harness)
    project = harness["project"]

    assert wrap_command(_parse_args(wrap_argv(harness)), prompt=scripted(list(_AS_READS))) == 0
    said = capsys.readouterr()

    assert said.err == ""
    assert "INTERPOSED" in said.out
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    assert list(document["projects"][str(project)]["mcpServers"]) == ["tegh"]
    store = store_of(harness)
    assert files_holding(harness["home"].parent, LEDGER_SECRET) == [store.secrets_path(project)]
    assert store.backup_path(project).exists()
    assert lock_paths(project)[0].read_bytes().count(b'"tool_name"') == TOY_TOOL_COUNT
    assert _call(harness, "ledger__get_entry", _ENTRY) == 0
    assert "opening balance" in capsys.readouterr().out


def test_no_rewrite_commits_and_says_where_the_credential_now_is(harness, capsys) -> None:
    """`--no-rewrite` is a commit of everything but the config, by choice.

    The credential is therefore in the config, where it was, and in tegh's
    store, where the gateway reads it. That is two copies, and the wrap says so
    in as many words.
    """
    _never_wrapped(harness)
    project, config = harness["project"], harness["claude_json"]
    untouched = config.read_bytes()

    done = wrap_command(
        _parse_args(wrap_argv(harness, "--no-rewrite")), prompt=scripted(list(_AS_READS))
    )
    said = capsys.readouterr()

    assert done == 0 and said.err == ""
    assert config.read_bytes() == untouched
    store = store_of(harness)
    assert not store.backup_path(project).exists()
    assert files_holding(harness["home"].parent, LEDGER_SECRET) == [
        config, store.secrets_path(project)
    ]
    assert "1 credential value(s) are now in TWO places" in said.out
    assert str(store.secrets_path(project)) in said.out
    assert "STAY in your harness config as well" in said.out
    assert "removed from your harness config" not in said.out
    assert _call(harness, "ledger__get_entry", _ENTRY) == 0
    assert "opening balance" in capsys.readouterr().out


def test_what_a_rolled_back_wrap_proposed_cannot_be_called(harness, capsys, monkeypatch) -> None:
    """The proposals a rollback leaves in the database authorise nothing.

    A project committed with ONE of the toy's two tools. A second wrap reviews
    both, proposes both, writes every file, and is rolled back. A call to the
    second tool is still refused and the tool committed earlier still answers.
    A wrap that then finishes makes the second tool callable.
    """
    _never_wrapped(harness)
    get_entry_only = ["", *ADMIT_AS_READ, "n"]
    committed = wrap_command(
        _parse_args(wrap_argv(harness, "--no-rewrite")), prompt=scripted(get_entry_only)
    )
    assert committed == 0
    assert _call(harness, "ledger__get_entry", _ENTRY) == 0
    assert _call(harness, "ledger__list_entries", _PAGE) == 1
    assert "no manifest entry" in capsys.readouterr().out

    with monkeypatch.context() as patched:
        stopped = _an_error_after_the_config_is_rewritten(
            _Scene(harness, capsys, patched, Path("unused"))
        )
    assert stopped.returncode == 1 and _PROPOSALS_WITHDRAWN in stopped.stderr, stopped.stderr
    assert "proposed  ledger/list_entries" in stopped.stdout
    assert "admitted  " not in stopped.stdout

    assert _call(harness, "ledger__list_entries", _PAGE) == 1
    assert "no manifest entry" in capsys.readouterr().out
    assert _call(harness, "ledger__get_entry", _ENTRY) == 0
    assert "opening balance" in capsys.readouterr().out

    assert wrap_command(_parse_args(wrap_argv(harness)), prompt=scripted(list(_AS_READS))) == 0
    assert _call(harness, "ledger__list_entries", _PAGE) == 0
    assert "L-001" in capsys.readouterr().out


def test_a_proposal_that_could_not_be_withdrawn_is_said_and_stops_no_later_wrap(
    harness, capsys, monkeypatch
) -> None:
    """A stale pending proposal beside a new one for the same tool changes nothing.

    The rollback's `admit-reject` is made to fail, so both proposals stay
    pending in the database, and the line says they expire. The next wrap
    proposes again and ratifies its OWN proposals, by id.
    """
    _never_wrapped(harness)
    with monkeypatch.context() as patched:
        scene = _Scene(harness, capsys, patched, Path("unused"))
        _failing(scene, "admit-reject")
        stopped = _an_error_after_the_config_is_rewritten(scene)
    assert stopped.returncode == 1, stopped.stderr
    assert (
        "The 2 admission proposal(s) it had made and not ratified expire within 1 "
        "hour(s) (2 could not be withdrawn); a proposal makes no tool callable."
    ) in stopped.stderr
    _assert_every_file_is_back(scene)

    # Not wrapped: there is no manifest for a gateway to start from.
    assert _call(harness, "ledger__get_entry", _ENTRY) == 2
    assert "has not been wrapped" in capsys.readouterr().err

    assert wrap_command(_parse_args(wrap_argv(harness)), prompt=scripted(list(_AS_READS))) == 0
    assert _call(harness, "ledger__get_entry", _ENTRY) == 0
    assert "opening balance" in capsys.readouterr().out
    assert _call(harness, "ledger__list_entries", _PAGE) == 0


# ---------------------------------------------------------------------------
# An earlier wrap's backup is never written over
# ---------------------------------------------------------------------------


def _block(wrapped: dict) -> tuple[dict, dict]:
    document = json.loads(wrapped["claude_json"].read_text(encoding="utf-8"))
    return document, document["projects"][str(wrapped["project"])]["mcpServers"]


def _write(wrapped: dict, document: dict) -> None:
    wrapped["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _gateway_entry_removed(wrapped: dict) -> list[str]:
    document, block = _block(wrapped)
    del block["tegh"]
    _write(wrapped, document)
    return ["ledger"]


def _another_server_in_its_place(wrapped: dict) -> list[str]:
    document, block = _block(wrapped)
    del block["tegh"]
    block["notes"] = {"command": sys.executable, "args": ["-m", TOY]}
    _write(wrapped, document)
    return ["ledger", "notes"]


def _servers_put_back_by_hand(wrapped: dict) -> list[str]:
    """Also what a wrap killed between its backup and its config write leaves."""
    wrapped["claude_json"].write_bytes(wrapped["before"])
    return ["ledger"]


@pytest.mark.parametrize(
    "disturb",
    [_gateway_entry_removed, _another_server_in_its_place, _servers_put_back_by_hand],
)
def test_a_wrap_never_replaces_an_earlier_wraps_backup(wrapped, capsys, disturb) -> None:
    """A backup with no gateway entry beside it: refused, and `tegh unwrap` named.

    The backup is the only record of the servers the earlier wrap displaced
    and of which stored credential is whose. A wrap that went ahead would
    replace it with one that has never heard of them. The unwrap it names is
    then followed, to the end: the servers and the credential come back, and a
    wrap runs.
    """
    project, root = wrapped["project"], wrapped["root"]
    servers = disturb(wrapped)
    before = fingerprint(root)
    capsys.readouterr()

    refused = main(wrap_argv(wrapped, "--admit-all"))
    refusal = capsys.readouterr().err.strip()

    assert refused == 2
    assert fingerprint(root) == before
    assert refusal.startswith(f"REFUSED: an earlier wrap of {project} left a backup"), refusal
    assert f"`tegh unwrap --project {project} --home {wrapped['tegh_home']}`" in refusal
    assert len(refusal.splitlines()) == 1, refusal

    assert unwrap_cli(wrapped, "--yes") == 0
    _, block = _block(wrapped)
    assert sorted(block) == servers
    assert block["ledger"]["env"] == _CREDENTIAL
    assert files_holding(root, LEDGER_SECRET) == [wrapped["claude_json"]]
    assert not wrapped["store"].backup_path(project).exists()

    assert wrap_command(_parse_args(wrap_argv(wrapped, "--admit-all")), prompt=scripted([""])) == 0


# ---------------------------------------------------------------------------
# A server that could not be reached, beside one that could
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "unwrap_first"),
    [
        pytest.param((), True, id="interposed"),
        pytest.param(("--no-rewrite",), False, id="no-rewrite"),
    ],
)
def test_the_advice_for_an_unreached_server_can_be_followed(
    harness, capsys, flags, unwrap_first: bool
) -> None:
    """The wrap commits for the server it reached, so "run it again" is not enough.

    Interposed, the project is wrapped and a second wrap is refused, so the
    advice has to go through an unwrap. Under `--no-rewrite` it is not, and
    wrapping again is right. Either way the advice is followed here, word for
    word, and ends in a wrap that pins both servers.
    """
    assert main(["init"]) == 0
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    block["down"] = {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}
    _write(harness, document)
    capsys.readouterr()

    assert main(wrap_argv(harness, "--admit-all", *flags)) == 0
    out = capsys.readouterr().out

    assert "!! down: could not be reached" in out
    assert "not wrapped, because it could not be reached: down" in out
    assert "re-run `tegh wrap`" not in out
    names_unwrap = f"run `tegh unwrap --project {harness['project']}`" in out
    assert names_unwrap == unwrap_first

    if unwrap_first:
        assert main(wrap_argv(harness, "--admit-all")) == 2, "wrapping again was enough"
        assert unwrap_cli(harness, "--yes") == 0
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    block = document["projects"][str(harness["project"])]["mcpServers"]
    block["down"] = {"command": sys.executable, "args": ["-m", TOY]}
    _write(harness, document)
    assert main(wrap_argv(harness, "--admit-all", *flags)) == 0
    lock = json.loads(lock_paths(harness["project"])[0].read_text(encoding="utf-8"))
    assert sorted(server["server_id"] for server in lock["servers"]) == ["down", "ledger"]
    assert all(len(server["admitted"]) == TOY_TOOL_COUNT for server in lock["servers"])
