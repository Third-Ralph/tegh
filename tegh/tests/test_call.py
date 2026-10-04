"""`tegh call`, driven for real: one tool call through a real wrap's gateway.

Nothing here stands in for the gateway or the broker. Every test that expects an
answer runs `tegh init` and `tegh wrap` against the toy ledger server, then runs
`tegh call` through `tegh.cli.main`, which spawns the real gateway, which asks
the real broker, which writes the real tape. The assertions read stdout, the
exit status, and the tape by path.

Two tests look at the SPAWN and not only at its result, through the `spawned`
fixture. That is a recorder around the base's own client, which still starts
the real child; it is how a test can say which argv and which environment the
gateway was given.

Needs the `mcp` extra at runtime: the gateway and the toy server both use the
SDK, in child processes. This module itself imports neither.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the spawned gateway and the toy server use the MCP SDK")

from tegh import call as call_module  # noqa: E402
from tegh.cli import main  # noqa: E402
from tegh.launch import (  # noqa: E402
    HARNESS_SPAWN_ENV_VARS,
    gateway_argv,
    harness_spawn_env,
)
from tegh.store import TeghStore  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    tape_records,
    wrap_admit_all,
    wrap_as_reads,
    written_gateway_entry,
)

#: A tool the wrap admitted, and one the toy server does not even have.
_ADMITTED = "ledger__get_entry"
_NEVER_OFFERED = "ledger__delete_entry"
_ENTRY = '{"entry_id": "L-001"}'


def _call(harness: dict, *argv: str) -> int:
    return main(["call", *argv, "--project", str(harness["project"])])


@pytest.fixture
def wrapped(harness: dict, capsys) -> dict:
    """`harness`, initialized and wrapped with `--admit-all`.

    `--admit-all` takes the proposal, and the toy advertises no annotations, so
    its tools are admitted as irreversible writes and the broker HOLDS every
    call to them. That is the state a held answer comes from.
    """
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    capsys.readouterr()
    return harness


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every gateway `tegh call` starts, recorded as it is started.

    A recorder, not a stand-in: it IS the base's client, it spawns the real
    child, and it changes nothing about the conversation. It only writes down
    the command and the environment on the way past, which are the two things a
    result alone cannot show.
    """
    seen: list[dict] = []
    real = call_module.GatewayClient

    class _Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, command=None, *, env=None, **kwargs) -> None:
            seen.append(
                {
                    "command": list(command) if command else None,
                    "env": dict(env) if env is not None else None,
                }
            )
            super().__init__(command, env=env, **kwargs)

    monkeypatch.setattr(call_module, "GatewayClient", _Recording)
    return seen


# ---------------------------------------------------------------------------
# Exit 1: the broker answered, and the call did not execute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "said", "recorded", "also"),
    [
        pytest.param(
            _ADMITTED,
            ("held for approval", "has NOT executed"),
            {"op": "get_entry", "decision": "require_approval", "outcome": "held"},
            lambda record: bool(record["intentId"]),
            id="held",
        ),
        pytest.param(
            _NEVER_OFFERED,
            ("refused by the broker",),
            {"op": "delete_entry", "decision": "deny", "outcome": "denied"},
            # The reason is the broker's, and it names the coordinate: a deny
            # with an empty reason would be a refusal nobody could act on.
            lambda record: "ledger.delete_entry" in (record["reason"] or ""),
            id="denied",
        ),
    ],
)
def test_a_call_the_broker_stops_exits_1_and_lands_on_the_tape(
    wrapped, capsys, tool, said, recorded, also
) -> None:
    """The reason the command exists: a held call and a DENIED one, in one line each.

    The deny is the case no harness client can produce. The gateway never
    advertised `delete_entry`, so an honest client never asks for it; `tegh
    call` asks anyway, and the refusal is the broker's, recorded like any other
    decision.
    """
    assert _call(wrapped, tool, "--args", _ENTRY) == 1
    out, err = capsys.readouterr()

    for phrase in said:
        assert phrase in out, out
    assert err == "", "a broker answer is not a tegh failure; stderr stays empty"

    matching = [
        record
        for record in tape_records(wrapped)
        if all(record[field] == value for field, value in recorded.items())
    ]
    assert len(matching) == 1, tape_records(wrapped)
    assert also(matching[0]), matching[0]


