"""`tegh hook`: carry ONE Claude Code tool-call hook event to the broker.

    <launcher> hook --project /abs/project --home /abs/tegh-home   < event.json

A wrapped Claude Code's own tools (Read, Bash, Write, Edit, WebFetch, WebSearch
and the rest) never become MCP calls, so the gateway never sees them. Nor does it
see a call to an MCP server the wrap could not displace (a plugin's, or a
claude.ai connector). `tegh wrap claude` installs this one command in the
project's local settings under two events, `PreToolUse` and `PostToolUse`
(`hooksite.py`), and Claude Code runs it before and after each such call. Both
events are classified the same way, into the broker's closed vocabulary, and
carried to the mouth the project's gateway opened beside its stdio MCP surface:
a `PreToolUse` event as a decision request to `/decide` (`broker/GATEWAY.md` G30
to G38), a `PostToolUse` event as a report to `/events` (G21 to G29).

## Before the call, the broker decides and tegh relays

On `PreToolUse` the broker decides the call at the reserved coordinate
`harness-tool`, op `<tool_class>/<locality>`, from the project's manifest, and
answers `permit` or `refuse` (G30, G32). This command decides nothing. On a
permit it prints the harness's `allow`; on a refuse it prints the harness's
`deny` with the broker's `text` as the reason, verbatim and with nothing added
(`allow_output`, `deny_output`). The sentence the model reads on a refusal is
the base's (G38). A hold is a refuse whose text names the intent, and once a
person approves that intent the same call is permitted once (G36).

Three rules, in this order (`pre_tool_use_output`):

1. A tool the gateway serves (`GATEWAY_TOOL_PREFIX`) is allowed here and no
   request is made. The broker decides that call when it arrives at the
   gateway, and a second decision here would be a second decider. The `allow`
   only tells the harness not to ask the person first: it is routing, and
   grants nothing.
2. A tool listed in `HARNESS_INTERNAL_TOOLS` gets no output, so the harness's
   own permission rules apply to it.
3. Everything else is asked about. An MCP tool the gateway does not serve is
   asked about as `other` under `claude-code-mcp`, as it is reported (below).

A harness `allow` is only as strong as the harness makes it: it never
overrides a `deny` or `ask` permission rule and never approves the actions no
mode auto-approves (`docs/references/harnesses/claude-code.md` §3, "Can a hook
block a tool call?").

## When no decision can be had, the call does not proceed on tegh's account

A `PreToolUse` event that gets no decision (no gateway running, no address
file, no token, a refused connection, a timeout, an answer other than a `200`
carrying `permit` or `refuse`, a refuse with no text, a bad command line, or an
event naming no tool or no call id) is answered one way: ONE line on stderr,
`tegh hook: not decided: <cause>`, nothing on stdout, and exit 2, which the
harness reads as a block whatever stdout holds (reference §3, "Failure
semantics"). A failure this process never gets to answer, such as a timeout
the harness enforces or a launcher that cannot start, would by the harness's
default let the call proceed. The wrap's `PreToolUse` entry carries
`"onFailure": "block"` for those (`hooksite.py`), which refuses the call on any
hook failure, a timeout included, in Claude Code 2.1.295 and later (in its
changelog and not yet on its hooks page; observed 2026-10-09 and 2026-10-12).

One input is not answered that way: hook input that is not a JSON object
cannot be told apart from a `PostToolUse` event, so it is handled as one (exit
0, one `not recorded` line). Claude Code sends a JSON object for every event
(reference §3, "Hook input").

What exit 2 and `onFailure` hold against is a decider that is down. The hook
runs because the harness's settings name it, so whatever can change those
settings can stop the harness asking. Built-in tools are MEDIATED by this hook
and NOT CONTAINED by it.

## After the call, it observes

`PostToolUse` runs after the tool has run. Nothing here can stop, undo or alter
a built-in call, and nothing tries to: on this event the command always exits
0 and always prints nothing on stdout, because a hook's stdout is parsed by the
harness and an exit of 2 would put text in front of the model. What the report
adds is that the call is SEEN, paired on the tape with the decision asked
before it ran, and that a read outside the project counts against the turn the
way an MCP read does.

## What is reported, and what is not

Three rules, in this order (`report_of`):

1. A tool the gateway serves (`GATEWAY_TOOL_PREFIX`) is not reported. The broker
   decided that call and recorded it, and a second record would say it happened
   twice.
2. A tool listed in `HARNESS_INTERNAL_TOOLS` is not reported. See the rule there
   for what earns a listing. Silence is only ever a named row.
3. Everything else is reported. A built-in tool is classified by `TOOL_TABLE`,
   and a name no table lists is `other`, so a tool nobody has heard of is on the
   tape by default.

An MCP tool the gateway does not serve falls under rule 3. This command knows
nothing about that server, so it cannot say whether the call read or wrote. It
is reported as `other` under a harness code of its own, `claude-code-mcp`, which
is what names it on the tape as an MCP call that went around the broker. **It
does not taint the turn**: the platform's mouth taints on its read classes only,
and calling such a call a read would put a false statement on the tape. The
class that would let it taint is asked for upstream
(https://github.com/wjatx/ptc-gal-reference/issues/187).

## A report that fails is lost, and that is not a choice made here

When a report cannot be made (no gateway running, no address file, no token, a
refused connection, a 401, 400 or 500, a timeout) this command writes ONE line
to stderr, `tegh hook: not recorded: <cause>`, and still exits 0. The call it
was reporting has already run, goes unrecorded and taints nothing. The
reference this adapter is built against (`docs/references/harnesses/claude-code.md`
§3, "What a `PostToolUse` hook can and cannot do") records that a `PostToolUse`
hook cannot undo the call, and that exit 2 there only shows stderr to Claude.

## Digests only

A decision request carries six leaves and nothing else (G31): the harness code,
the tool class, the locality, the digest of the call's subject (a path, a URL,
a search query or a command line), the digest of the call's full input, and the
digest of Claude Code's id for the call (`tool_use_id`). A report carries up to
six (G23): the first four, the digest of the tool's response when the event
carries one, and the call digest when the event carries a call id, so the
report pairs on the tape with the decision asked before the call ran. No path,
URL, command, content, session id or call id leaves this process, on the wire
or on stderr. Every digest is `sha256:` and the hex SHA-256 of UTF-8 bytes. The
subject is digested exactly as Claude Code spelled it (G25), so a reader
holding a candidate path can recompute it; an input or a response is digested
as canonical JSON (sorted keys, no spaces, non-ASCII kept).

## A thin client

Standard library only, and nothing from the base: this runs twice per built-in
tool call, so it loads no broker runtime and opens no store. It posts with
`urllib`, with every proxy setting ignored and no redirect followed, because
the mouth is on loopback and a proxy named in the environment, or a redirect
to anywhere else, would otherwise be handed the launch token.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from tegh.store import TeghStore, tegh_home

#: The harness code a report names. A short lowercase code, as the mouth requires.
HARNESS_CODE = "claude-code"

#: The hook event this command asks the broker about, before the call runs.
PRE_TOOL_USE = "PreToolUse"

#: The hook event this command reports, after the call has run. An event that
#: is neither is ignored.
POST_TOOL_USE = "PostToolUse"

#: The harness code for an MCP call that did not go through tegh's gateway. The
#: mouth's tool classes are a closed set with no member for such a call, and its
#: harness code is not (`broker/GATEWAY.md` G23), so until the platform has a
#: class for it this code is what identifies the call on the tape. It names no
#: `trusted_read_sources` entry tegh writes, so nothing trusts it.
UNBROKERED_MCP_HARNESS_CODE = "claude-code-mcp"

#: Claude Code names every MCP tool `mcp__<server>__<tool>`.
MCP_TOOL_PREFIX = "mcp__"

#: The name a wrap gives the gateway's entry (`tegh/interpose.py`,
#: `InterposePlan.gateway_name`; a test holds the two equal). Spelled here and
#: not imported, because this process loads as little as it can.
GATEWAY_SERVER_NAME = "tegh"

#: Tools the gateway serves. Their calls are brokered there and recorded by the
#: broker, so reporting one here would record it twice.
GATEWAY_TOOL_PREFIX = f"{MCP_TOOL_PREFIX}{GATEWAY_SERVER_NAME}__"

#: Claude Code tools that are not reported at all.
#:
#: The rule for a row: the tool reaches no file, process or network of its own
#: and acts only on what the harness already holds in the session, AND that was
#: observed or is documented, with the source named beside the row. A tool that
#: is merely noisy does not qualify, and neither does one nobody has looked at:
#: a name this set does not list is reported (as `other` when `TOOL_TABLE` does
#: not list it either), so the next internal tool Claude Code adds shows up on
#: the tape until someone decides it belongs here.
#:
#: What a row costs: the tape is silent about that tool. For `ToolSearch` that
#: includes the fact that it puts a deferred tool's description in front of the
#: model, and for a server outside the gateway nobody admitted that description.
HARNESS_INTERNAL_TOOLS = frozenset(
    {
        # Loads the schema of a deferred tool from the harness's own registry.
        # Observed 2026-10-07 on Claude Code 2.1.292 (Third-Ralph/tegh#27).
        "ToolSearch",
    }
)

#: Claude Code's built-in tool name, mapped to the broker's closed `tool_class`
#: and to the `tool_input` keys that name the call's subject, first match wins.
#: Data, so a tool Claude Code adds or renames is one row here. A name not listed
#: is `other`, and its subject is the tool name itself.
#:
#: `file_path` is documented as always absolute for Read, Write and Edit
#: (reference §3, "Hook input"). MultiEdit takes the same key. NotebookEdit's
#: input is not documented in the reference; its notebook path is read from
#: `notebook_path` and then `file_path`, and a value that is missing or relative
#: is classified `unknown` rather than guessed.
TOOL_TABLE: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "Read": ("file-read", ("file_path",)),
    "Write": ("file-write", ("file_path",)),
    "Edit": ("file-edit", ("file_path",)),
    "MultiEdit": ("file-edit", ("file_path",)),
    "NotebookEdit": ("file-edit", ("notebook_path", "file_path")),
    "Bash": ("shell", ("command",)),
    "PowerShell": ("shell", ("command",)),
    "WebFetch": ("web-fetch", ("url",)),
    "WebSearch": ("web-search", ("query",)),
}
OTHER_CLASS = "other"

FILE_CLASSES = frozenset({"file-read", "file-write", "file-edit"})
WEB_CLASSES = frozenset({"web-fetch", "web-search"})

#: The mouth's report route (`broker/GATEWAY.md` G28).
EVENTS_PATH = "/events"

#: The mouth's decision route, on the same listener (`broker/GATEWAY.md` G38).
DECIDE_PATH = "/decide"

#: How long a report may take. The harness waits on this process, and the call
#: it reports has already finished.
POST_TIMEOUT_SECONDS = 2.0

#: How long a decision may take. The call waits on it. The broker decides
#: before it answers and writes a record first (G33), so this is longer than a
#: report's wait, and well inside the hook entry's own timeout
#: (`hooksite.HOOK_TIMEOUT_SECONDS`).
DECIDE_TIMEOUT_SECONDS = 5.0

#: The two answers a decision may carry (G32). Anything else is no decision.
PERMIT = "permit"
REFUSE = "refuse"

#: The exit status that blocks a `PreToolUse` call whatever stdout holds.
EXIT_NOT_DECIDED = 2

#: What `event-mouth.addr` holds: `host:port`, an IPv6 host in brackets.
_ADDRESS = re.compile(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<host>[A-Za-z0-9.-]+)):(?P<port>[0-9]{1,5})")

#: The stderr line for a `PostToolUse` event whose report was not made.
PREFIX = "tegh hook: not recorded: "

#: The stderr line for a `PreToolUse` event that got no decision.
DECIDE_PREFIX = "tegh hook: not decided: "


class HookFailure(Exception):
    """The event was not carried, or its answer not read. The message is the
    cause, and names no subject."""


# ---------------------------------------------------------------------------
# Classification: pure, and the whole of what a report says
# ---------------------------------------------------------------------------


def digest(text: str) -> str:
    """`sha256:` and the hex SHA-256 of `text`'s UTF-8 bytes, the mouth's one form."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_digest(value: Any) -> str:
    """The digest of a JSON value as canonical JSON (sorted keys, no spaces, non-ASCII kept)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return digest(canonical)


def result_digest(tool_response: Any) -> str:
    """The digest of a tool response, as canonical JSON."""
    return canonical_digest(tool_response)


def input_digest(tool_input: Any) -> str:
    """The digest of a call's full input, as canonical JSON. A missing input is `null`."""
    return canonical_digest(tool_input)


