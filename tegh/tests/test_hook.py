"""`tegh hook`, without a gateway: what it classifies, what it sends, what it relays.

Nothing here starts a broker. A request's or a report's body is caught by a
stdlib HTTP server on a thread, standing where the gateway's tool-event mouth
would, so a test can say exactly which bytes left the process, and the answer
it gives is the test's. The real mouth, the real tape and the taint it causes
are `test_hook_e2e.py`.

The event fixtures under `events/` are built from the field list in
`docs/references/harnesses/claude-code.md` §3 ("Hook input"). They were not
captured from a running Claude Code, and the shape of a `Read` tool response is
undocumented there, so the response in the `PostToolUse` one is illustrative.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tegh import hook
from tegh.cli import main
from tegh.store import TeghStore

EVENT_FIXTURE = Path(__file__).with_name("events") / "post_tool_use_read.json"
PRE_EVENT_FIXTURE = Path(__file__).with_name("events") / "pre_tool_use_bash.json"
TOKEN = "t" * 64

#: A refusal's text as the broker spells it (`broker/GATEWAY.md` G38), with a
#: quote, a non-ASCII character and a newline in its reason, so a relay that
#: re-spells anything shows.
REFUSED_TEXT = (
    "Refused by policy. Do not attempt the same action another way. Tell the user "
    "what was refused, and continue with any part of the task this does not affect. "
    'Reason: no manifest entry for harness-tool.shell/unknown ("é")\nsecond line'
)

#: A tool the wrapped gateway serves, as Claude Code names it.
GATEWAY_TOOL = "mcp__tegh__ledger__get_entry"

#: What Claude Code 2.1.274 and later sends beside a call to the gateway's tool.
GATEWAY_SERVER = {"name": "tegh", "source": "local"}

#: `mcp_server` values that say nothing about which server answered, each with
#: the gateway's tool name: absent, empty, not an object, and a `null` name.
MALFORMED_SERVERS = [
    pytest.param({}, id="mcp_server absent"),
    pytest.param({"mcp_server": {}}, id="mcp_server {}"),
    pytest.param({"mcp_server": "evil"}, id="mcp_server a string"),
    pytest.param({"mcp_server": {"name": None}}, id="mcp_server name null"),
]


def recorded_event(**changes) -> dict:
    event = json.loads(EVENT_FIXTURE.read_text(encoding="utf-8"))
    event.update(changes)
    return event


def pre_event(**changes) -> dict:
    event = json.loads(PRE_EVENT_FIXTURE.read_text(encoding="utf-8"))
    event.update(changes)
    return event


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("Read", "file-read"),
        ("Write", "file-write"),
        ("Edit", "file-edit"),
        ("MultiEdit", "file-edit"),
        ("NotebookEdit", "file-edit"),
        ("Bash", "shell"),
        ("PowerShell", "shell"),
        ("WebFetch", "web-fetch"),
        ("WebSearch", "web-search"),
        ("Glob", "other"),
        ("TodoWrite", "other"),
        ("Agent", "other"),
    ],
)
def test_each_tool_name_maps_to_one_class_of_the_brokers_closed_vocabulary(tool_name, expected):
    assert hook.tool_class(tool_name) == expected


def test_the_table_speaks_only_the_brokers_vocabulary():
    """The mouth refuses a class outside its enum with a 400 that records nothing."""
    allowed = {"file-read", "file-write", "file-edit", "shell", "web-fetch", "web-search", "other"}
    assert {kind for kind, _ in hook.TOOL_TABLE.values()} | {hook.OTHER_CLASS} <= allowed


@pytest.fixture
def places(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    project = home / "code" / "widget"
    project.mkdir(parents=True)
    (home / ".ssh").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    return {"home": home, "project": project, "outside": outside}


@pytest.mark.parametrize(
    ("tool_name", "where", "expected"),
    [
        ("Read", lambda p: str(p["project"] / "src" / "main.py"), "project"),
        ("Read", lambda p: str(p["project"]), "project"),
        ("Edit", lambda p: str(p["project"] / "README.md"), "project"),
        ("Read", lambda p: str(p["home"] / ".ssh" / "id_ed25519"), "home"),
        ("Write", lambda p: str(p["home"] / "notes.md"), "home"),
        ("Read", lambda p: str(p["outside"] / "x"), "outside"),
        ("Read", lambda p: "/etc/hosts", "outside"),
        # `..` out of the project is not in the project.
        ("Read", lambda p: str(p["project"] / ".." / ".." / ".ssh" / "id"), "home"),
        ("Read", lambda p: "src/main.py", "unknown"),
        ("Read", lambda p: None, "unknown"),
        ("Read", lambda p: "", "unknown"),
        ("WebFetch", lambda p: "https://example.com/", "remote"),
        ("WebSearch", lambda p: None, "remote"),
        ("Bash", lambda p: str(p["outside"]), "unknown"),
        ("Glob", lambda p: str(p["project"]), "unknown"),
    ],
)
def test_locality(places, tool_name, where, expected):
    value = where(places)
    key = {"WebFetch": "url", "WebSearch": "query", "Bash": "command"}.get(tool_name, "file_path")
    tool_input = {} if value is None else {key: value}
    assert (
        hook.locality(tool_name, tool_input, project=places["project"], home=places["home"])
        == expected
    )


def test_a_link_inside_the_project_is_classified_by_where_it_points(places):
    """Comparing the spelling would call a read of `~/.ssh` a project read, and trust it."""
    link = places["project"] / "keys"
    link.symlink_to(places["home"] / ".ssh")
    tool_input = {"file_path": str(link / "id_ed25519")}
    assert (
        hook.locality("Read", tool_input, project=places["project"], home=places["home"])
        == "home"
    )


def test_the_subject_digest_is_sha256_of_the_subject_as_claude_code_spelled_it(places):
    spelled = str(places["project"] / ".." / "widget" / "a.py")
    event = recorded_event(tool_input={"file_path": spelled})
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert report["subject_digest"] == _sha(spelled)
    assert report["locality"] == "project"


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "subject"),
    [
        ("WebFetch", {"url": "https://example.com/a?b=c", "prompt": "summarize"}, "https://example.com/a?b=c"),
        ("WebSearch", {"query": "tegh broker"}, "tegh broker"),
        ("Bash", {"command": "cat ~/.ssh/id_rsa", "description": "x"}, "cat ~/.ssh/id_rsa"),
        ("NotebookEdit", {"notebook_path": "/n.ipynb", "new_source": "x"}, "/n.ipynb"),
        ("Glob", {"pattern": "**/*.py"}, "Glob"),
        ("Read", {}, ""),
    ],
)
def test_the_subject_of_each_kind_of_call(tool_name, tool_input, subject):
    assert hook.subject(tool_name, tool_input) == subject


def test_the_result_digest_is_over_canonical_json():
    response = {"b": [1, "é"], "a": {"y": 2, "x": 1}}
    canonical = '{"a":{"x":1,"y":2},"b":[1,"é"]}'
    assert hook.result_digest(response) == _sha(canonical)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(recorded_event(hook_event_name="PreToolUse"), id="PreToolUse"),
        pytest.param(recorded_event(hook_event_name="PostToolUseFailure"), id="failure"),
        pytest.param(recorded_event(hook_event_name="SessionStart"), id="other event"),
        pytest.param(
            recorded_event(tool_name=GATEWAY_TOOL, mcp_server=GATEWAY_SERVER),
            id="gateway tool, server named",
        ),
        pytest.param(recorded_event(tool_name="ToolSearch"), id="harness-internal tool"),
        pytest.param(recorded_event(tool_name=None), id="no tool name"),
    ],
)
def test_events_that_are_not_reported(places, event):
    assert hook.report_of(event, project=places["project"], home=places["home"]) is None


def test_the_gateway_prefix_is_the_name_a_wrap_gives_the_gateway():
    """The skip rule is a rule about one server name, so it must be the wrap's.

    And the name in the tool's prefix is not enough: the event's `mcp_server`
    has to name the gateway too (reference §3, "Hook input").
    """
    from tegh import interpose

    assert hook.GATEWAY_SERVER_NAME == interpose._UNRECORDED_GATEWAY_NAME
    assert hook.GATEWAY_SERVER_NAME == interpose.InterposePlan.__dataclass_fields__[
        "gateway_name"
    ].default
    assert hook.GATEWAY_TOOL_PREFIX == "mcp__tegh__"
    assert hook.served_by_gateway({"tool_name": GATEWAY_TOOL, "mcp_server": GATEWAY_SERVER})
    assert not hook.served_by_gateway({"tool_name": GATEWAY_TOOL})


@pytest.mark.parametrize("server", MALFORMED_SERVERS)
def test_a_gateway_name_without_the_gateway_server_is_reported_as_unbrokered(places, server):
    """The prefix alone never makes a call the gateway's: it is on the tape as `other`."""
    event = recorded_event(tool_name=GATEWAY_TOOL)
    event.pop("mcp_server", None)
    event.update(server)
    assert not hook.served_by_gateway(event)
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert (report["harness"], report["tool_class"], report["subject_digest"]) == (
        "claude-code-mcp",
        "other",
        _sha(GATEWAY_TOOL),
    )


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            recorded_event(tool_name="mcp__plugin_notes_vault__read_note"), id="plugin server"
        ),
        pytest.param(
            recorded_event(tool_name="mcp__claude_ai_Mail__search_threads"), id="connector"
        ),
        pytest.param(recorded_event(tool_name="mcp__teghx__get"), id="a name that only starts alike"),
        pytest.param(
            recorded_event(
                tool_name="mcp__tegh__x__get", mcp_server={"name": "tegh__x", "source": "user"}
            ),
            id="another server wearing the gateway's prefix",
        ),
    ],
)
def test_an_mcp_tool_the_gateway_does_not_serve_is_reported_as_unbrokered(places, event):
    """Third-Ralph/tegh#26: the `mcp__` prefix is not proof a call was brokered.

    Reported under a harness code of its own, which is what names it on the
    tape, and as `other`: this adapter cannot say whether the call read, and
    none of the mouth's read classes would be a true statement about it.
    """
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert report == {
        "harness": "claude-code-mcp",
        "tool_class": "other",
        "locality": "unknown",
        "subject_digest": _sha(event["tool_name"]),
        "result_digest": hook.result_digest(event["tool_response"]),
        "call_digest": _sha(event["tool_use_id"]),
    }


