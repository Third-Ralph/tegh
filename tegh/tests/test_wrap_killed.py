"""A wrap killed at any instant never leaves more authority than it found or meant.

SIGKILL and a power cut run no handler, so nothing a wrap does on the way out
can be what makes them safe. The ORDER a wrap writes in has to be: after every
single write, the state on disk and in tegh's store must already be one where
a call executes only what executed before the wrap, or what the finished wrap
would serve, and never under a definition that neither the tegh.lock from
before the wrap nor the one on disk pins.

This file checks that by doing it. A wrap runs as a real process and is
killed, at steps it is held at from outside (`stopping.run_gated`) and at
seeded random moments across its whole run. After each kill, and after the
ceremony the dead wrap left running has finished, every tool is CALLED, with
the server advertising each definition it has ever advertised, and what
executes is compared with what executed before and what the finished wrap
serves. SIGTERM and SIGHUP are sent the same way; a wrap handles those, so it
must also end in one of its two reported states, or be finished.

A kill at a named step is also held to the rule that makes the rest true: the
manifest on disk names exactly the tools this wrap had ratified AND named by
that step. A tool it names any earlier is one the store may still hold an
earlier admission of.

Four starting points:

- a project never wrapped;
- one committed with `--no-rewrite`, whose server then changed a definition;
- one committed with both tools as held writes, whose server then changed a
  definition, re-wrapped with both tools reclassified as reads. This is the
  one where a tool could come loose: the store still holds the admission of
  the earlier definition, and the wrap is about to stop holding the tool.
- the same on two servers that have the same two tools, where the definitions
  that changed are the SECOND tool of the first server and the first tool of
  the second. Killed with only the first tool of the first server ratified,
  the tool after it and the tool of its name on the other server are each
  still admitted at the earlier definition, and neither may be named yet.

A kill is also something to recover from. After each kill at a named step
(and, in the sweep, after each random SIGKILL) the next thing a person would do
is done: `tegh wrap` again, with a `tegh unwrap` first when the wrap refuses
for want of one. It has to end in the finished wrap.

The default run is a small sample: the steps that matter and two random
moments per starting point (none for the last, which is there for its one
step). The full sweep is every step and eighty random
moments per starting point, and takes far longer (about half an hour for
the first three starting points):

    TEGH_KILL_SWEEP=1 python -m pytest tegh/tests/test_wrap_killed.py

`TEGH_KILL_SEED` chooses the random moments. Every failure names the seed.
"""

from __future__ import annotations

import os
import random
import re
import signal
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.tests.stopping import (  # noqa: E402
    AS_READ,
    EXECUTES,
    GET_ENTRY,
    HELD,
    LIST_ENTRIES,
    REFUSED,
    executes_under,
    lock_bytes,
    named,
    outcomes,
    pinned,
    put_back,
    run_gated,
    saved,
)
from tegh.tests.wrapping import (  # noqa: E402
    ADMIT_AS_READ,
    TOY_TOOL_COUNT,
    V1,
    V2,
    description_file,
    fingerprint,
    scripted,
    serve_versioned,
    unwrap_cli,
    wrap_argv,
)

_SEED = int(os.environ.get("TEGH_KILL_SEED", "20261005"))
_SWEEP = os.environ.get("TEGH_KILL_SWEEP") == "1"
_PAST_THE_POINT = "after the point where a wrap can still be put back"
#: The store database and the audit tape only grow; see `test_wrap_transaction.py`.
#: The launch token and the tool-event mouth's address are the GATEWAY's: the
#: calls this test makes to see what is served start one, and it writes them.
#: No wrap writes either, so neither says anything about what a killed wrap left.
_ONLY_GROW = ("store.db", "audit.jsonl", "gateway-token", "event-mouth.addr")
#: A second server with the toy's two tools, under its own name.
MIRROR_GET, MIRROR_LIST = "mirror/get_entry", "mirror/list_entries"


def _every_step(tools: int) -> list[str]:
    """Both sides of every ceremony of a wrap that admits `tools` tools."""
    return [
        f"{side}-{step}"
        for step in (
            *(f"admit-propose-{n}" for n in range(tools)),
            "seed-0",
            *(f"admit-ratify-{n}" for n in range(tools)),
        )
        for side in ("before", "after")
    ]


def _never_wrapped(harness: dict) -> None:
    serve_versioned(harness, V1)
    assert main(["init"]) == 0


def _committed_then_changed(harness: dict) -> None:
    _never_wrapped(harness)
    reads = scripted([*ADMIT_AS_READ * TOY_TOOL_COUNT])
    assert wrap_command(_parse_args(wrap_argv(harness, "--no-rewrite")), prompt=reads) == 0
    description_file(harness).write_text(V2, encoding="utf-8")


def _held_then_changed(harness: dict) -> None:
    """Both tools admitted as the server proposes them: irreversible writes, held."""
    _never_wrapped(harness)
    assert main(wrap_argv(harness, "--admit-all", "--no-rewrite")) == 0
    description_file(harness).write_text(V2, encoding="utf-8")


