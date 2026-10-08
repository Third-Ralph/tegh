"""`tegh hook`: report ONE Claude Code `PostToolUse` event to the broker's tool-event mouth.

    <launcher> hook --project /abs/project --home /abs/tegh-home   < event.json

A wrapped Claude Code's own tools (Read, Bash, Write, Edit, WebFetch, WebSearch
and the rest) never become MCP calls, so the gateway never sees them. Nor does it
see a call to an MCP server the wrap could not displace (a plugin's, or a
claude.ai connector). `tegh wrap claude` installs this command as a `PostToolUse`
hook in the project's local settings, and Claude Code runs it after each such call. It classifies the call
into the broker's closed vocabulary and posts the classification to the mouth the
project's gateway opened beside its stdio MCP surface. The broker records it on
the audit tape and, when the call read content from somewhere the project's
manifest does not trust, taints the session turn, so the agent's next external
write through the gateway is held for a person.

## It observes, and gates nothing

`PostToolUse` runs after the tool has run. Nothing here can stop, undo or alter a
built-in call, and nothing tries to: this command always exits 0 and always
prints nothing on stdout, because a hook's stdout is parsed by the harness and an
exit of 2 would put text in front of the model. Built-in tools stay ungated and
uncontained. What this adds is that they are SEEN, and that a read outside the
project counts against the turn the way an MCP read does.

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

## Every failure fails open, and that is not a choice made here

When the report cannot be made (no gateway running, no address file, no token, a
refused connection, a 401, 400 or 500, a timeout) this command writes ONE line to
stderr, `tegh hook: not recorded: <cause>`, and still exits 0. The call it was
reporting goes unrecorded and taints nothing. That is the harness's own rule
too: the reference this adapter is built against
(`docs/references/harnesses/claude-code.md` §3, "Failure semantics") records
that a command hook which errors, times out or is missing lets the harness carry
on, and that no setting found makes a settings-file hook's failure block. So an
agent that can stop this hook from running, or from reaching the gateway, is
unobserved again, exactly as it was before the hook existed. Containment, not a
hook, is what closes that (posture 2).

## Digests only

The report carries five leaves and nothing else: the harness code, the tool
class, the locality, the digest of the call's subject (a path, a URL, a search
query or a command line), and the digest of the tool's response. No path, URL,
command, content or session id leaves this process, on the wire or on stderr.
The subject digest is `sha256:` and the hex SHA-256 of the subject's UTF-8 bytes
exactly as Claude Code spelled it (`broker/GATEWAY.md` G25), so a reader holding
a candidate path can recompute it.

## A thin client

Standard library only, and nothing from the base: this runs once per built-in
tool call, so it loads no broker runtime and opens no store. It posts with
`urllib`, with every proxy setting ignored, because the mouth is on loopback and
a proxy named in the environment would otherwise be handed the launch token.
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

#: The one hook event this command reports. Anything else is ignored.
HOOK_EVENT = "PostToolUse"

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

#: The mouth's one path (`broker/GATEWAY.md` G28).
EVENTS_PATH = "/events"

#: How long a report may take. The harness waits on this process, and the call
#: it reports has already finished.
POST_TIMEOUT_SECONDS = 2.0

#: What `event-mouth.addr` holds: `host:port`, an IPv6 host in brackets.
_ADDRESS = re.compile(r"(?:\[(?P<v6>[0-9A-Fa-f:.]+)\]|(?P<host>[A-Za-z0-9.-]+)):(?P<port>[0-9]{1,5})")

PREFIX = "tegh hook: not recorded: "


class NotRecorded(Exception):
    """The report was not made. The message is the cause, and names no subject."""


# ---------------------------------------------------------------------------
# Classification: pure, and the whole of what a report says
# ---------------------------------------------------------------------------


def digest(text: str) -> str:
    """`sha256:` and the hex SHA-256 of `text`'s UTF-8 bytes, the mouth's one form."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def result_digest(tool_response: Any) -> str:
    """The digest of a tool response, as canonical JSON (sorted keys, no spaces)."""
    canonical = json.dumps(
        tool_response, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return digest(canonical)


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


def report_of(
    event: Mapping[str, Any], *, project: Path, home: Path
) -> Optional[dict[str, str]]:
    """The report for one hook event, or None when the event is not reported.

    None for any event that is not `PostToolUse`, for a tool the gateway
    serves, and for a listed harness-internal tool (the module docstring, "What
    is reported"). Otherwise exactly five keys or four: `result_digest` is left
    out when the event carries no tool response. An MCP tool the gateway does
    not serve is reported under `UNBROKERED_MCP_HARNESS_CODE`, with its own
    name as the subject.
    """
    if event.get("hook_event_name") != HOOK_EVENT:
        return None
    name = event.get("tool_name")
    if not isinstance(name, str):
        return None
    if served_by_gateway(event) or name in HARNESS_INTERNAL_TOOLS:
        return None
    unbrokered_mcp = name.startswith(MCP_TOOL_PREFIX)
    tool_input = event.get("tool_input")
    report = {
        "harness": UNBROKERED_MCP_HARNESS_CODE if unbrokered_mcp else HARNESS_CODE,
        "tool_class": tool_class(name),
        "locality": locality(name, tool_input, project=project, home=home),
        "subject_digest": digest(subject(name, tool_input)),
    }
    if event.get("tool_response") is not None:
        report["result_digest"] = result_digest(event["tool_response"])
    return report


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
    """The `(host, port)` the gateway wrote to `path`, or `NotRecorded`.

    Loopback only. tegh never names a host for the mouth, so the gateway binds
    the base's loopback default; an address file naming anything else was not
    written by that gateway, and posting the launch token to it would hand the
    token to whoever listens there.
    """
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise NotRecorded(
            "no tool-event mouth address (the project's gateway is not running)"
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise NotRecorded(f"the tool-event mouth address could not be read ({exc})") from None
    found = _ADDRESS.fullmatch(text.strip())
    if found is None:
        raise NotRecorded("the tool-event mouth address is not host:port")
    host = found.group("v6") or found.group("host")
    port = int(found.group("port"))
    if not _is_loopback(host) or not 0 < port < 65536:
        raise NotRecorded("the tool-event mouth address is not a loopback port")
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
    except NotRecorded:
        return False
    try:
        with socket.create_connection(address, timeout=LISTENING_PROBE_SECONDS):
            return True
    except OSError:
        return False


def launch_token(path: Path) -> str:
    """The launch token, one trailing newline dropped, or `NotRecorded`."""
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        raise NotRecorded("no launch token (the project's gateway has never started)") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise NotRecorded(f"the launch token could not be read ({exc})") from None
    token = raw[:-1] if raw.endswith("\n") else raw
    if not token:
        raise NotRecorded("the launch token file is empty")
    return token


def _url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}{EVENTS_PATH}"


#: An opener that ignores every proxy variable. See "A thin client" above.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(report: Mapping[str, str], *, address: tuple[str, int], token: str) -> None:
    """POST one report to the mouth. Returns on a 2xx; `NotRecorded` otherwise."""
    request = urllib.request.Request(
        _url(*address),
        data=json.dumps(dict(report), separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=POST_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise NotRecorded(f"the tool-event mouth answered {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise NotRecorded("the tool-event mouth did not answer in time") from None
        detail = getattr(reason, "strerror", None) or type(reason).__name__
        raise NotRecorded(f"the tool-event mouth could not be reached ({detail})") from None
    except TimeoutError:
        raise NotRecorded("the tool-event mouth did not answer in time") from None
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise NotRecorded(f"the tool-event mouth could not be reached ({detail})") from None


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _parse(argv: Sequence[str]) -> tuple[Path, TeghStore]:
    """`--project` and `--home`, in either order, and nothing else.

    Parsed by hand rather than with argparse, which answers a bad command line
    with exit 2: for a `PostToolUse` hook that puts its stderr in front of the
    model, which an observer must never do.
    """
    values: dict[str, str] = {}
    words = list(argv)
    while words:
        flag = words.pop(0)
        if flag not in ("--project", "--home") or not words or flag in values:
            raise NotRecorded("usage: tegh hook --project <path> --home <path>")
        values[flag] = words.pop(0)
    if "--project" not in values:
        raise NotRecorded("usage: tegh hook --project <path> --home <path>")
    project = Path(values["--project"]).expanduser().resolve()
    home = Path(values["--home"]).expanduser() if "--home" in values else tegh_home()
    return project, TeghStore(home=home)


def _read_event(stream) -> Mapping[str, Any]:
    try:
        event = json.load(stream)
    except (ValueError, UnicodeDecodeError):
        raise NotRecorded("the hook input is not JSON") from None
    if not isinstance(event, dict):
        raise NotRecorded("the hook input is not a JSON object")
    return event


def report_event(event: Mapping[str, Any], *, project: Path, store: TeghStore) -> None:
    """Classify one event and post it to this project's mouth, or raise `NotRecorded`."""
    report = report_of(event, project=project, home=Path.home())
    if report is None:
        return
    address = mouth_address(store.event_mouth_addr_path(project))
    token = launch_token(store.gateway_token_path(project))
    post(report, address=address, token=token)


def hook_main(argv: Sequence[str], *, stdin=None) -> int:
    """Run the hook. Always returns 0; see the module docstring for why."""
    try:
        project, store = _parse(argv)
        report_event(_read_event(stdin or sys.stdin), project=project, store=store)
    except NotRecorded as exc:
        print(f"{PREFIX}{exc}", file=sys.stderr)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - an observer fails open
        print(f"{PREFIX}{type(exc).__name__}", file=sys.stderr)
    return 0