def tool_class(tool_name: str) -> str:
    return TOOL_TABLE.get(tool_name, (OTHER_CLASS, ()))[0]


def subject(tool_name: str, tool_input: Any) -> str:
    """What the call was about, as Claude Code spelled it; never sent, only digested.

    The first string named by the tool's subject keys. A known tool with none
    of them has the empty subject, so its report still lands; a tool this table
    does not know is identified by its own name.
    """
    if tool_name not in TOOL_TABLE:
        return tool_name
    keys = TOOL_TABLE[tool_name][1]
    if isinstance(tool_input, Mapping):
        for key in keys:
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return ""


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def locality(
    tool_name: str, tool_input: Any, *, project: Path, home: Path
) -> str:
    """Where the call's subject was: `project`, `home`, `outside`, `remote` or `unknown`.

    For the file classes, from the path Claude Code gave, with symbolic links
    and `..` resolved before it is compared: a link inside the project that
    points at `~/.ssh` is a read of `~/.ssh`, and comparing the spelling would
    call it a project read and trust it. A path that is missing or relative is
    `unknown`, never assumed to be in the project. The project is checked
    before the home, since a project usually sits inside one.

    The web classes are `remote`. A shell command or any other tool says
    nothing reliable about where it reached, so it is `unknown`.
    """
    kind = tool_class(tool_name)
    if kind in WEB_CLASSES:
        return "remote"
    if kind not in FILE_CLASSES:
        return "unknown"
    raw = subject(tool_name, tool_input)
    if not raw or not os.path.isabs(raw):
        return "unknown"
    seen = Path(os.path.realpath(raw))
    if _under(seen, Path(os.path.realpath(project))):
        return "project"
    if _under(seen, Path(os.path.realpath(home))):
        return "home"
    return "outside"


