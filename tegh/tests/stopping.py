"""Helpers for the tests that stop a wrap where no rollback follows.

`test_wrap_commit_point.py` stops a wrap after its commit point, and
`test_wrap_killed.py` kills one outright at any point. Both need three things
the other wrap tests do not:

- a wrap in a REAL process that can be held still at a named step, with no
  switch in product code (`run_gated`);
- what a call to each tool gets afterwards, and under which definition it was
  served (`outcomes`);
- the project put back to an earlier state, store database included, so one
  setup serves many stops (`saved` and `put_back`).
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tegh.cli import main
from tegh.lockfile import lock_paths
from tegh.tests.wrapping import (
    REPO,
    V1,
    V2,
    _wrap_process_env,
    description_file,
    processes_naming,
    wrap_argv,
)

ENTRY = '{"entry_id": "L-001"}'
PAGE = '{"limit": 1}'
GET_ENTRY, LIST_ENTRIES = "ledger/get_entry", "ledger/list_entries"
#: Both toy tools corrected to reads, as typed at a terminal.
AS_READS = "e\nread\n\ny\n" * 2

REFUSED = "refused"
HELD = "held for approval"
EXECUTES = "executes"


def executes_under(description: str) -> str:
    return f"{EXECUTES} under {description!r}"


#: `tegh wrap` in a process of its own, with two things added from outside.
#: Every child it starts is written down by pid, so a test that kills the wrap
#: can wait for what the wrap left running. And a ceremony can be held still:
#: for a file named `before-<word>-<n>` or `after-<word>-<n>` in the notes
#: directory, the wrap waits at that side of the n-th ceremony whose argv
#: holds <word>, says so in `at-...` (with the argv), and goes on when the
#: file is taken away.
_GATED_WRAP = """
import json, subprocess, sys, time
from pathlib import Path
from tegh.cli import main
from tegh.transaction import WrapTransaction

notes = Path(sys.argv[1])
start, run, seen = subprocess.Popen, WrapTransaction.run, {}

def _noted(*args, **kwargs):
    child = start(*args, **kwargs)
    with (notes / "children").open("a") as log:
        log.write(f"{child.pid}\\n")
    return child

def _wait_at(name, argv):
    gate = notes / name
    if gate.exists():
        (notes / f"at-{name}").write_text(json.dumps(list(argv)))
        while gate.exists():
            time.sleep(0.01)

def _gated(self, argv, env):
    word = next((w for w in ("admit-propose", "admit-ratify", "seed") if w in argv), None)
    count = seen[word] = seen.get(word, -1) + 1
    if word:
        _wait_at(f"before-{word}-{count}", argv)
    done = run(self, argv, env)
    if word:
        _wait_at(f"after-{word}-{count}", argv)
    return done

