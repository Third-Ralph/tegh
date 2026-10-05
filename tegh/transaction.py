"""transaction.py — what a wrap wrote, recorded so a stopped wrap can put it back.

A `tegh wrap` that stops before its commit point leaves every file it wrote as
it was before the command. This module is how. It knows nothing about wraps:
it records files, starts child processes, and undoes both.

## One object, one span, one commit point

A :class:`WrapTransaction` is used as a context manager around everything a
wrap does up to its COMMIT POINT, :meth:`WrapTransaction.commit`. Leaving the
block before that by any road rolls back: a refusal, the end of input, Ctrl-C,
SIGTERM, SIGHUP, an exception nobody expected. No exit has a rollback call of
its own, so none can forget it.

A rollback puts files back and nothing else, so everything a wrap does before
the commit point has to be a file this module recorded, or something that
authorises nothing. Whatever cannot be taken back is done after the commit
point, where nothing is rolled back and the caller reports how far it got.

## What is recorded

Bytes and permission bits, per exact path, with "was not there" as a value. A
file that did not exist is removed again by that path; a directory the wrap
had to create is removed if it is empty again. Nothing is found by pattern.

A file is recorded BEFORE the wrap first writes it. For tegh's own files that
is the start of the wrap, because nothing else writes them. A harness config
is different: the harness keeps writing its own state into it while a wrap
waits at a question, so it is recorded at the last moment, immediately before
the wrap writes it, and a rollback then undoes the wrap's write and nothing
the harness wrote in the minutes before.

## What is put back first

Paths recorded with `restore_first` go back before the rest, and if one of
them cannot be written the rest are LEFT as the wrap wrote them. Those paths
are the harness configs. A wrap moves a credential out of the config and into
tegh's store, and records where it went in the wrap backup; if the config
cannot be restored, taking the store and the backup back as well would leave
the credential nowhere. Left in place, they are exactly what `tegh unwrap`
restores from.

## What goes around the files

The caller gives the rollback steps of its own: `on_rollback` for one that
runs after the children are stopped and BEFORE any file is put back, and
`after_rollback` for one that runs once they are back. A wrap uses the first
to withdraw its pending proposals (the ceremony that does it writes a file of
tegh's on the way, which the rollback then puts back with the rest) and the
second to print its one line. Both run inside the hold below, so a signal that
arrives while the files go back cannot take the report's place. A step that
raises stops neither the files going back nor the steps after it; it is kept
in `steps_failed`.

## Signals

Ctrl-C, SIGTERM and SIGHUP (a closed terminal) stop a wrap the same way:
`stops_like_ctrl_c` makes the last two raise where the first already does.
`signals_held` keeps all three waiting while something that must not be cut in
half runs: a child being started, the rollback and its report, and everything
after the commit point. A signal that cannot be handled at all (SIGKILL, a
power cut) is the caller's to make harmless, by the order it writes in.

What this module guarantees about a child is that it is not still running
when the rollback returns, or that `not_stopped` names it.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence

from tegh.store import TeghStoreError

#: How long a child that was told to stop is given to stop the processes it
#: started itself. The MCP SDK allows a server two seconds before it kills it.
_CHILD_GRACE_SECONDS = 6.0

#: The mode of a file whose recorded mode is unknown. Never widened past this.
_OWNER_ONLY = 0o600


@dataclass(frozen=True)
class _Before:
    """One file as it was: its bytes, or None when there was no file."""

    content: Optional[bytes]
    mode: int
    restore_first: bool


def _read(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _replace(path: Path, content: bytes, mode: int) -> None:
    """Replace `path` atomically, readable no more widely than `mode` at any point.

    The same shape as `interpose._replace_file` and the credential map in
    `store.py`, for the same reason: what goes back can be a credential.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tegh-tmp")
    tmp.unlink(missing_ok=True)  # only a tegh that died mid-write leaves this name
    handle = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(handle, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: The signals that stop a wrap: Ctrl-C, a polite kill, and a closed terminal.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Stopped(KeyboardInterrupt):
    """One of `STOP_SIGNALS` arrived. A KeyboardInterrupt that knows which."""

    def __init__(self, number: int) -> None:
        super().__init__()
        self.number = number


def _handlers(install: Callable[[int], object]) -> dict[int, object]:
    """Replace the handler of each stop signal; return what each one was.

    A signal the process was started ignoring stays ignored (`nohup` does that
    to SIGHUP, a background job to SIGINT): this must not turn a signal the
    process was deaf to into one that stops it.
    """
    previous: dict[int, object] = {}
    for number in STOP_SIGNALS:
        was = signal.getsignal(number)
        handler = install(number)
        if was is signal.SIG_IGN or handler is None:
            continue
        try:
            signal.signal(number, handler)
        except ValueError:
            break  # not the main thread, where no signal is delivered anyway
        previous[number] = was if was is not None else signal.SIG_DFL
    return previous


def _restore(previous: dict[int, object]) -> None:
    for number, was in previous.items():
        signal.signal(number, was)


@contextmanager
def stops_like_ctrl_c() -> Iterator[None]:
    """Make SIGTERM and SIGHUP raise inside the block, as Ctrl-C does.

    Left alone they end the process where it stands, with no rollback and no
    line saying what was left. Raised, they take the road a Ctrl-C takes.
    """

    def _raise(number: int, _frame: object) -> None:
        raise Stopped(number)

    previous = _handlers(lambda number: None if number == signal.SIGINT else _raise)
    try:
        yield
    finally:
        _restore(previous)


#: The signals each hold in force has kept waiting, innermost last.
_holds: list[list[int]] = []


@contextmanager
def signals_held(*, deliver: bool) -> Iterator[list[int]]:
    """Keep a stop signal that arrives inside the block until the block is done.

    Yields the list the signals are kept in, for a caller that wants to look
    between two steps. `deliver` raises the first of them afterwards; the
    rollback does not ask for that, because it is already how the wrap is
    stopping. A hold inside a hold is the outer one: the signal is the outer
    block's to act on, and so is the choice of whether to raise it.
    """
    if _holds:
        yield _holds[-1]
        return
    arrived: list[int] = []
    previous = _handlers(lambda _number: lambda number, _frame: arrived.append(number))
    _holds.append(arrived)
    try:
        yield arrived
    finally:
        _holds.pop()
        _restore(previous)
    if arrived and deliver:
        raise KeyboardInterrupt if arrived[0] == signal.SIGINT else Stopped(arrived[0])


def _process_table() -> dict[int, tuple[int, str]]:
    """`{pid: (parent pid, start time)}` for every process `ps` shows, or `{}`."""
    try:
        listed = subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
            ["ps", "-axo", "pid=,ppid=,lstart="],
            capture_output=True, text=True, check=False, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, tuple[int, str]] = {}
    for line in listed.splitlines():
        pid, _, rest = line.strip().partition(" ")
        parent, _, started = rest.strip().partition(" ")
        if pid.isdigit() and parent.isdigit():
            table[int(pid)] = (int(parent), started.strip())
    return table