def served_by_gateway(event: Mapping[str, Any]) -> bool:
    """Whether the event's tool is one the project's gateway serves.

    By the tool name's prefix. Where Claude Code also says which server the
    tool came from (`mcp_server`, v2.1.274 or later; reference §3, "Hook
    input"), that name has to agree, because a server named `tegh__x` would
    otherwise wear the gateway's prefix and go unreported.
    """
    name = event.get("tool_name")
    if not isinstance(name, str) or not name.startswith(GATEWAY_TOOL_PREFIX):
        return False
    server = event.get("mcp_server")
    if isinstance(server, Mapping) and isinstance(server.get("name"), str):
        return server["name"] == GATEWAY_SERVER_NAME
    return True


def _classified(name: str, tool_input: Any, *, project: Path, home: Path) -> dict[str, str]:
    """The four leaves a request and a report share: who, what, where, and about what.

    An MCP tool the gateway does not serve is named under
    `UNBROKERED_MCP_HARNESS_CODE`, with its own name as the subject.
    """
    unbrokered_mcp = name.startswith(MCP_TOOL_PREFIX)
    return {
        "harness": UNBROKERED_MCP_HARNESS_CODE if unbrokered_mcp else HARNESS_CODE,
        "tool_class": tool_class(name),
        "locality": locality(name, tool_input, project=project, home=home),
        "subject_digest": digest(subject(name, tool_input)),
    }