def test_a_name_that_is_not_a_coordinate_is_answered_by_the_mouth_alone(wrapped, capsys) -> None:
    """Exit 1 with NO tape record: the one answer that is not a broker decision.

    Pinned because the difference matters to a drill. This looks like a deny on
    stdout and in the exit status, and it is not one on the tape, so a drill
    asserting "the broker denied it" has to call a `<server>__<tool>` name.
    """
    assert _call(wrapped, "noseparator") == 1
    assert "is not a brokered tool name" in capsys.readouterr().out
    assert tape_records(wrapped) == []


def test_json_reports_a_held_call_without_deriving_a_verb(wrapped, capsys) -> None:
    """`--json` carries exactly what the wire carried: the text and whether it ran.

    Three keys and no `decision`. The MCP result has no structured verb, so one
    here would have to be parsed out of the broker's prose.
    """
    assert _call(wrapped, _ADMITTED, "--args", _ENTRY, "--json") == 1
    out = capsys.readouterr().out

    assert out.count("\n") == 1, "one JSON object on one line, and nothing else"
    answer = json.loads(out)
    assert set(answer) == {"tool", "executed", "text"}
    assert answer["tool"] == _ADMITTED, "the name as typed, not the broker's dotted form"
    assert answer["executed"] is False
    assert "held for approval" in answer["text"]


# ---------------------------------------------------------------------------
# Exit 0: the call executed
# ---------------------------------------------------------------------------


def test_a_call_to_a_corrected_read_executes_and_exits_0(harness, capsys) -> None:
    """After a human corrects the classification to a read, the same call runs.

    Both output forms against one wrap. The plain form prints the connector's
    result and nothing around it, so it can be piped.
    """
    assert main(["init"]) == 0
    assert wrap_as_reads(harness) == 0
    capsys.readouterr()

    assert _call(harness, _ADMITTED, "--args", _ENTRY) == 0
    plain = capsys.readouterr().out
    assert "opening balance" in plain

    assert _call(harness, _ADMITTED, "--args", _ENTRY, "--json") == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["executed"] is True
    assert answer["text"] == plain.rstrip("\n"), "--json wraps the same text, unchanged"

    executed = [
        record
        for record in tape_records(harness)
        if (record["op"], record["decision"], record["outcome"])
        == ("get_entry", "allow", "executed")
    ]
    assert len(executed) == 2, "each `tegh call` is one brokered call on the tape"


# ---------------------------------------------------------------------------
# Exit 2: tegh could not ask
# ---------------------------------------------------------------------------


def _nothing(harness: dict) -> None:
    """No tegh home at all."""


def _init_only(harness: dict) -> None:
    assert main(["init"]) == 0


def _init_and_wrap(harness: dict) -> None:
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0


@pytest.mark.parametrize(
    ("prepare", "argv", "says"),
    [
        pytest.param(_nothing, [_ADMITTED], "no tegh home", id="no-tegh-home"),
        pytest.param(_init_only, [_ADMITTED], "has not been wrapped", id="not-wrapped"),
        pytest.param(
            _init_and_wrap, [_ADMITTED, "--args", "{entry_id: L-001}"], "not JSON", id="args-not-json"
        ),
        pytest.param(
            _init_and_wrap, [_ADMITTED, "--args", '["L-001"]'], "JSON array", id="args-an-array"
        ),
        pytest.param(
            _init_and_wrap, [_ADMITTED, "--timeout", "0"], "--timeout", id="timeout-not-positive"
        ),
    ],
)
def test_a_refusal_exits_2_and_spawns_nothing(
    harness, capsys, monkeypatch, prepare, argv, says
) -> None:
    """What `tegh call` can refuse by itself, it refuses before starting a child.

    The tripwire replaces the client with something that fails the test if it is
    ever constructed. "Nothing spawned" is then a fact about the run and not a
    reading of the code, and the empty tape says the same thing from the other
    side: the broker was never asked.
    """
    prepare(harness)
    capsys.readouterr()

    def _tripwire(*args, **kwargs):
        raise AssertionError(f"a gateway was spawned for a call tegh should refuse: {args}")

    monkeypatch.setattr(call_module, "GatewayClient", _tripwire)

    assert _call(harness, *argv) == 2
    out, err = capsys.readouterr()
    assert out == "", "stdout is the broker's answer; a refusal has none"
    assert err.startswith("REFUSED: "), err
    assert says in err, err
    assert tape_records(harness) == []