def test_only_a_listed_internal_tool_goes_unreported(places):
    """Third-Ralph/tegh#27: silence is a named row, and an unlisted tool is on the tape."""
    assert hook.HARNESS_INTERNAL_TOOLS == {"ToolSearch"}
    assert not hook.HARNESS_INTERNAL_TOOLS & set(hook.TOOL_TABLE)
    unlisted = recorded_event(tool_name="ToolSearchNext")
    report = hook.report_of(unlisted, project=places["project"], home=places["home"])
    assert (report["harness"], report["tool_class"], report["locality"]) == (
        "claude-code",
        "other",
        "unknown",
    )


def test_a_report_is_six_leaves_and_nothing_from_the_event(places):
    """No path, URL, command, content, session id or call id is in what leaves."""
    event = recorded_event()
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert set(report) == {
        "harness", "tool_class", "locality", "subject_digest", "result_digest", "call_digest",
    }
    sent = json.dumps(report)
    for leaked in (
        event["session_id"], event["cwd"], event["transcript_path"],
        event["tool_input"]["file_path"], event["tool_response"]["file"]["content"],
        event["tool_use_id"],
    ):
        assert leaked not in sent


def test_an_event_with_no_tool_response_carries_no_result_digest(places):
    event = recorded_event()
    del event["tool_response"]
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert set(report) == {"harness", "tool_class", "locality", "subject_digest", "call_digest"}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param({}, _sha("toolu_00000000000000000000000000"), id="tool_use_id"),
        pytest.param({"tool_use_id": None}, None, id="null"),
        pytest.param({"tool_use_id": 7}, None, id="not a string"),
        pytest.param({"tool_use_id": ...}, None, id="absent"),
    ],
)
def test_a_report_carries_the_call_digest_only_when_the_event_has_a_call_id(
    places, changes, expected
):
    """So the report pairs on the tape with the decision asked before the call (G23)."""
    event = recorded_event(**changes)
    if event.get("tool_use_id") is ...:
        del event["tool_use_id"]
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert report.get("call_digest") == expected


