"""Where a signal lands, and what `transaction.py` does about each place.

`test_wrap_transaction.py` stops a wrap at the points a person can reach. The
cases here are the ones a person reaches only by bad luck, each made to happen
on purpose:

- a second Ctrl-C that arrives while the first is being rolled back;
- SIGTERM and SIGHUP, which stop a wrap the way Ctrl-C does;
- a signal the process was started ignoring, which stays ignored;
- a Ctrl-C that arrives in the instant a child is being started;
- a Ctrl-C from a terminal, which goes to a whole process group;
- a child that would read the answers meant for the wrap;
- a process the rollback is not allowed to signal.

None of them is reached through a switch in product code. The signals are real
and the place they land is arranged from outside: a file the rollback has to
read is made a FIFO, so the rollback waits there until the test lets it go.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tegh import transaction
from tegh.tests.wrapping import REPO, STUBBORN_CHILD, processes_naming
from tegh.transaction import WrapTransaction


@pytest.fixture
def ctrl_c_is_live():
    """SIGINT raises KeyboardInterrupt here, as it does at a terminal.

    A test run started in the background inherits SIGINT ignored, and a test
    that sent itself one would then wait for ever.
    """
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _when(condition, *, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# A second Ctrl-C, during the rollback
# ---------------------------------------------------------------------------


def _reader_waiting_on(fifo: Path) -> int:
    """Block until some process has `fifo` open for reading; return a writer on it.

    A FIFO opened for writing without blocking fails with ENXIO for as long as
    nobody is reading it, which makes "the rollback has reached this file" a
    thing a test can wait for rather than sleep and hope for.
    """
    deadline = time.monotonic() + 60
    while True:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            assert exc.errno == errno.ENXIO, exc
            assert time.monotonic() < deadline, "the rollback never read the file"
            time.sleep(0.02)


def test_a_second_ctrl_c_during_the_rollback_does_not_cut_it_short(harness) -> None:
    """Two real signals, the second landing inside the rollback of the first.

    A wrap waiting at a tool question has written its manifest. The test
    replaces that file with a FIFO, so the rollback, which reads each file
    before it puts it back, stops there with two files already restored and
    the rest still to do. The second SIGINT arrives while it waits. Then the
    FIFO is released, and the rollback has to finish: every file back, one
    line, and the status of an interrupted command.
    """
    pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")
    from tegh.cli import main  # noqa: PLC0415 - after the skip
    from tegh.tests.wrapping import (  # noqa: PLC0415
        LEDGER_FIELD,
        LEDGER_SECRET,
        fingerprint,
        give_ledger,
        interrupt_wrap,
        store_of,
    )

    give_ledger(harness, env={LEDGER_FIELD: LEDGER_SECRET})
    assert main(["init"]) == 0
    root = harness["home"].parent
    before = fingerprint(root)
    manifest = store_of(harness).manifest_path(harness["project"])
    still_running: list[bool] = []

    def _twice(wrap: subprocess.Popen) -> None:
        manifest.unlink()
        os.mkfifo(manifest)
        wrap.send_signal(signal.SIGINT)
        writer = _reader_waiting_on(manifest)
        try:
            wrap.send_signal(signal.SIGINT)
            time.sleep(0.5)  # long enough for the signal to be acted on, if it will be
            still_running.append(wrap.poll() is None)
        finally:
            os.close(writer)

    done = interrupt_wrap(
        harness, typed="\n", signal_it=_twice,
        when=lambda stdout: "[y] admit  [e] edit classification" in stdout,
    )

    assert still_running == [True], "the second signal ended the rollback: " + done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == 130, done.stderr
    assert done.stderr.strip().startswith("INTERRUPTED: tegh wrap was stopped"), done.stderr
    assert len(done.stderr.strip().splitlines()) == 1
    assert fingerprint(root) == before
    assert not manifest.exists()


# ---------------------------------------------------------------------------
# A Ctrl-C in the instant a child is started
# ---------------------------------------------------------------------------


def test_a_ctrl_c_while_a_child_starts_is_kept_until_the_child_can_be_stopped(
    tmp_path, monkeypatch, ctrl_c_is_live
) -> None:
    """The signal lands after the fork and before the handle is kept.

    Acted on there, it would leave a process no rollback knows about. It is
    held until the handle is kept and then raised, so the wrap stops as it was
    asked to AND the child is one the rollback stops.
    """
    recorded = tmp_path / "written-by-the-wrap"
    start = subprocess.Popen
    signalled: list[bool] = []

    def _start_then_signal(*args, **kwargs):
        child = start(*args, **kwargs)
        if not signalled:  # the first child only: the rollback starts `ps`
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)  # the signal is acted on here or it is being held
        return child

    marker = f"started-by-{tmp_path}"
    stay = [sys.executable, "-c", "import time; time.sleep(20)", marker]
    transaction_ = WrapTransaction()
    transaction_.record(recorded)
    try:
        with transaction_:
            recorded.write_text("the wrap", encoding="utf-8")
            monkeypatch.setattr(transaction.subprocess, "Popen", _start_then_signal)
            transaction_.run(stay, {})
            pytest.fail("the signal was dropped: the child ran to its end")
    finally:
        monkeypatch.undo()
        left = processes_naming(marker)
        for line in left:
            os.kill(int(line.split()[0]), signal.SIGKILL)

    assert isinstance(transaction_.stopped_by, KeyboardInterrupt)
    assert left == [], "the child was started and never stopped"
    assert not recorded.exists()


@pytest.mark.parametrize("number", transaction.STOP_SIGNALS, ids=lambda n: signal.Signals(n).name)
@pytest.mark.parametrize("deliver", [True, False])
def test_a_held_signal_is_raised_afterwards_only_where_that_was_asked(
    ctrl_c_is_live, number: int, deliver: bool
) -> None:
    reached_the_end = False
    raised: list[BaseException] = []
    try:
        with transaction.stops_like_ctrl_c():
            handlers = {n: signal.getsignal(n) for n in transaction.STOP_SIGNALS}
            with transaction.signals_held(deliver=deliver) as arrived:
                os.kill(os.getpid(), number)
                time.sleep(0.2)
                reached_the_end = arrived == [number]
            assert {n: signal.getsignal(n) for n in transaction.STOP_SIGNALS} == handlers
    except KeyboardInterrupt as exc:
        raised.append(exc)

    assert reached_the_end, "the signal was acted on inside the block"
    assert bool(raised) == deliver
    if raised and number != signal.SIGINT:
        assert isinstance(raised[0], transaction.Stopped) and raised[0].number == number
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP], ids=lambda n: signal.Signals(n).name)
def test_sigterm_and_sighup_raise_inside_a_wrap_and_roll_it_back(tmp_path, number: int) -> None:
    recorded = tmp_path / "written-by-the-wrap"
    transaction_ = WrapTransaction()
    transaction_.record(recorded)
    with transaction.stops_like_ctrl_c(), transaction_:
        recorded.write_text("the wrap", encoding="utf-8")
        os.kill(os.getpid(), number)
        time.sleep(5)
        pytest.fail("the signal did not stop the wrap")

    assert isinstance(transaction_.stopped_by, transaction.Stopped)
    assert transaction_.stopped_by.number == number
    assert not recorded.exists()
    assert signal.getsignal(number) is signal.SIG_DFL


def test_a_hold_inside_a_hold_is_the_outer_one(ctrl_c_is_live) -> None:
    """A child started after the commit point, or by a step of the rollback.

    `run` holds a signal while it starts a child and raises it afterwards.
    Inside a hold already in force that would end what the outer hold exists
    to protect, so the signal is the outer hold's to keep, and to look at.
    """
    try:
        with transaction.signals_held(deliver=False) as outer:
            with transaction.signals_held(deliver=True) as inner:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.2)
            after_the_inner_block = list(outer)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pytest.fail("the inner hold raised the signal that was the outer hold's to keep")

    assert inner is outer
    assert after_the_inner_block == [signal.SIGINT], "the inner hold raised, or lost it"
    assert outer == [signal.SIGINT, signal.SIGTERM]


def test_a_signal_the_process_was_started_ignoring_stays_ignored() -> None:
    """`nohup tegh wrap` ignores SIGHUP, and a background job ignores SIGINT.

    Neither a hold nor `stops_like_ctrl_c` may turn that into a signal that
    stops the wrap, and both must leave it ignored behind them.
    """
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with transaction.stops_like_ctrl_c():
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            with transaction.signals_held(deliver=True) as arrived:
                assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
                os.kill(os.getpid(), signal.SIGHUP)
                time.sleep(0.2)
            assert arrived == []
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_a_step_after_the_files_runs_inside_the_rollbacks_hold(tmp_path, ctrl_c_is_live) -> None:
    """Where a wrap prints its one line: a signal there must not take its place.

    The step that fails first is there to show that it stops neither the step
    after it nor the report of it.
    """
    recorded = tmp_path / "written-by-the-wrap"
    seen: list[str] = []

    def _fails() -> None:
        raise OSError("this step failed")

    def _reports() -> None:
        seen.append("exists" if recorded.exists() else "put back")
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)
        seen.append("reported")

    transaction_ = WrapTransaction()
    transaction_.record(recorded)
    transaction_.on_rollback(lambda: seen.append("exists" if recorded.exists() else "too late"))
    transaction_.after_rollback(_fails)
    transaction_.after_rollback(_reports)
    try:
        with transaction_:
            recorded.write_text("the wrap", encoding="utf-8")
            raise ValueError("the wrap stops")
    except KeyboardInterrupt:
        pytest.fail("the signal was acted on in place of the report")

    assert seen == ["exists", "put back", "reported"]
    assert [str(failed) for failed in transaction_.steps_failed] == ["this step failed"]


# ---------------------------------------------------------------------------
# What a child is kept apart from
# ---------------------------------------------------------------------------

#: A wrap in miniature: one child run through a transaction, then one answer
#: read from stdin, as a wrap reads the answer to its next question.
_ONE_CHILD_THEN_AN_ANSWER = """
import sys
from tegh.transaction import WrapTransaction
transaction = WrapTransaction()
with transaction:
    print(transaction.run([sys.executable, "-c", *sys.argv[1:]], {}).stdout, end="")