def report_of(
    event: Mapping[str, Any], *, project: Path, home: Path
) -> Optional[dict[str, str]]:
    """The report for one hook event, or None when the event is not reported.

    None for any event that is not `PostToolUse`, for a tool the gateway
    serves, and for a listed harness-internal tool (the module docstring, "What
    is reported"). Otherwise four keys and up to two more: `result_digest` when
    the event carries a tool response, and `call_digest` when it carries a
    string `tool_use_id`.
    """
    if event.get("hook_event_name") != POST_TOOL_USE:
        return None
    name = event.get("tool_name")
    if not isinstance(name, str):
        return None
    if served_by_gateway(event) or name in HARNESS_INTERNAL_TOOLS:
        return None
    report = _classified(name, event.get("tool_input"), project=project, home=home)
    if event.get("tool_response") is not None:
        report["result_digest"] = result_digest(event["tool_response"])
    call_id = event.get("tool_use_id")
    if isinstance(call_id, str):
        report["call_digest"] = digest(call_id)
    return report


def request_of(event: Mapping[str, Any], *, project: Path, home: Path) -> dict[str, str]:
    """The decision request for one `PreToolUse` event: exactly six keys (G31).

    `HookFailure` for an event that names no tool or carries no string
    `tool_use_id`: without the call id the decision cannot be paired with the
    report made after the call, and the request would be refused as malformed.
    Which tools are asked about at all is `pre_tool_use_output`'s.
    """
    name = event.get("tool_name")
    if not isinstance(name, str):
        raise HookFailure("the hook input names no tool")
    call_id = event.get("tool_use_id")
    if not isinstance(call_id, str):
        raise HookFailure("the hook input carries no tool_use_id")
    tool_input = event.get("tool_input")
    return {
        **_classified(name, tool_input, project=project, home=home),
        "input_digest": input_digest(tool_input),
        "call_digest": digest(call_id),
    }


