"""`tegh hook`, without a gateway: what it classifies, what it sends, and that it never gates.

Nothing here starts a broker. A report's body is caught by a stdlib HTTP server
on a thread, standing where the gateway's tool-event mouth would, so a test can
say exactly which bytes left the process. The real mouth, the real tape and the
taint it causes are `test_hook_e2e.py`.

The event fixture under `events/` is built from the field list in
`docs/references/harnesses/claude-code.md` §3 ("Hook input"). It was not
captured from a running Claude Code, and the shape of a `Read` tool response is
undocumented there, so the response in it is illustrative.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tegh import hook
from tegh.cli import main
from tegh.store import TeghStore

EVENT_FIXTURE = Path(__file__).with_name("events") / "post_tool_use_read.json"
TOKEN = "t" * 64

#: A tool the wrapped gateway serves, as Claude Code names it.
GATEWAY_TOOL = "mcp__tegh__ledger__get_entry"


def recorded_event(**changes) -> dict:
    event = json.loads(EVENT_FIXTURE.read_text(encoding="utf-8"))
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
        pytest.param(recorded_event(tool_name=GATEWAY_TOOL), id="gateway tool"),
        pytest.param(
            recorded_event(tool_name=GATEWAY_TOOL, mcp_server={"name": "tegh", "source": "local"}),
            id="gateway tool, server named",
        ),
        pytest.param(recorded_event(tool_name="ToolSearch"), id="harness-internal tool"),
        pytest.param(recorded_event(tool_name=None), id="no tool name"),
    ],
)
def test_events_that_are_not_reported(places, event):
    assert hook.report_of(event, project=places["project"], home=places["home"]) is None


def test_the_gateway_prefix_is_the_name_a_wrap_gives_the_gateway():
    """The skip rule is a rule about one server name, so it must be the wrap's."""
    from tegh import interpose

    assert hook.GATEWAY_SERVER_NAME == interpose._UNRECORDED_GATEWAY_NAME
    assert hook.GATEWAY_SERVER_NAME == interpose.InterposePlan.__dataclass_fields__[
        "gateway_name"
    ].default
    assert hook.GATEWAY_TOOL_PREFIX == "mcp__tegh__"


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


def test_a_report_is_five_leaves_and_nothing_from_the_event(places):
    """No path, URL, command, content or session id is in what leaves."""
    event = recorded_event()
    report = hook.report_of(event, project=places["project"], home=places["home"])
    assert set(report) == {"harness", "tool_class", "locality", "subject_digest", "result_digest"}
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
    assert set(report) == {"harness", "tool_class", "locality", "subject_digest"}


# ---------------------------------------------------------------------------
# Delivery, against a stand-in for the mouth
# ---------------------------------------------------------------------------


class _Mouth:
    """A loopback HTTP server that records what it was sent and answers `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
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
                body = b'{"source":null}'
                self.send_response(mouth.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

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
        recorded_event(hook_event_name="PreToolUse"),
        recorded_event(tool_name=GATEWAY_TOOL),
        recorded_event(tool_name="ToolSearch"),
    ],
)
def test_an_ignored_event_posts_nothing_and_says_nothing(wired, capsys, event):
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert _run(wired["argv"], event, capsys) == (0, "", "")
    assert mouth.requests == []


def test_the_cli_routes_hook_past_argparse_so_a_bad_command_line_still_exits_0(capsys):
    """argparse answers a bad command line with exit 2, which the harness shows the model."""
    assert main(["hook", "--nonsense"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(hook.PREFIX)


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


def test_mouth_is_listening_answers_from_a_connect(wired):
    path = wired["store"].event_mouth_addr_path(wired["project"])
    assert hook.mouth_is_listening(path) is False
    with _Mouth() as mouth:
        wired["listen"](f"127.0.0.1:{mouth.port}")
        assert hook.mouth_is_listening(path) is True
    assert hook.mouth_is_listening(path) is False