# ---------------------------------------------------------------------------
# Delivery, against a stand-in for the mouth
# ---------------------------------------------------------------------------


class _Mouth:
    """A loopback HTTP server that records what it was sent and answers `status`."""

    def __init__(self, status: int = 200, body: bytes = b'{"source":null}', **headers) -> None:
        self.status = status
        self.body = body
        self.headers = headers
        self.delay = 0.0
        self.requests: list[dict] = []
        mouth = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - the stdlib's name
                length = int(self.headers.get("content-length", "0"))
                mouth.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": self.rfile.read(length),
                    }
                )
                if mouth.delay:
                    time.sleep(mouth.delay)
                body = mouth.body
                self.send_response(mouth.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                for name, value in mouth.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        # A short poll, so a test's `shutdown` returns at once and not after 0.5 s.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def wired(tmp_path: Path, places, monkeypatch):
    """A tegh home whose project has a launch token and, once `listen` is called, an address."""
    monkeypatch.setenv("HOME", str(places["home"]))
    store = TeghStore(home=tmp_path / "tegh")
    project = places["project"]
    token = store.gateway_token_path(project)
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN + "\n", encoding="utf-8")

    def listen(address: str) -> None:
        store.event_mouth_addr_path(project).write_text(address + "\n", encoding="ascii")

    argv = ["--project", str(project), "--home", str(store.home)]
    return {"store": store, "project": project, "listen": listen, "argv": argv, **places}