def allow_output() -> str:
    """What a permit prints: the harness's `allow`, one line, and no reason."""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": PRE_TOOL_USE, "permissionDecision": "allow"}}
    )


def deny_output(text: str) -> str:
    """What a refuse prints: the harness's `deny`, with the broker's `text` as the reason.

    `text` goes in verbatim. tegh composes no model-facing sentence of its own.
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": text,
            }
        }
    )


def output_of(answer: Any) -> str:
    """The stdout line for the broker's answer, or `HookFailure` when it is no decision.

    A JSON object whose `answer` is `permit` is an `allow`. One whose `answer`
    is `refuse` and whose `text` is a string is a `deny` carrying that text.
    Anything else decides nothing here.
    """
    if not isinstance(answer, Mapping):
        raise HookFailure("the decision answer is not a JSON object")
    word = answer.get("answer")
    if word == PERMIT:
        return allow_output()
    if word == REFUSE:
        text = answer.get("text")
        if not isinstance(text, str):
            raise HookFailure("the refusal carries no text")
        return deny_output(text)
    raise HookFailure("the decision answer is neither permit nor refuse")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def mouth_address(path: Path) -> tuple[str, int]:
    """The `(host, port)` the gateway wrote to `path`, or `HookFailure`.

    Loopback only. tegh never names a host for the mouth, so the gateway binds
    the base's loopback default; an address file naming anything else was not
    written by that gateway, and posting the launch token to it would hand the
    token to whoever listens there.
    """
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise HookFailure(
            "no tool-event mouth address (the project's gateway is not running)"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise HookFailure(f"the tool-event mouth address could not be read ({exc})") from None
    found = _ADDRESS.fullmatch(text.strip())
    if found is None:
        raise HookFailure("the tool-event mouth address is not host:port")
    host = found.group("v6") or found.group("host")
    port = int(found.group("port"))
    if not _is_loopback(host) or not 0 < port < 65536:
        raise HookFailure("the tool-event mouth address is not a loopback port")
    return host, port


#: How long `tegh gateway` waits on a connect to decide whether an address
#: file is live. Loopback answers or refuses at once.
LISTENING_PROBE_SECONDS = 0.5


def mouth_is_listening(path: Path) -> bool:
    """Whether something accepts a connection at the address `path` names.

    For `tegh gateway`, before it starts: a project's gateway can be started
    more than once at a time (`tegh call` starts its own, and a harness may
    start one to list a server's tools), and the one the session's hooks report
    to must not lose its address file to one of those. A bare connect, closed
    at once, and no request, so nothing is posted and nothing is recorded.
    False for a missing or malformed file.
    """
    import socket  # noqa: PLC0415 - only the gateway launcher needs it

    try:
        address = mouth_address(path)
    except HookFailure:
        return False
    try:
        with socket.create_connection(address, timeout=LISTENING_PROBE_SECONDS):
            return True
    except OSError:
        return False


def launch_token(path: Path) -> str:
    """The launch token, one trailing newline dropped, or `HookFailure`."""
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise HookFailure("no launch token (the project's gateway has never started)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise HookFailure(f"the launch token could not be read ({exc})") from None
    token = raw[:-1] if raw.endswith("\n") else raw
    if not token:
        raise HookFailure("the launch token file is empty")
    return token


def _url(host: str, port: int, path: str) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}{path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. `urllib` would re-send the bearer token to wherever one
    pointed; a redirect surfaces as an `HTTPError` carrying its status instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: An opener that ignores every proxy variable and follows no redirect. See "A
#: thin client" above.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _send(
    body: Mapping[str, str], *, path: str, address: tuple[str, int], token: str, timeout: float
) -> tuple[int, bytes]:
    """POST one JSON body to the mouth at `path`. `(status, body)` on a 2xx;
    `HookFailure` otherwise."""
    request = urllib.request.Request(
        _url(*address, path),
        data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        raise HookFailure(f"the tool-event mouth answered {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise HookFailure("the tool-event mouth did not answer in time") from None
        detail = getattr(reason, "strerror", None) or type(reason).__name__
        raise HookFailure(f"the tool-event mouth could not be reached ({detail})") from None
    except TimeoutError:
        raise HookFailure("the tool-event mouth did not answer in time") from None
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise HookFailure(f"the tool-event mouth could not be reached ({detail})") from None


def post(report: Mapping[str, str], *, address: tuple[str, int], token: str) -> None:
    """POST one report to `/events`. Returns on a 2xx; `HookFailure` otherwise."""
    _send(report, path=EVENTS_PATH, address=address, token=token, timeout=POST_TIMEOUT_SECONDS)


def decide(request: Mapping[str, str], *, address: tuple[str, int], token: str) -> Any:
    """POST one decision request to `/decide` and return the parsed answer.

    The route answers every authenticated request `200` (G32), so any other
    status, a 2xx included, is no answer. `HookFailure` for that and for a body
    that is not JSON. What the JSON says is `output_of`'s to judge.
    """
    status, raw = _send(
        request, path=DECIDE_PATH, address=address, token=token, timeout=DECIDE_TIMEOUT_SECONDS
    )
    if status != 200:
        raise HookFailure(f"the tool-event mouth answered {status}")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HookFailure("the decision answer is not JSON") from None


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _parse(argv: Sequence[str]) -> tuple[Path, TeghStore]:
    """`--project` and `--home`, in either order, and nothing else.

    Parsed by hand rather than with argparse, which answers a bad command line
    with its own exit 2 and usage text: the exit status here depends on the
    event (`hook_main`), so this parser only raises.
    """
    values: dict[str, str] = {}
    words = list(argv)
    while words:
        flag = words.pop(0)
        if flag not in ("--project", "--home") or not words or flag in values:
            raise HookFailure("usage: tegh hook --project <path> --home <path>")
        values[flag] = words.pop(0)
    if "--project" not in values:
        raise HookFailure("usage: tegh hook --project <path> --home <path>")
    project = Path(values["--project"]).expanduser().resolve()
    home = Path(values["--home"]).expanduser() if "--home" in values else tegh_home()
    return project, TeghStore(home=home)


def _read_event(stream) -> Mapping[str, Any]:
    try:
        event = json.load(stream)
    except (ValueError, UnicodeDecodeError):
        raise HookFailure("the hook input is not JSON") from None
    if not isinstance(event, dict):
        raise HookFailure("the hook input is not a JSON object")
    return event


def _mouth(store: TeghStore, project: Path) -> tuple[tuple[str, int], str]:
    """This project's mouth address and launch token, or `HookFailure`."""
    address = mouth_address(store.event_mouth_addr_path(project))
    token = launch_token(store.gateway_token_path(project))
    return address, token


def report_event(event: Mapping[str, Any], *, project: Path, store: TeghStore) -> None:
    """Classify one event and post it to this project's mouth, or raise `HookFailure`."""
    report = report_of(event, project=project, home=Path.home())
    if report is None:
        return
    address, token = _mouth(store, project)
    post(report, address=address, token=token)


def pre_tool_use_output(
    event: Mapping[str, Any], *, project: Path, store: TeghStore
) -> Optional[str]:
    """The stdout line for one `PreToolUse` event, None for no output, or `HookFailure`.

    The three rules in the module docstring ("Before the call"), in order.
    """
    name = event.get("tool_name")
    if not isinstance(name, str):
        raise HookFailure("the hook input names no tool")
    if served_by_gateway(event):
        return allow_output()
    if name in HARNESS_INTERNAL_TOOLS:
        return None
    request = request_of(event, project=project, home=Path.home())
    address, token = _mouth(store, project)
    return output_of(decide(request, address=address, token=token))


def _pre_tool_use(argv: Sequence[str], event: Mapping[str, Any]) -> int:
    """Relay the broker's decision, or exit 2 with one line on stderr."""
    try:
        project, store = _parse(argv)
        line = pre_tool_use_output(event, project=project, store=store)
        if line is not None:
            print(line)
    except HookFailure as exc:
        print(f"{DECIDE_PREFIX}{exc}", file=sys.stderr)
        return EXIT_NOT_DECIDED
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - no decision is a block
        print(f"{DECIDE_PREFIX}{type(exc).__name__}", file=sys.stderr)
        return EXIT_NOT_DECIDED
    return 0


def _post_tool_use(argv: Sequence[str], event: Mapping[str, Any]) -> int:
    """Report the call, or say in one line on stderr that it was not recorded. Exit 0."""
    try:
        project, store = _parse(argv)
        report_event(event, project=project, store=store)
    except HookFailure as exc:
        print(f"{PREFIX}{exc}", file=sys.stderr)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - an observer fails open
        print(f"{PREFIX}{type(exc).__name__}", file=sys.stderr)
    return 0


def hook_main(argv: Sequence[str], *, stdin=None) -> int:
    """Run the hook and return its exit status.

    The event is read before the command line, because what a failure does
    depends on the event: `EXIT_NOT_DECIDED` for a `PreToolUse` event that got
    no decision, 0 for everything else (the module docstring says why). Input
    that is not a JSON object is handled as a `PostToolUse` event's failure.
    """
    try:
        event = _read_event(stdin or sys.stdin)
    except HookFailure as exc:
        print(f"{PREFIX}{exc}", file=sys.stderr)
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - see the docstring
        print(f"{PREFIX}{type(exc).__name__}", file=sys.stderr)
        return 0
    name = event.get("hook_event_name")
    if name == PRE_TOOL_USE:
        return _pre_tool_use(argv, event)
    if name == POST_TOOL_USE:
        return _post_tool_use(argv, event)
    return 0