def _descendants(root: int) -> list[tuple[int, str]]:
    """Every process started under `root`, each with its start time."""
    table = _process_table()
    found: list[tuple[int, str]] = []
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, started) in table.items():
            if ppid == parent:
                found.append((pid, started))
                frontier.append(pid)
    return found


def _signalled(send: Callable[[], None]) -> bool:
    """Send one signal. False when the process is there and would not take it.

    A process that has already gone is not a failure: gone is what was asked
    for. One that belongs to another user (a server a ceremony started through
    `sudo`) refuses the signal, and that is reported, never raised: the files
    still have to be put back.
    """
    try:
        send()
    except ProcessLookupError:
        pass  # it went between the listing and the signal
    except OSError:
        return False
    return True


def _stop(child: subprocess.Popen) -> list[int]:
    """Stop a child the wrap started, and whatever that child started.

    Returns the pids that could not be signalled and may still be running.

    The child is asked first (SIGINT), because a ceremony that is asked closes
    the MCP server it spawned, and that server is in a session of its own where
    no signal sent to the child reaches it. A child that does not go is killed.
    Then anything it had started that is still running is killed by pid, and
    only if the pid still names the process that was listed: one started at the
    same instant, as `ps` reports it.

    Its output is waited for last and not for long. A server can hold the
    child's pipes open after the child is gone, so the wait that comes before
    the server is dealt with must be able to give up.
    """
    if child.poll() is not None:
        return []
    refused: list[int] = []
    started_by_child = _descendants(child.pid)
    asked = _signalled(lambda: child.send_signal(signal.SIGINT))
    try:
        child.communicate(timeout=_CHILD_GRACE_SECONDS if asked else 0)
    except subprocess.TimeoutExpired:
        if not _signalled(child.kill):
            refused.append(child.pid)
    still = _process_table()
    for pid, started in started_by_child:
        if pid in still and still[pid][1] == started:
            if not _signalled(lambda pid=pid: os.kill(pid, signal.SIGKILL)):
                refused.append(pid)
    try:
        child.communicate(timeout=_CHILD_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass  # something this wrap did not start has the pipes; the child is dead
    return refused


class WrapTransaction:
    """The files one wrap may write and the children it starts. See the module."""

    def __init__(self) -> None:
        self._before: dict[Path, _Before] = {}
        #: Directories that did not exist, to be removed again if they end empty.
        self._absent_dirs: list[Path] = []
        self._children: list[subprocess.Popen] = []
        self._first: list[Callable[[], None]] = []
        self._after: list[Callable[[], None]] = []
        self.committed = False
        #: What ended the block without a commit; None when it simply ended.
        self.stopped_by: Optional[BaseException] = None
        #: Recorded files a rollback could not put back, each with the reason.
        self.not_restored: list[tuple[Path, str]] = []
        #: Recorded files the rollback found changed, as it found them.
        self.found_changed: dict[Path, Optional[bytes]] = {}
        #: Processes the rollback could not signal, which may still be running.
        self.not_stopped: list[int] = []
        #: What stopping a child raised, or a step the caller gave the rollback.
        self.steps_failed: list[BaseException] = []

    # -- recording -----------------------------------------------------------

    def record(self, *paths: Path, restore_first: bool = False) -> None:
        """Remember each file as it is now. The first record of a path stands."""
        for path in paths:
            if path in self._before:
                continue
            try:
                content = _read(path)
                mode = stat.S_IMODE(path.stat().st_mode) if content is not None else _OWNER_ONLY
            except OSError as exc:
                raise TeghStoreError(
                    f"{path} could not be read ({exc.strerror}), so tegh could not "
                    "promise to put it back. Nothing was changed."
                ) from exc
            self._before[path] = _Before(content, mode, restore_first)
            missing = []
            for directory in path.parents:
                if directory.exists():
                    break
                missing.append(directory)
            self._absent_dirs.extend(d for d in missing if d not in self._absent_dirs)

    def before(self, path: Path) -> Optional[bytes]:
        """The recorded bytes of `path`; None when it was not there."""
        return self._before[path].content

    def on_rollback(self, step: Callable[[], None]) -> None:
        """Run `step` if this wrap is rolled back, before any file is put back.

        It may start children of its own with `run`, and whatever it writes
        to a recorded file is put back with the rest. See "What goes around
        the files".
        """
        self._first.append(step)

    def after_rollback(self, step: Callable[[], None]) -> None:
        """Run `step` if this wrap is rolled back, once the files are back."""
        self._after.append(step)

    # -- children ------------------------------------------------------------

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
        """Run a child to its end and return its output, keeping its handle meanwhile.

        In a session of its own, with no terminal on stdin: a Ctrl-C at the
        terminal then reaches the wrap alone, and the child hears about it
        once, from `_stop`. Signalled twice it could abandon the server it
        started in the middle of closing it. And a child started inside a hold
        (by a step of the rollback, or after the commit point) hears nothing at
        all: the hold keeps the signal for the wrap, and the terminal, which
        signals a whole process group, would otherwise deliver it straight to
        the child.

        Nothing of the wrap's own stdin reaches a child either. The answers to
        a wrap's questions can be a pipe, and a child that read from it would
        take them.
        """
        # Inside a hold already in force this one is that hold, and so is the signal.
        with signals_held(deliver=True):
            child = subprocess.Popen(  # noqa: S603 - the caller's fixed argv, no shell
                list(argv), env=dict(env), text=True, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self._children.append(child)
        stdout, stderr = child.communicate()
        self._children.remove(child)
        return subprocess.CompletedProcess(list(argv), child.returncode, stdout, stderr)

    # -- the two ways out ----------------------------------------------------

    def commit(self) -> None:
        """The commit point. From here nothing is put back."""
        self.committed = True

    def __enter__(self) -> "WrapTransaction":
        return self

    def __exit__(self, _exc_type, exc: Optional[BaseException], _traceback) -> bool:
        if self.committed:
            return False
        self.stopped_by = exc
        with signals_held(deliver=False):
            # The files go back whatever the steps before them do. None is
            # trusted not to raise: one signals processes this wrap does not
            # own, and the rest are the caller's code.
            try:
                self._each(self.stop_children, *self._first)
            finally:
                self._put_back()
            self._each(*self._after)
        # Swallowed, for the caller to report in words. Anything that is not an
        # ordinary error or a Ctrl-C (an exit asked for by name) goes on up,
        # after the rollback it has just had.
        return exc is None or isinstance(exc, (Exception, KeyboardInterrupt))

    def _each(self, *steps: Callable[[], None]) -> None:
        for step in steps:
            try:
                step()
            except Exception as failed:  # noqa: BLE001 - reported by the caller
                self.steps_failed.append(failed)

    def stop_children(self) -> None:
        """Stop every child still running; `not_stopped` names what would not."""
        for child in list(self._children):
            self._children.remove(child)
            self.not_stopped.extend(_stop(child))

    def _put_back(self) -> None:
        def restore(path: Path, before: _Before) -> bool:
            try:
                found = _read(path)
                if found == before.content:
                    return True
                self.found_changed[path] = found
                if before.content is None:
                    path.unlink(missing_ok=True)
                else:
                    _replace(path, before.content, before.mode)
                return True
            except OSError as exc:
                self.not_restored.append((path, exc.strerror or type(exc).__name__))
                return False

        first = [restore(p, b) for p, b in self._before.items() if b.restore_first]
        if not all(first):
            return  # see "What is put back first" in the module docstring
        for path, before in self._before.items():
            if not before.restore_first:
                restore(path, before)
        for directory in sorted(self._absent_dirs, key=lambda d: len(d.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass  # not empty, or already gone: it holds something this wrap did not write