subprocess.Popen = _noted
WrapTransaction.run = _gated
raise SystemExit(main(sys.argv[2:]))
"""


@dataclass
class Stopped:
    """A wrap process that has ended, and what it said."""

    returncode: int
    stdout: str
    stderr: str


def _default_signals() -> None:
    # A test run started in the background inherits these ignored, and the
    # wrap would then never see the signal a test sends.
    for number in (signal.SIGINT, signal.SIGHUP):
        signal.signal(number, signal.SIG_DFL)


def _wait_for(condition: Callable[[], bool], what: str, *, within: float = 60.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, f"{what} never happened"
        time.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_gated(
    harness: dict,
    notes: Path,
    *flags: str,
    typed: str,
    gate: Optional[str] = None,
    at_gate: Optional[Callable[[subprocess.Popen, list[str]], None]] = None,
    after: Optional[float] = None,
    send: int = signal.SIGKILL,
) -> Stopped:
    """Run a wrap in its own process and stop it, or let it run to its end.

    With `gate`, the wrap is held at that step and `at_gate(process, argv)` is
    called there (the default sends `send`); the gate is then opened. With
    `after`, `send` goes that many seconds after the start. With neither, the
    wrap just runs. Returns once the wrap AND every child it started are gone,
    because a killed wrap leaves its ceremony running, and the state a test
    must look at is the one that ceremony leaves when it finishes.
    """
    notes.mkdir(exist_ok=True)
    for name in ("children", *(path.name for path in notes.glob("at-*"))):
        (notes / name).unlink(missing_ok=True)
    if gate:
        (notes / gate).write_text("")
    wrap = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _GATED_WRAP, str(notes), *wrap_argv(harness, *flags)],
        env={**_wrap_process_env(harness), "PYTHONUNBUFFERED": "1"}, cwd=REPO, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=_default_signals,
    )
    try:
        wrap.stdin.write(typed)
        wrap.stdin.flush()
        if gate:
            reached = notes / f"at-{gate}"
            _wait_for(
                lambda: reached.exists() or wrap.poll() is not None, f"the wrap reaching {gate}"
            )
            assert reached.exists(), f"the wrap ended before {gate}: {wrap.stderr.read()}"
            _wait_for(lambda: reached.read_text() != "", "the note at the gate")
            argv = json.loads(reached.read_text())
            if at_gate is None:
                wrap.send_signal(send)
            else:
                at_gate(wrap, argv)
            (notes / gate).unlink()
        elif after is not None:
            try:
                wrap.wait(timeout=after)
            except subprocess.TimeoutExpired:
                wrap.send_signal(send)
        out, err = wrap.communicate(timeout=120)
    finally:
        wrap.kill()
        if gate:
            (notes / gate).unlink(missing_ok=True)
    children = notes / "children"
    pids = [int(pid) for pid in children.read_text().split()] if children.exists() else []
    _wait_for(lambda: not any(_alive(pid) for pid in pids), "the wrap's children ending")
    _wait_for(
        lambda: processes_naming(description_file(harness)) == [], "the wrap's server ending"
    )
    return Stopped(wrap.returncode, out, err)


# ---------------------------------------------------------------------------
# What a call gets
# ---------------------------------------------------------------------------


def _call(harness: dict, capsys, tool: str, args: str) -> str:
    capsys.readouterr()
    status = main(["call", tool, "--args", args, "--project", str(harness["project"])])
    said = capsys.readouterr()
    text = said.out + said.err
    if status == 0:
        for description in (V1, V2):
            if f"served under: {description}" in text:
                return executes_under(description)
        return EXECUTES
    return HELD if HELD in text else REFUSED


def outcomes(harness: dict, capsys) -> dict[tuple[str, str], str]:
    """What a call to each tool gets, with the server advertising each description.

    `get_entry` is asked twice, because whether it may run depends on which
    definition the server gives at that moment, and a server can go back to
    one it gave before. The description the server had is put back afterwards.
    """
    described = description_file(harness)
    now = described.read_text(encoding="utf-8")
    found: dict[tuple[str, str], str] = {}
    try:
        for description in (V1, V2):
            described.write_text(description, encoding="utf-8")
            found[GET_ENTRY, description] = _call(harness, capsys, "ledger__get_entry", ENTRY)
        found[LIST_ENTRIES, ""] = _call(harness, capsys, "ledger__list_entries", PAGE)
    finally:
        described.write_text(now, encoding="utf-8")
    return found


def pinned(lock: Optional[bytes]) -> set[str]:
    """Every `get_entry` description the given tegh.lock bytes pin."""
    if lock is None:
        return set()
    return {
        tool["tool_def"]["description"]
        for server in json.loads(lock)["servers"]
        for tool in server["admitted"]
        if tool["tool_def"]["tool_name"] == "get_entry"
    }


def lock_bytes(harness: dict) -> Optional[bytes]:
    path = lock_paths(harness["project"])[0]
    return path.read_bytes() if path.exists() else None


# ---------------------------------------------------------------------------
# One setup, many stops
# ---------------------------------------------------------------------------


def saved(root: Path) -> dict[Path, tuple[bytes, int]]:
    """Every file under `root`, with its permission bits, to be put back later."""
    return {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def put_back(root: Path, files: dict[Path, tuple[bytes, int]]) -> None:
    """Make `root` hold exactly `files` again, tegh's store database included."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and path not in files:
            path.unlink()
    for path, (content, mode) in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