def _run(argv, event, capsys) -> tuple[int, str, str]:
    stdin = io.StringIO(event if isinstance(event, str) else json.dumps(event))
    status = hook.hook_main(argv, stdin=stdin)
    out, err = capsys.readouterr()
    return status, out, err


def test_a_report_is_posted_to_the_mouth_with_the_token_and_nothing_else(wired, capsys):
    target = wired["home"] / ".ssh" / "id_ed25519"
    event = recorded_event(tool_input={"file_path": str(target)})
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, "", "")

    (request,) = mouth.requests
    assert request["path"] == "/events"
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["content-type"] == "application/json"
    assert json.loads(request["body"]) == {
        "harness": "claude-code",
        "tool_class": "file-read",
        "locality": "home",
        "subject_digest": _sha(str(target)),
        "result_digest": hook.result_digest(event["tool_response"]),
        "call_digest": _sha(event["tool_use_id"]),
    }


def test_a_proxy_in_the_environment_is_not_handed_the_token(wired, capsys, monkeypatch):
    """The mouth is on loopback; a proxy named in the shell must not see the bearer token."""
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], recorded_event(), capsys) == (0, "", "")
    assert len(mouth.requests) == 1


@pytest.mark.parametrize(
    ("arrange", "cause"),
    [
        pytest.param(lambda w, port: None, "no tool-event mouth address", id="no address file"),
        pytest.param(
            lambda w, port: w["listen"](f"127.0.0.1:{port}"),
            "could not be reached",
            id="nothing listening",
        ),
        pytest.param(
            lambda w, port: w["listen"]("example.com:80"),
            "not a loopback port",
            id="not loopback",
        ),
        pytest.param(lambda w, port: w["listen"]("garbage"), "not host:port", id="malformed"),
        pytest.param(
            lambda w, port: (
                w["listen"](f"127.0.0.1:{port}"),
                w["store"].gateway_token_path(w["project"]).unlink(),
            ),
            "no launch token",
            id="no token",
        ),
    ],
)
def test_every_failure_is_one_line_on_stderr_and_exit_0(wired, capsys, arrange, cause):
    with _Mouth() as mouth:
        port = mouth.port
    arrange(wired, port)
    status, out, err = _run(wired["argv"], recorded_event(), capsys)
    assert (status, out) == (0, "")
    assert err.count("\n") == 1 and err.startswith(hook.PREFIX), err
    assert cause in err
    # The subject is never named in a diagnostic.
    assert "outside-the-project" not in err


@pytest.mark.parametrize("status_code", [400, 401, 500])
def test_a_refusal_from_the_mouth_is_one_line_and_exit_0(wired, capsys, status_code):
    with _Mouth(status=status_code) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        status, out, err = _run(wired["argv"], recorded_event(), capsys)
    assert (status, out) == (0, "")
    assert err == f"{hook.PREFIX}the tool-event mouth answered {status_code}\n"


