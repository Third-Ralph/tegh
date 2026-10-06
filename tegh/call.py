"""`tegh call`: drive ONE `tools/call` through a wrapped project's gateway.

    tegh call memory__delete_entities --args '{"entityNames": ["x"]}'

It exists so a deny can be SHOWN. Every client the wrapped harness runs is
offered only the admitted tools, so an honest agent never produces one: it asks
for a tool it was never offered, is told no such tool exists, and stops. A deny
needs a client that calls a coordinate it was never offered, which is the
position of a compromised or injected agent guessing at a name. Until this
existed, reproducing that meant hand-writing an MCP client.

**It asks as the wrapped principal, through the same door.** The command it
spawns is the one `tegh wrap` wrote into the harness config (`launch.gateway_argv`
builds both), given the environment a harness gives a spawned stdio child
(`launch.harness_spawn_env`) and never this process's own. The answer therefore
comes from the broker's full per-call path, lands on the project's audit tape,
and is the answer the harness's client would have got. `tegh call` holds a
client and can do nothing with it except ask: it cannot approve, cannot widen a
grant, and cannot reach a connector the broker did not reach for it.

**It reports the broker's words and derives nothing from them.** The MCP wire
carries text blocks and `isError`, and no structured decision verb. So the text
is printed verbatim, and `executed` is `not isError` and nothing finer. Parsing
the prose for "held" or "refused" would be tegh inventing a decision the broker
did not put on the wire, and it would break the first time the base reworded a
message. The verb, the reason and the intent id are on the tape: `tegh audit`.

## Exit status

    0  the call executed
    1  the broker answered and the call did NOT execute: held for approval,
       denied, or the connector failed
    2  tegh could not ask: no tegh home, the project is not wrapped, `--args` is
       not a JSON object, the gateway did not start or did not answer as an MCP
       server, or it did not reply within `--timeout`

1 and 2 are kept apart because they mean opposite things to a script. 1 is the
control working, and a drill asserts it. 2 is no evidence about the control at
all.

One answer under 1 does not come from the broker's decision path. A name with
no `<server>__<tool>` shape is not a coordinate, so the gateway's mouth refuses
it by itself ("is not a brokered tool name") and nothing is written to the tape.
It is still an MCP answer with `isError` set, so it is still 1; a drill that
wants a DENY on the tape has to name a server.

## The import boundary

This is the one tegh module that imports from `safe_agents.broker.api`, and it
takes three names: the gateway client, its error, and `result_text`. They are
the base's own stdio MCP client, published for exactly this use. tegh still
never imports `build_runtime` or anything else that decides, and
`tests/test_lock.py::TestImportBoundary` permits these three names and no other
part of that module. `cli.py` imports this module only when `call` is the
command being run, so `tegh gateway` stays the thin launcher it claims to be and
does not load the broker runtime into a process that is about to exec it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from safe_agents.broker.api import GatewayClient, GatewayClientError, result_text
from tegh.launch import gateway_argv, harness_spawn_env, tegh_launcher
from tegh.store import TeghStore, tegh_home

EXIT_EXECUTED = 0
EXIT_NOT_EXECUTED = 1
EXIT_COULD_NOT_ASK = 2

#: What this client calls itself in the MCP handshake, so a gateway log line can
#: tell a `tegh call` from the harness's own client.
CLIENT_NAME = "tegh-call"


#: What a non-object `--args` is called in the refusal, in JSON's own vocabulary
#: and not Python's ("array", never "list").
_JSON_TYPE_NAMES: dict[type, str] = {
    list: "array",
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    type(None): "null",
}


class _CannotAsk(Exception):
    """A refusal raised before anything is spawned. The message is user-facing."""


def _parse_arguments(raw: str) -> dict[str, Any]:
    """`--args` as the JSON object MCP's `tools/call` takes.

    An object and nothing else: `arguments` on the wire is a map of parameter
    name to value, so an array or a bare string has no meaning there. Refusing
    it here names the mistake; passing it along would get back whatever the
    gateway makes of a malformed frame, which reads as a broker decision and is
    not one.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _CannotAsk(
            f"--args is not JSON ({exc.msg} at character {exc.pos}). Pass one JSON "
            """object, quoted for your shell: --args '{"name": "value"}'."""
        ) from None
    if not isinstance(parsed, dict):
        raise _CannotAsk(
            f"--args must be a JSON object mapping argument names to values; got a "
            f"JSON {_JSON_TYPE_NAMES.get(type(parsed), type(parsed).__name__)}."
        )
    return parsed


def _wrapped_store(args: argparse.Namespace) -> tuple[Path, TeghStore]:
    """Resolve the project and its store exactly as `gateway` does, or refuse.

    The same checks `gateway_command` makes, made here FIRST. The spawned
    gateway would refuse on each by itself, but its refusal would arrive as a
    child that exited during the handshake, wrapped in a client error about a
    missing reply. Saying it here says it plainly and spawns nothing. The last
    is the project wrapped under an earlier store layout, which
    `TeghStore.gateway_env` refuses for the gateway.
    """
    project = Path(args.project).expanduser().resolve()
    store = TeghStore(home=Path(args.home).expanduser() if args.home else tegh_home())
    if not store.is_provisioned:
        raise _CannotAsk(f"no tegh home at {store.home}. Run `tegh init`.")
    manifest = store.manifest_path(project)
    if not manifest.exists():
        raise _CannotAsk(
            f"{project} has not been wrapped, so there is no gateway to call "
            f"(no manifest at {manifest}). Run `tegh wrap claude --project <path>` first."
        )
    earlier_layout = store.layout_refusal(project)
    if earlier_layout is not None:
        raise _CannotAsk(earlier_layout)
    return project, store


def call_command(args: argparse.Namespace) -> int:
    """Ask the broker for one tool call and print what it answered.

    Everything that can be refused without a child process is refused first:
    the store, the wrap, `--args`, `--timeout`. Only then is the gateway
    spawned, with the argv a wrap writes and the environment a harness gives.

    Returns `EXIT_EXECUTED`, `EXIT_NOT_EXECUTED` or `EXIT_COULD_NOT_ASK`; the
    module docstring says what each means and why 1 and 2 are distinct.
    """
    try:
        project, store = _wrapped_store(args)
        arguments = _parse_arguments(args.args)
        if not (math.isfinite(args.timeout) and args.timeout > 0):
            raise _CannotAsk(
                f"--timeout must be a positive number of seconds; got {args.timeout:g}."
            )
    except _CannotAsk as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_ASK

    command = gateway_argv(project, launcher=tegh_launcher(), home=store.home)
    try:
        with GatewayClient(
            command, env=harness_spawn_env(), timeout=args.timeout
        ) as gateway:
            gateway.initialize(client_name=CLIENT_NAME)
            result = gateway.call_tool(args.tool, arguments)
    except GatewayClientError as exc:
        # The error already carries the gateway's stderr, which is where a
        # refusal to boot says why.
        print(f"REFUSED: the gateway did not answer. {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_ASK
    except OSError as exc:
        # The launcher itself could not be started (moved, or not executable).
        # The harness's spawn fails the same way, with less to say about it.
        print(
            f"REFUSED: could not start the gateway ({command[0]}): {exc}",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_ASK

    executed = not result.get("isError", False)
    text = result_text(result)
    if args.json:
        print(json.dumps({"tool": args.tool, "executed": executed, "text": text}))
    else:
        print(text)
    return EXIT_EXECUTED if executed else EXIT_NOT_EXECUTED