def test_a_gateway_that_will_not_boot_exits_2_and_says_why(wrapped, capsys) -> None:
    """Exit 2 is also for a gateway that started and could not serve.

    The manifest is overwritten after the wrap, so the project still counts as
    wrapped and the real gateway is really spawned, then refuses at boot. The
    client's error carries the child's stderr, and that is what the user needs:
    the cause, in the gateway's own words.
    """
    TeghStore(home=wrapped["tegh_home"]).manifest_path(wrapped["project"]).write_text(
        "this is: [not a manifest\n", encoding="utf-8"
    )

    assert _call(wrapped, _ADMITTED, "--args", _ENTRY) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("REFUSED: the gateway did not answer."), err
    assert "--- gateway stderr ---" in err, err


# ---------------------------------------------------------------------------
# The spawn: the argv a wrap wrote, and the environment a harness gives
# ---------------------------------------------------------------------------


def test_the_spawned_command_is_the_entry_the_wrap_wrote(wrapped, spawned, capsys) -> None:
    """`tegh call` runs the command the harness would run, read back off disk.

    Compared against the rewritten `.claude.json` and not against a second call
    to the helper that built it. If the two ever spell the gateway differently,
    `tegh call` is testing a process the user's harness never starts.
    """
    assert _call(wrapped, _ADMITTED, "--args", _ENTRY) == 1

    entry = written_gateway_entry(wrapped)
    assert [spawn["command"] for spawn in spawned] == [[entry["command"], *entry["args"]]]


def test_the_gateway_does_not_inherit_this_process_environment(
    wrapped, spawned, monkeypatch, tmp_path, capsys
) -> None:
    """The child gets the six variables a harness passes and nothing from here.

    Three variables are poisoned in THIS process. Only one of them carries the
    behavioural half of this test, and it matters which:

    - `BROKER_SQLITE_GRANTS_PATH` names a grants database that does not exist.
      `TeghStore.gateway_env` starts from the gateway's own environment and
      does not set this one, so it reaches the broker if the spawn inherits.
      Checked by mutation: with the spawn inheriting, the answer below becomes
      "refused by the broker: tool not granted to this principal".
    - `TEGH_HOME` names an empty directory and `BROKER_MANIFEST` a missing file.
      Neither changes the answer even when inherited (the command line names
      the home, and `gateway_env` overwrites the manifest), which was also
      checked by mutation. They are the two a reader would reach for first, so
      they stay, and the structural assertions are what cover them.

    The answer has to be the SAME held call the harness's client gets. An
    inheriting spawn is what hid the missing `--home` once: the test passed on
    its own leaked environment and the first real operator's harness failed.
    """
    empty_home = tmp_path / "not-a-tegh-home"
    empty_home.mkdir()
    poison = {
        "TEGH_HOME": str(empty_home),
        "BROKER_MANIFEST": str(tmp_path / "no-such-manifest.yaml"),
        "BROKER_SQLITE_GRANTS_PATH": str(tmp_path / "no-such-grants.db"),
    }
    for name, value in poison.items():
        monkeypatch.setenv(name, value)

    rc = _call(wrapped, _ADMITTED, "--args", _ENTRY, "--home", str(wrapped["tegh_home"]))
    out, err = capsys.readouterr()

    # Behaviour: the poison changed nothing about the answer.
    assert (rc, err) == (1, ""), (rc, out, err)
    assert "held for approval" in out, out

    # Structure: the environment handed to the child, as it was handed over.
    (spawn,) = spawned
    assert spawn["env"] is not None, "env=None means INHERIT to subprocess"
    assert set(spawn["env"]) <= set(HARNESS_SPAWN_ENV_VARS), sorted(spawn["env"])
    assert not set(poison) & set(spawn["env"])


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        pytest.param(
            {"PATH": "/bin", "HOME": "/home/x", "TEGH_HOME": "/elsewhere", "BROKER_STORE": "dynamo"},
            {"PATH": "/bin", "HOME": "/home/x"},
            id="only-the-six-names",
        ),
        pytest.param({"TEGH_HOME": "/elsewhere"}, {}, id="absent-names-are-not-invented"),
        pytest.param(
            {"PATH": "/bin", "SHELL": "() { :; }"}, {"PATH": "/bin"}, id="shell-functions-skipped"
        ),
    ],
)
def test_harness_spawn_env_passes_only_what_a_harness_passes(environ, expected) -> None:
    assert harness_spawn_env(environ) == expected


def test_the_gateway_command_refuses_an_empty_launcher() -> None:
    """An empty launcher would write `gateway` as the executable to run."""
    with pytest.raises(ValueError, match="launcher"):
        gateway_argv(Path("/project"), launcher=[], home=Path("/home/x/.tegh"))