print("the wrap read:", repr(sys.stdin.readline()))
"""


def _miniature_wrap(*child: str, **popen) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _ONE_CHILD_THEN_AN_ANSWER, *child],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)},
        text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **popen,
    )


def test_a_child_cannot_read_the_answers_meant_for_the_wrap() -> None:
    """A wrap's answers can be a pipe, and a child would inherit it.

    The child here reads all of its stdin. It gets nothing, and the answer is
    still there for the wrap's next question.
    """
    wrap = _miniature_wrap("import sys; print('the child read:', repr(sys.stdin.read()))")

    out, err = wrap.communicate("y\n", timeout=60)

    assert wrap.returncode == 0, err
    assert out.splitlines() == ["the child read: ''", "the wrap read: 'y\\n'"]


#: Counts the SIGINTs it hears in a file, and outlives each of them.
_COUNTS_SIGNALS = """
import pathlib, signal, sys, time
heard, up = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def note(*_):
    with heard.open("a") as log:
        log.write("heard\\n")
signal.signal(signal.SIGINT, note)
up.write_text("up")
time.sleep(3)
"""


def test_a_ctrl_c_at_the_terminal_does_not_reach_a_child(tmp_path) -> None:
    """A terminal signals the whole foreground process group, not one process.

    The child is in a session of its own, so the group's signal passes it by
    and it hears exactly one: the rollback's, asking it to stop. That is what
    lets a rollback hold a second Ctrl-C for itself while a child it started
    is in the middle of putting an admission back.
    """
    heard, up = tmp_path / "heard", tmp_path / "up"
    wrap = _miniature_wrap(
        _COUNTS_SIGNALS, str(heard), str(up),
        # Its own group, so that the signal below reaches nothing of pytest's.
        start_new_session=True,
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    try:
        _when(up.exists)
        os.killpg(wrap.pid, signal.SIGINT)
        wrap.communicate(timeout=60)
    finally:
        wrap.kill()

    assert heard.read_text(encoding="utf-8").splitlines() == ["heard"]


# ---------------------------------------------------------------------------
# A process the rollback may not signal
# ---------------------------------------------------------------------------


def test_a_process_that_refuses_the_signal_does_not_stop_the_files_going_back(
    tmp_path, monkeypatch, ctrl_c_is_live
) -> None:
    """Stopping children can fail, and the rollback of files must not care.

    The child ignores SIGINT and has started a process in another session.
    Signalling THAT one is made to fail the way it does for a process another
    user owns. The rollback goes on: the child is killed, the file is put
    back, nothing is raised, and the process that is still running is named.
    """
    monkeypatch.setattr(transaction, "_CHILD_GRACE_SECONDS", 1.0)
    recorded = tmp_path / "written-by-the-wrap"
    recorded.write_text("before", encoding="utf-8")
    marker = f"stubborn-{tmp_path}"
    kill = os.kill
    refused: list[int] = []

    def _not_permitted(pid: int, signal_number: int) -> None:
        if refused and pid == refused[0]:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        kill(pid, signal_number)

    def _interrupt_once_both_run() -> None:
        _when(lambda: len(processes_naming(marker)) == 2)
        ours = {str(child.pid) for child in transaction_._children}
        refused.extend(
            int(line.split()[0]) for line in processes_naming(marker)
            if line.split()[0] not in ours
        )
        kill(os.getpid(), signal.SIGINT)

    transaction_ = WrapTransaction()
    transaction_.record(recorded)
    monkeypatch.setattr(transaction.os, "kill", _not_permitted)
    try:
        with transaction_:
            recorded.write_text("the wrap", encoding="utf-8")
            threading.Thread(target=_interrupt_once_both_run, daemon=True).start()
            transaction_.run([sys.executable, "-c", STUBBORN_CHILD, marker], {})
    finally:
        monkeypatch.undo()
        for pid in refused:
            kill(pid, signal.SIGKILL)

    assert isinstance(transaction_.stopped_by, KeyboardInterrupt)
    assert recorded.read_text(encoding="utf-8") == "before"
    assert len(refused) == 1 and transaction_.not_stopped == refused
    assert transaction_.steps_failed == []
    _when(lambda: processes_naming(marker) == [])


def test_a_ctrl_c_while_an_undo_starts_a_child_does_not_end_the_rollback(
    tmp_path, monkeypatch, ctrl_c_is_live
) -> None:
    """The same instant as above, inside the rollback, where it must do nothing.

    An undo runs a child of its own, and the signal lands as that child is
    started. Outside a rollback it is held and then raised. Inside one, raising
    it would end the undo half-way and take the files' turn with it, so it is
    the rollback's to hold and nobody's to act on.
    """
    recorded = tmp_path / "written-by-the-wrap"
    start = subprocess.Popen
    finished: list[int] = []

    def _start_then_signal(*args, **kwargs):
        child = start(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)
        return child

    def _undo() -> None:
        monkeypatch.setattr(transaction.subprocess, "Popen", _start_then_signal)
        try:
            finished.append(transaction_.run([sys.executable, "-c", "pass"], {}).returncode)
        finally:
            monkeypatch.undo()

    transaction_ = WrapTransaction()
    transaction_.record(recorded)
    with transaction_:
        transaction_.on_rollback(_undo)
        recorded.write_text("the wrap", encoding="utf-8")
        raise ValueError("the wrap stops")

    assert finished == [0], "the undo was cut short"
    assert transaction_.steps_failed == []
    assert not recorded.exists()