def _held_on_two_servers_then_changed(harness: dict) -> None:
    """Four tools admitted as held writes; then one definition changes on each server."""
    serve_versioned(harness, V1, tool="list_entries")
    serve_versioned(harness, V1, server="mirror")
    assert main(["init"]) == 0
    assert main(wrap_argv(harness, "--admit-all", "--no-rewrite")) == 0
    for server in ("ledger", "mirror"):
        description_file(harness, server).write_text(V2, encoding="utf-8")


@dataclass(frozen=True)
class _Start:
    id: str
    prepare: Callable[[dict], None]
    #: What a call gets before the wrap, and what the finished wrap serves.
    before: dict[tuple[str, str], str]
    intended: dict[tuple[str, str], str]
    flags: tuple[str, ...] = ()
    #: The steps a default run is killed at; a sweep is killed at every step.
    steps: tuple[str, ...] = ()
    #: How many tools the wrap reviews, proposes and ratifies.
    tools: int = TOY_TOOL_COUNT
    #: Whether a default run also kills it at random moments; a sweep always does.
    at_random: bool = True


_NOTHING = {(GET_ENTRY, V1): REFUSED, (GET_ENTRY, V2): REFUSED, (LIST_ENTRIES, ""): REFUSED}
_STARTS = [
    _Start(
        "first-wrap", _never_wrapped,
        before=_NOTHING,
        intended={**_NOTHING, (GET_ENTRY, V1): executes_under(V1), (LIST_ENTRIES, ""): EXECUTES},
    ),
    _Start(
        "a-definition-changed", _committed_then_changed,
        before={**_NOTHING, (GET_ENTRY, V1): executes_under(V1), (LIST_ENTRIES, ""): EXECUTES},
        intended={**_NOTHING, (GET_ENTRY, V2): executes_under(V2), (LIST_ENTRIES, ""): EXECUTES},
        steps=("before-admit-ratify-1",),
    ),
    _Start(
        "held-then-reclassified", _held_then_changed,
        # Held whatever the server advertises: the broker holds a write before
        # the connector is asked whether the definition is the admitted one.
        before={(GET_ENTRY, V1): HELD, (GET_ENTRY, V2): HELD, (LIST_ENTRIES, ""): HELD},
        intended={**_NOTHING, (GET_ENTRY, V2): executes_under(V2), (LIST_ENTRIES, ""): EXECUTES},
        flags=("--no-rewrite",),
        steps=("before-admit-ratify-0", "before-admit-ratify-1"),
    ),
    _Start(
        "held-on-two-servers-then-reclassified", _held_on_two_servers_then_changed,
        before={
            (GET_ENTRY, ""): HELD, (LIST_ENTRIES, V1): HELD, (LIST_ENTRIES, V2): HELD,
            (MIRROR_GET, V1): HELD, (MIRROR_GET, V2): HELD, (MIRROR_LIST, ""): HELD,
        },
        intended={
            (GET_ENTRY, ""): EXECUTES,
            (LIST_ENTRIES, V1): REFUSED, (LIST_ENTRIES, V2): executes_under(V2),
            (MIRROR_GET, V1): REFUSED, (MIRROR_GET, V2): executes_under(V2),
            (MIRROR_LIST, ""): EXECUTES,
        },
        flags=("--no-rewrite",),
        # One tool ratified and named. The next tool, and the tool of the same
        # name on the other server, are the two a wrap could name too early.
        steps=("before-admit-ratify-1",),
        tools=2 * TOY_TOOL_COUNT,
        # Twice the tools is twice the calls after every kill, and the random
        # moments of a default run are already spent on the starts above.
        at_random=False,
    ),
]


@dataclass(frozen=True)
class _Kill:
    send: int
    gate: Optional[str] = None
    after: Optional[float] = None

    def __str__(self) -> str:
        where = self.gate or f"{self.after:.3f}s in"
        return f"{signal.Signals(self.send).name} {where} (TEGH_KILL_SEED={_SEED})"


def _kills(start: _Start, took: float) -> list[_Kill]:
    """Where this run is killed: named steps first, then seeded random moments."""
    moments = random.Random(f"{_SEED}-{start.id}")
    handled = (signal.SIGTERM, signal.SIGHUP)
    if _SWEEP:
        sends = [signal.SIGKILL] * 40 + [handled[i % 2] for i in range(40)]
        steps = _every_step(start.tools)
    else:
        sends = [signal.SIGKILL, handled[_STARTS.index(start) % 2]] if start.at_random else []
        steps = start.steps
    return [
        *(_Kill(signal.SIGKILL, gate=step) for step in steps),
        *(_Kill(send, after=moments.uniform(0.0, took)) for send in sends),
    ]