@pytest.mark.parametrize(
    ("argv", "stdin"),
    [
        pytest.param(None, "not json", id="input not JSON"),
        pytest.param(None, "[1, 2]", id="input not an object"),
        pytest.param(["--project"], None, id="flag with no value"),
        pytest.param(["--bogus", "x"], None, id="unknown flag"),
        pytest.param([], None, id="no project"),
    ],
)
def test_a_bad_invocation_is_one_line_and_exit_0(wired, capsys, argv, stdin):
    status, out, err = _run(
        wired["argv"] if argv is None else argv,
        stdin if stdin is not None else recorded_event(),
        capsys,
    )
    assert (status, out) == (0, "")
    assert err.count("\n") == 1 and err.startswith(hook.PREFIX), err


@pytest.mark.parametrize(
    "event",
    [
        recorded_event(tool_name=GATEWAY_TOOL, mcp_server=GATEWAY_SERVER),
        recorded_event(tool_name="ToolSearch"),
    ],
)
def test_an_ignored_event_posts_nothing_and_says_nothing(wired, capsys, event):
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, "", "")
    assert mouth.requests == []


def _without_event_name(event: dict) -> dict:
    del event["hook_event_name"]
    return event


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_without_event_name(pre_event()), id="no event name"),
        pytest.param(pre_event(hook_event_name="PreToolUSE"), id="misspelled"),
        pytest.param(pre_event(hook_event_name=7), id="not a string"),
        pytest.param(recorded_event(hook_event_name="SessionStart"), id="another event"),
        pytest.param(recorded_event(hook_event_name="PostToolUseFailure"), id="failure event"),
    ],
)
def test_an_unknown_hook_event_is_one_line_posts_nothing_and_exits_0(wired, capsys, event):
    """Claude Code always names its event, so an object that does not is not silent."""
    with _Mouth(body=_answer("refuse", text="must not be asked")) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (
            0,
            "",
            f"{hook.PREFIX}{hook.UNKNOWN_EVENT}\n",
        )
    assert mouth.requests == []


def test_an_event_that_is_neither_pre_nor_post_is_one_line_even_with_a_bad_command_line(capsys):
    event = recorded_event(hook_event_name="Stop")
    assert _run(["--bogus"], event, capsys) == (0, "", f"{hook.PREFIX}{hook.UNKNOWN_EVENT}\n")


