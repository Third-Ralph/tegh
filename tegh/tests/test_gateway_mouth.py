"""What `tegh gateway` hands the broker for the tool-event mouth, and who owns its address.

The gateway is never started here: `os.execve` is replaced by a recorder, so a
test sees the exact environment the broker would run under. The mouth itself,
running, is `test_hook_e2e.py`.
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from tegh.cli import main
from tegh.store import TeghStore, provision

_MOUTH_VARS = ("BROKER_EVENT_MOUTH_PORT", "BROKER_EVENT_MOUTH_ADDR_FILE")
#: RFC 6750 b64token, as the base checks it.
_B64TOKEN = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~+/")


@pytest.fixture
def ready(tmp_path: Path) -> dict:
    """A provisioned home and a project it has a manifest and a layout marker for."""
    store = provision(tmp_path / "tegh")
    project = tmp_path / "widget"
    project.mkdir()
    manifest = store.manifest_path(project)
    manifest.parent.mkdir(parents=True)
    manifest.write_text("principal: {}\n", encoding="utf-8")
    store.mark_layout(project)
    return {"store": store, "project": project.resolve()}


def test_the_gateway_environment_opens_the_mouth_on_a_token_by_file(ready):
    store: TeghStore = ready["store"]
    env = store.gateway_env(project=ready["project"])
    token_path = store.gateway_token_path(ready["project"])
    assert env["BROKER_GATEWAY_AUTH"] == "launch_token"
    assert env["BROKER_GATEWAY_TOKEN_FILE"] == str(token_path)
    assert env["BROKER_EVENT_MOUTH_PORT"] == "0"
    assert env["BROKER_EVENT_MOUTH_ADDR_FILE"] == str(
        store.event_mouth_addr_path(ready["project"])
    )
    # No host is named: the base binds loopback by default.
    assert "BROKER_EVENT_MOUTH_HOST" not in env
    # The token reaches the broker by file, never as a value in its environment.
    token = token_path.read_text(encoding="ascii").removesuffix("\n")
    assert token not in env.values()
    assert len(token) >= 32 and set(token) <= _B64TOKEN
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_the_token_is_made_once_and_never_replaced(ready):
    store: TeghStore = ready["store"]
    store.gateway_env(project=ready["project"])
    first = store.gateway_token_path(ready["project"]).read_bytes()
    store.gateway_env(project=ready["project"])
    assert store.gateway_token_path(ready["project"]).read_bytes() == first


def test_without_the_mouth_the_port_and_address_are_not_named(ready):
    env = ready["store"].gateway_env(project=ready["project"], event_mouth=False)
    assert not set(_MOUTH_VARS) & set(env)


@pytest.fixture
def execed(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def record(path, argv, env):
        seen.append({"argv": list(argv), "env": dict(env)})
        return 0

    monkeypatch.setattr(os, "execve", record)
    return seen


def _gateway(ready) -> None:
    main(["gateway", "--project", str(ready["project"]), "--home", str(ready["store"].home)])


def test_an_address_nothing_answers_at_is_removed_and_this_gateway_takes_the_mouth(
    ready, execed
):
    addr = ready["store"].event_mouth_addr_path(ready["project"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]  # bound, never listening: a refused connect
        addr.write_text(f"127.0.0.1:{dead}\n", encoding="ascii")
        _gateway(ready)
    assert not addr.exists()
    (spawn,) = execed
    assert spawn["env"]["BROKER_EVENT_MOUTH_ADDR_FILE"] == str(addr)


def test_a_live_mouth_keeps_its_address_and_this_gateway_opens_none(ready, execed, capsys):
    """`tegh call`, or a harness listing tools, must not take the session's mouth away."""
    addr = ready["store"].event_mouth_addr_path(ready["project"])
    with socket.socket() as live:
        live.bind(("127.0.0.1", 0))
        live.listen()
        addr.write_text(f"127.0.0.1:{live.getsockname()[1]}\n", encoding="ascii")
        before = addr.read_bytes()
        _gateway(ready)
    assert addr.read_bytes() == before
    (spawn,) = execed
    assert not set(_MOUTH_VARS) & set(spawn["env"])
    assert spawn["env"]["BROKER_GATEWAY_AUTH"] == "launch_token"
    assert "holds the tool-event mouth" in capsys.readouterr().err