def _assert_never_more(
    harness: dict, capsys, start: _Start, kill: _Kill, lock_before: Optional[bytes]
) -> dict[tuple[str, str], str]:
    """After any stop: nothing executes but what did before or what was meant to."""
    found = outcomes(harness, capsys, tuple(start.before))
    for asked, got in found.items():
        assert got in {REFUSED, start.before[asked], start.intended[asked]}, (
            f"after {kill}, a call to {asked[0]} with the server advertising "
            f"{asked[1]!r} got {got!r}; before the wrap it got "
            f"{start.before[asked]!r} and the finished wrap gives {start.intended[asked]!r} "
            f"(the manifest on disk names {sorted(named(harness)) or 'no tool'})"
        )
    for (tool, description), got in found.items():
        if description and got == executes_under(description):
            pins = pinned(lock_before, tool) | pinned(lock_bytes(harness), tool)
            assert description in pins, (
                f"after {kill}, {tool} executes under {description!r}, which "
                "neither the tegh.lock from before the wrap nor the one on disk pins"
            )
    return found


def _assert_named_only_once_ratified(harness: dict, kill: _Kill, order: list[str]) -> None:
    """Killed at a named step: the manifest names the tools ratified by then, and no other.

    `order` is the order the wrap ratifies in. A wrap names a tool as the step
    after its ratification, so held in front of the n-th ratification it has
    named the n before it, and held behind that ratification it has still
    named only those. At every earlier step it has named none.
    """
    _side, _, step = kill.gate.partition("-")
    ceremony, _, count = step.rpartition("-")
    ratified = order[: int(count)] if ceremony == "admit-ratify" else []
    found = named(harness)
    early = sorted(found - set(ratified))
    assert not early, (
        f"after {kill}, the manifest names {early}, which this wrap had not "
        f"ratified; it had ratified and named {ratified or 'no tool'}"
    )
    assert found == set(ratified), f"after {kill}, the manifest names only {sorted(found)}"


def _assert_ended_as_reported(
    harness: dict, start: _Start, kill: _Kill, done, found, files_before: dict[str, str]
) -> None:
    """A signal a wrap handles: rolled back, or stopped past the point, or finished."""
    said = done.stderr.strip()
    if done.returncode == 0:
        assert found == start.intended, f"after {kill}: {found}"
        return
    if done.returncode == -kill.send:
        # Before the wrap's first write or after its last line, where the
        # signal is nobody's to handle and nothing is half-done.
        assert said == "" and found in (start.before, start.intended), f"after {kill}: {said}"
        return
    assert done.returncode == 128 + kill.send, f"after {kill}: {said}"
    assert said.startswith("INTERRUPTED: ") and len(said.splitlines()) == 1, said
    if _PAST_THE_POINT in said:
        return
    assert "Nothing was wrapped" in said, said
    assert found == start.before, f"after {kill}: {found}"
    files = fingerprint(harness["home"].parent)
    changed = sorted(
        name
        for name in files.keys() | files_before.keys()
        if files.get(name) != files_before.get(name) and not name.endswith(_ONLY_GROW)
    )
    assert changed == [], f"after {kill}"


def _assert_a_wrap_then_finishes(harness: dict, capsys, start: _Start, kill: _Kill) -> None:
    """What a person does next: wrap again, unwrapping first if the wrap says to."""

    def _wrap() -> int:
        reads = scripted([*ADMIT_AS_READ * start.tools])
        return wrap_command(_parse_args(wrap_argv(harness, *start.flags)), prompt=reads)

    status = _wrap()
    if status == 2:
        refusal = capsys.readouterr().err
        assert "tegh unwrap --project" in refusal, f"after {kill}: {refusal}"
        assert unwrap_cli(harness, "--yes") == 0, f"after {kill}"
        status = _wrap()
    assert status == 0, f"after {kill}: {capsys.readouterr().err}"
    assert outcomes(harness, capsys, tuple(start.before)) == start.intended, f"after {kill}"


@pytest.mark.parametrize("start", _STARTS, ids=lambda start: start.id)
def test_a_wrap_killed_anywhere_leaves_no_more_than_before_or_than_intended(
    harness, capsys, tmp_path_factory, start: _Start
) -> None:
    root, notes = harness["home"].parent, tmp_path_factory.mktemp("notes")
    asked, typed = tuple(start.before), AS_READ * start.tools
    start.prepare(harness)
    files, files_before = saved(root), fingerprint(root)
    lock_before = lock_bytes(harness)
    assert outcomes(harness, capsys, asked) == start.before

    began = time.monotonic()
    finished = run_gated(harness, notes, *start.flags, typed=typed)
    took = time.monotonic() - began
    assert finished.returncode == 0, finished.stderr
    assert outcomes(harness, capsys, asked) == start.intended
    order = re.findall(r"^  admitted  (\S+)$", finished.stdout, re.MULTILINE)
    assert len(order) == start.tools and named(harness) == set(order), finished.stdout

    for kill in _kills(start, took):
        put_back(root, files)
        done = run_gated(
            harness, notes, *start.flags, typed=typed,
            gate=kill.gate, after=kill.after, send=kill.send,
        )
        found = _assert_never_more(harness, capsys, start, kill, lock_before)
        if kill.gate:
            _assert_named_only_once_ratified(harness, kill, order)
        if kill.send != signal.SIGKILL:
            _assert_ended_as_reported(harness, start, kill, done, found, files_before)
        elif kill.gate or _SWEEP:
            _assert_a_wrap_then_finishes(harness, capsys, start, kill)