@pytest.mark.parametrize(
    ("event", "status", "prefix"),
    [
        pytest.param(recorded_event(), 0, hook.PREFIX, id="PostToolUse"),
        pytest.param(pre_event(), hook.EXIT_NOT_DECIDED, hook.DECIDE_PREFIX, id="PreToolUse"),
    ],
)
def test_the_cli_routes_hook_past_argparse_and_passes_its_status_through(
    capsys, monkeypatch, event, status, prefix
):
    """argparse answers a bad command line with exit 2 whatever the event.

    For `PostToolUse` that would show the model stderr; for `PreToolUse` the
    hook chooses 2 itself, with its own line.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert main(["hook", "--nonsense"]) == status
    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("\n") == 1 and err.startswith(prefix), err


def test_a_redirect_is_not_followed_so_the_token_goes_nowhere_else(wired, capsys):
    with _Mouth() as elsewhere:
        target = f"http://127.0.0.1:{elsewhere.port}/events"
        with _Mouth(status=302, body=b"", Location=target) as mouth:
            wired["listen"](f"127.0.0.1:{mouth.port}")
            status, out, err = _run(wired["argv"], recorded_event(), capsys)
    assert (status, out) == (0, "")
    assert err == f"{hook.PREFIX}the tool-event mouth answered 302\n"
    assert len(mouth.requests) == 1
    assert elsewhere.requests == []


# ---------------------------------------------------------------------------
# PreToolUse: the request, and the answer relayed
# ---------------------------------------------------------------------------


def _answer(answer: str, text=None, intent_id=None, reason="r") -> bytes:
    return json.dumps(
        {"answer": answer, "reason": reason, "intent_id": intent_id, "text": text}
    ).encode("utf-8")


PERMIT = _answer("permit")
REFUSE = _answer("refuse", text=REFUSED_TEXT, reason="no manifest entry")


def test_the_allow_and_deny_lines_are_the_harness_shapes_and_one_line_each():
    assert json.loads(hook.allow_output()) == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
    }
    assert json.loads(hook.deny_output(REFUSED_TEXT)) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REFUSED_TEXT,
        }
    }
    assert "\n" not in hook.allow_output() + hook.deny_output(REFUSED_TEXT)


def test_a_decision_request_is_six_keys_with_the_right_digests(wired, capsys):
    """G31: six leaves, each recomputed here from the event, and nothing from the event."""
    event = pre_event()
    with _Mouth(body=PERMIT) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, hook.allow_output() + "\n", "")

    (request,) = mouth.requests
    assert request["path"] == "/decide"
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["content-type"] == "application/json"
    canonical_input = json.dumps(
        event["tool_input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert json.loads(request["body"]) == {
        "harness": "claude-code",
        "tool_class": "shell",
        "locality": "unknown",
        "subject_digest": _sha(event["tool_input"]["command"]),
        "input_digest": _sha(canonical_input),
        "call_digest": _sha(event["tool_use_id"]),
    }
    for leaked in (
        event["tool_input"]["command"], event["tool_use_id"], event["session_id"], event["cwd"],
        "outside-the-project",
    ):
        assert leaked.encode("utf-8") not in request["body"]
    assert len(request["body"]) < 4096


def test_the_request_and_the_report_for_one_call_carry_the_same_call_digest(places):
    pre = pre_event(tool_use_id="toolu_pair")
    post = recorded_event(tool_use_id="toolu_pair")
    request = hook.request_of(pre, project=places["project"], home=places["home"])
    report = hook.report_of(post, project=places["project"], home=places["home"])
    assert request["call_digest"] == report["call_digest"] == _sha("toolu_pair")


def test_a_missing_tool_input_is_digested_as_null(places):
    event = pre_event()
    del event["tool_input"]
    request = hook.request_of(event, project=places["project"], home=places["home"])
    assert request["input_digest"] == _sha("null")
    assert request["subject_digest"] == _sha("")


def test_an_mcp_tool_the_gateway_does_not_serve_is_asked_about_as_unbrokered(wired, capsys):
    event = pre_event(tool_name="mcp__plugin_notes_vault__read_note", tool_input={"id": 1})
    with _Mouth(body=PERMIT) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys)[0] == 0
    body = json.loads(mouth.requests[0]["body"])
    assert (body["harness"], body["tool_class"], body["locality"]) == (
        "claude-code-mcp",
        "other",
        "unknown",
    )
    assert body["subject_digest"] == _sha(event["tool_name"])


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(REFUSE, id="refuse"),
        pytest.param(
            _answer("refuse", text=REFUSED_TEXT, intent_id="intent-1", reason="held"), id="hold"
        ),
    ],
)
def test_a_refuse_relays_the_brokers_text_verbatim_and_nothing_else(wired, capsys, answer):
    with _Mouth(body=answer) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        status, out, err = _run(wired["argv"], pre_event(), capsys)
    assert (status, err) == (0, "")
    assert out == hook.deny_output(REFUSED_TEXT) + "\n"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == json.loads(answer)["text"]


def test_a_permit_prints_allow_whatever_else_the_answer_carries(wired, capsys):
    answer = _answer("permit", text="ignored", reason="granted")
    with _Mouth(body=answer) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], pre_event(), capsys) == (0, hook.allow_output() + "\n", "")


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            pre_event(tool_name=GATEWAY_TOOL, mcp_server=GATEWAY_SERVER),
            id="gateway tool, server named",
        ),
        pytest.param(
            pre_event(tool_name=GATEWAY_TOOL, mcp_server=GATEWAY_SERVER, tool_use_id=None),
            id="gateway tool, no call id",
        ),
    ],
)
def test_a_tool_the_gateway_serves_is_allowed_without_asking(wired, capsys, event):
    """The broker decides that call at the gateway; a second decision here would be a
    second decider. Nothing is posted, so no address is needed either."""
    with _Mouth(body=_answer("refuse", text="must not be asked")) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, hook.allow_output() + "\n", "")
    assert mouth.requests == []


@pytest.mark.parametrize("server", MALFORMED_SERVERS)
def test_a_gateway_name_without_the_gateway_server_is_asked_about(wired, capsys, server):
    """The prefix alone never earns the routing `allow`: the broker is asked, as for
    any MCP tool the gateway does not serve."""
    event = pre_event(tool_name=GATEWAY_TOOL)
    event.update(server)
    with _Mouth(body=REFUSE) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, hook.deny_output(REFUSED_TEXT) + "\n", "")
    (request,) = mouth.requests
    body = json.loads(request["body"])
    assert (body["harness"], body["tool_class"], body["locality"]) == (
        "claude-code-mcp",
        "other",
        "unknown",
    )


def test_a_server_wearing_the_gateway_prefix_is_asked_about(wired, capsys):
    event = pre_event(tool_name="mcp__tegh__x__get", mcp_server={"name": "tegh__x"})
    with _Mouth(body=REFUSE) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys)[1] == hook.deny_output(REFUSED_TEXT) + "\n"
    assert len(mouth.requests) == 1


def test_a_harness_internal_tool_prints_nothing_and_asks_nothing(wired, capsys):
    with _Mouth(body=REFUSE) as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], pre_event(tool_name="ToolSearch"), capsys) == (0, "", "")
    assert mouth.requests == []


def _listening(answer: bytes, status: int = 200, **headers):
    def arrange(w, mouth):
        mouth.body, mouth.status, mouth.headers = answer, status, headers
        w["listen"](f"127.0.0.1:{mouth.port}")

    return arrange


def _closed(address: str):
    def arrange(w, mouth):
        w["listen"](address.format(port=mouth.closed_port))

    return arrange


@pytest.mark.parametrize(
    ("arrange", "event", "cause"),
    [
        pytest.param(lambda w, m: None, pre_event(), "no tool-event mouth address", id="no address"),
        pytest.param(
            _closed("127.0.0.1:{port}"), pre_event(), "could not be reached", id="nothing listening"
        ),
        pytest.param(
            _closed("example.com:80"), pre_event(), "not a loopback port", id="not loopback"
        ),
        pytest.param(_closed("garbage"), pre_event(), "not host:port", id="malformed address"),
        pytest.param(
            lambda w, m: (
                _listening(PERMIT)(w, m),
                w["store"].gateway_token_path(w["project"]).unlink(),
            ),
            pre_event(),
            "no launch token",
            id="no token",
        ),
        pytest.param(_listening(b"", 401), pre_event(), "answered 401", id="401"),
        pytest.param(_listening(b"", 400), pre_event(), "answered 400", id="400"),
        pytest.param(_listening(b"", 500), pre_event(), "answered 500", id="500"),
        pytest.param(_listening(PERMIT, 202), pre_event(), "answered 202", id="202"),
        pytest.param(
            _listening(b"", 302, Location="http://127.0.0.1:9/decide"),
            pre_event(),
            "answered 302",
            id="redirect",
        ),
        pytest.param(_listening(b"permit"), pre_event(), "not JSON", id="body not JSON"),
        pytest.param(_listening(b'["permit"]'), pre_event(), "not a JSON object", id="not object"),
        pytest.param(
            _listening(_answer("allow")), pre_event(), "neither permit nor refuse", id="other word"
        ),
        pytest.param(
            _listening(b'{"reason":"r"}'), pre_event(), "neither permit nor refuse", id="no answer"
        ),
        pytest.param(
            _listening(_answer("refuse", text=None)), pre_event(), "no text", id="refuse, null text"
        ),
        pytest.param(
            _listening(_answer("refuse", text=7)), pre_event(), "no text", id="refuse, text a number"
        ),
        pytest.param(
            _listening(PERMIT), pre_event(tool_use_id=None), "no tool_use_id", id="no call id"
        ),
        pytest.param(_listening(PERMIT), pre_event(tool_name=None), "names no tool", id="no tool"),
    ],
)
def test_every_pre_failure_is_one_line_on_stderr_nothing_on_stdout_and_exit_2(
    wired, capsys, arrange, event, cause
):
    """Exit 2 blocks the call whatever stdout holds (reference §3, "Failure semantics")."""
    with _Mouth() as closed:
        closed_port = closed.port
    with _Mouth() as mouth:
        mouth.closed_port = closed_port
        arrange(wired, mouth)
        status, out, err = _run(wired["argv"], event, capsys)
    assert (status, out) == (hook.EXIT_NOT_DECIDED, "")
    assert err.count("\n") == 1 and err.startswith(hook.DECIDE_PREFIX), err
    assert cause in err
    assert "outside-the-project" not in err


def test_a_decision_that_does_not_come_in_time_is_exit_2(wired, capsys, monkeypatch):
    monkeypatch.setattr(hook, "DECIDE_TIMEOUT_SECONDS", 0.2)
    with _Mouth(body=PERMIT) as mouth:
        mouth.delay = 1.0
        wired["listen"](f"127.0.0.1:{mouth.port}")
        status, out, err = _run(wired["argv"], pre_event(), capsys)
    assert (status, out) == (hook.EXIT_NOT_DECIDED, "")
    assert err == f"{hook.DECIDE_PREFIX}the tool-event mouth did not answer in time\n"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--project"], id="flag with no value"),
        pytest.param(["--bogus", "x"], id="unknown flag"),
        pytest.param([], id="no project"),
    ],
)
def test_a_bad_command_line_on_a_pre_event_is_exit_2(capsys, argv):
    status, out, err = _run(argv, pre_event(), capsys)
    assert (status, out) == (hook.EXIT_NOT_DECIDED, "")
    assert err.count("\n") == 1 and err.startswith(hook.DECIDE_PREFIX), err


def test_an_unexpected_error_on_a_pre_event_is_exit_2(wired, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "locality", boom)
    status, out, err = _run(wired["argv"], pre_event(), capsys)
    assert (status, out, err) == (
        hook.EXIT_NOT_DECIDED,
        "",
        f"{hook.DECIDE_PREFIX}RuntimeError\n",
    )


def test_an_unexpected_error_on_a_post_event_is_still_exit_0(wired, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hook, "locality", boom)
    assert _run(wired["argv"], recorded_event(), capsys) == (0, "", f"{hook.PREFIX}RuntimeError\n")


def test_tegh_hook_loads_nothing_from_the_platform():
    """It runs once per built-in call: no broker runtime, no schemas, no store."""
    probe = (
        "import sys, tegh.hook; "
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'safe_agents'); "
        "print(loaded)"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"


#: Top-level packages the hook's own run must not import. `tegh.cli` imports
#: every one of them through the base's schemas.
_HEAVY_PACKAGES = frozenset({"safe_agents", "pydantic", "yaml", "cryptography"})


def _imported(importtime_stderr: str) -> set[str]:
    """The top-level package of every module `-X importtime` says was imported."""
    return {
        line.rsplit("|", 1)[-1].strip().split(".")[0]
        for line in importtime_stderr.splitlines()
        if line.startswith("import time:") and "|" in line
    }


def test_the_console_script_runs_the_hook_without_loading_the_platform(tmp_path):
    """The command a wrap writes runs `tegh.entry`, and `tegh hook` there imports no base.

    The console script and the launcher form both name `tegh.entry`, so this
    runs that module the way the launcher does, on a `PreToolUse` event with no
    gateway, under `-X importtime`. Before `tegh.entry` the console script was
    `tegh.cli:main`, whose imports load 21 `safe_agents` modules on every call.
    """
    import tomllib

    scripts = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert scripts["project"]["scripts"]["tegh"] == "tegh.entry:main"
    project, home = tmp_path / "project", tmp_path / "tegh"
    project.mkdir()
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable, "-X", "importtime", "-m", "tegh.entry",
            "hook", "--project", str(project), "--home", str(home),
        ],
        input=json.dumps(pre_event()),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == hook.EXIT_NOT_DECIDED, done.stderr[-2000:]
    assert done.stdout == ""
    imported = _imported(done.stderr)
    assert "tegh" in imported, "the importtime trace was not read"
    assert not imported & _HEAVY_PACKAGES, sorted(imported & _HEAVY_PACKAGES)


def test_the_entry_runs_every_other_subcommand_through_the_cli(capsys):
    """Off the hook path `tegh.entry` is `tegh.cli`'s `main`, status included."""
    from tegh import entry

    with pytest.raises(SystemExit) as exited:
        entry.main(["--version-of-nothing"])
    assert exited.value.code == 2
    assert "usage: tegh" in capsys.readouterr().err


def test_mouth_is_listening_answers_from_a_connect(wired):
    path = wired["store"].event_mouth_addr_path(wired["project"])
    assert hook.mouth_is_listening(path) is False
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert hook.mouth_is_listening(path) is True
    assert hook.mouth_is_listening(path) is False
