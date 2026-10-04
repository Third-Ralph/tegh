"""`tegh init` -> `wrap` -> `gateway` -> `unwrap`, for real.

Every other tegh test drives one seam. This drives the whole product claim: a
project with a real MCP server in its config comes out with the gateway as its
only server, an MCP client talking to that gateway sees the admitted tool and
gets a live answer through the broker's per-call path, and `unwrap` puts the
config back byte-for-byte.

Real on every axis except the harness home, which is a `tmp_path` tree so the
suite never touches the developer's own `~/.claude.json` — that is what
`--harness-home` is for. The ceremony, the sqlite store, the signed ledger, the
spawned gateway and the spawned toy MCP server are all genuine.

Needs the optional `mcp` extra (the toy server and the client both use the SDK).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("mcp", reason="the end-to-end wrap drives a real MCP client and server")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from tegh import configvalues, interpose  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.store import TeghStore  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    REPO,
    give_ledger as _give_ledger,
    minimal_child_env,
    scripted,
    store_of as _store,
    wrap_admit_all,
    wrap_answering as _wrap_answering,
)

_CALL_TIMEOUT = datetime.timedelta(seconds=120)


def test_wrap_interposes_the_gateway_and_unwrap_restores_it(harness, capsys) -> None:
    before = harness["claude_json"].read_bytes()

    assert main(["init"]) == 0
    capsys.readouterr()
    assert wrap_admit_all(harness) == 0
    out = capsys.readouterr().out
    assert "INTERPOSED" in out, out

    # --- the harness now loads the gateway, and nothing else -----------------
    document = json.loads(harness["claude_json"].read_text())
    servers = document["projects"][str(harness["project"])]["mcpServers"]
    assert list(servers) == ["tegh"]
    # The launcher prefix varies (console script vs. `-m`); the command does not.
    # Both resolution inputs are on the line: a spawned stdio child gets a
    # minimal environment, so anything inherited is anything missing.
    assert servers["tegh"]["args"][-5:] == [
        "gateway",
        "--project", str(harness["project"]),
        "--home", str(harness["tegh_home"]),
    ]
    # The config the wrapped agent can read carries no credential material.
    assert set(servers["tegh"]) == {"command", "args"}
    # Live state is untouched by the rewrite.
    assert document["projects"][str(harness["project"])]["lastSessionId"] == "before-the-wrap"

    # --- the lock is a projection; the authority is outside the project ------
    assert (harness["project"] / "tegh.lock").exists()
    assert (harness["tegh_home"] / "tegh.db").exists()

    # --- unwrap restores byte-for-byte ---------------------------------------
    assert main(["unwrap", "--yes", "--project", str(harness["project"])]) == 0
    assert harness["claude_json"].read_bytes() == before


def test_the_wrapped_gateway_serves_the_admitted_tool(harness, capsys) -> None:
    """The DoD's other half: the pointer written into the config actually works.

    Spawning it exactly as the harness would — the `command` and `args` read
    back out of the rewritten config, not a hand-built invocation — because the
    thing worth proving is that what tegh WROTE is launchable, and `claude mcp
    add` does not validate its target (claude-code.md §7).
    """
    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    capsys.readouterr()

    servers = json.loads(harness["claude_json"].read_text())["projects"][
        str(harness["project"])
    ]["mcpServers"]
    entry = servers["tegh"]

    async def _drive() -> dict:
        params = StdioServerParameters(
            command=entry["command"], args=entry["args"], env=minimal_child_env()
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_CALL_TIMEOUT) as client:
                await client.initialize()
                listed = await client.list_tools()
                allowed = await client.call_tool("ledger__get_entry", {"entry_id": "L-001"})
                refused = await client.call_tool("ledger__delete_entry", {"entry_id": "L-001"})
                return {
                    "names": sorted(t.name for t in listed.tools),
                    "allowed": allowed,
                    "refused": refused,
                }

    observed = asyncio.run(_drive())

    # Only the admitted coordinates are advertised, in wire form.
    assert observed["names"] == ["ledger__get_entry", "ledger__list_entries"]

    # --- `--admit-all` accepts the PROPOSAL, so this is held ----------------
    # The toy advertises no annotations, so the proposal is the restrictive end
    # (`effect: write`, `reversible: false`); `seed` issues the grant at on-loop;
    # and the PDP's "irreversible external write" rule holds it. Every layer is
    # right on its own terms, and this is what `--admit-all` MEANS — it admits
    # what was proposed without a human in the loop, so it inherits whatever the
    # server did or did not advertise.
    #
    # A human who corrects the classification gets an executing read; that is
    # the next test, and it is the correction fix. Both are pinned because the gap
    # between them is the whole reason `--admit-all` is not the golden path.
    assert observed["allowed"].isError is True
    assert "held for approval" in observed["allowed"].content[0].text
    assert "has NOT executed" in observed["allowed"].content[0].text

    # A tool the toy does not even have is refused by the broker, not the mouth.
    assert observed["refused"].isError is True

    # Both outcomes are on this project's own tape — the hold IS a record.
    audit = harness["tegh_home"] / "projects"
    tapes = list(audit.glob("*/audit.jsonl"))
    assert tapes, "the gateway wrote no audit tape"
    records = [json.loads(line) for line in tapes[0].read_text().splitlines() if line.strip()]
    assert any(
        r["op"] == "get_entry" and r["decision"] == "require_approval" and r["outcome"] == "held"
        for r in records
    ), records


def test_a_wrap_that_admits_nothing_leaves_the_config_alone(harness, capsys) -> None:
    """A gateway serving no tools would remove the user's servers and give
    nothing back — strictly worse than not wrapping.

    Driven through `wrap_command` rather than `main` so the review prompt can be
    answered "no"; `main` has no seam for it, and a test that let the prompt
    reach a closed stdin would be testing pytest, not the wrap.
    """
    before = harness["claude_json"].read_bytes()
    assert main(["init"]) == 0
    rc = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
            ]
        ),
        prompt=lambda _: "n",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "left alone" in out, out
    assert harness["claude_json"].read_bytes() == before


def test_no_rewrite_admits_without_interposing(harness, capsys) -> None:
    before = harness["claude_json"].read_bytes()
    assert main(["init"]) == 0
    assert _wrap_no_rewrite(harness) == 0
    out = capsys.readouterr().out
    assert "--no-rewrite" in out
    assert harness["claude_json"].read_bytes() == before
    assert (harness["project"] / "tegh.lock").exists()


def _wrap_no_rewrite(harness: dict) -> int:
    return main(
        [
            "wrap", "claude",
            "--project", str(harness["project"]),
            "--harness-home", str(harness["home"]),
            "--admit-all",
            "--no-rewrite",
        ]
    )


def test_the_posture_rung_moves_only_while_the_project_is_wrapped(harness, capsys) -> None:
    """The posture is a claim about THIS configuration, so it must be read from it.

    Asserted in both directions and across the transition, because a posture that
    only ever went up would be indistinguishable from one that was hard-coded —
    which is what it was before the wrap could interpose, honestly, and would now be a lie.
    """
    assert main(["init"]) == 0
    capsys.readouterr()
    posture_argv = [
        "posture", "--project", str(harness["project"]),
        "--harness-home", str(harness["home"]), "--json",
    ]

    assert main(posture_argv) == 0
    before = json.loads(capsys.readouterr().out)
    assert before["posture"] == "pre-1"

    assert wrap_admit_all(harness) == 0
    capsys.readouterr()

    assert main(posture_argv) == 0
    during = json.loads(capsys.readouterr().out)
    assert during["posture"] == "1"
    assert any(
        "routed through tegh" in line["claim"] and line["holds"] == "yes"
        for line in during["gaps"]
    ), during["gaps"]

    assert main(["unwrap", "--yes", "--project", str(harness["project"])]) == 0
    capsys.readouterr()

    assert main(posture_argv) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["posture"] == "pre-1"


def test_a_corrected_classification_produces_an_executing_read(harness, capsys) -> None:
    """The correction fix, end to end: correct the classification, get a real answer.

    The toy's two tools are plainly reads and advertise nothing. Before the
    review could CORRECT a proposal, the only outcomes were "admit as an
    irreversible write" (held forever) or "skip" (not wrapped at all), so a
    wrapped agent could execute nothing. Here a human says `read`, and the call
    executes through the broker's full per-call path.
    """
    assert main(["init"]) == 0
    capsys.readouterr()

    # Per tool: edit -> effect=read -> keep egress_arg -> admit.
    script = ["e", "read", "", "y"] * 2
    rc = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
            ]
        ),
        prompt=scripted(script),
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "YOU SET" in out, "the corrected field must render as operator-set"

    servers = json.loads(harness["claude_json"].read_text())["projects"][
        str(harness["project"])
    ]["mcpServers"]
    entry = servers["tegh"]

    async def _drive():
        params = StdioServerParameters(
            command=entry["command"], args=entry["args"], env=minimal_child_env()
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_CALL_TIMEOUT) as client:
                await client.initialize()
                listed = await client.list_tools()
                result = await client.call_tool("ledger__get_entry", {"entry_id": "L-001"})
                return listed, result

    listed, result = asyncio.run(_drive())

    # The gateway now describes them as reads, from the CORRECTED classification.
    descriptions = {t.name: t.description for t in listed.tools}
    assert descriptions["ledger__get_entry"].startswith(
        "ledger.get_entry — brokered read, external"
    )

    # And the call EXECUTES. This is the line that was unreachable before a review could correct a proposal.
    assert result.isError is False, result
    assert "opening balance" in result.content[0].text

    tapes = list((harness["tegh_home"] / "projects").glob("*/audit.jsonl"))
    records = [json.loads(line) for line in tapes[0].read_text().splitlines() if line.strip()]
    assert any(
        r["op"] == "get_entry" and r["decision"] == "allow" and r["outcome"] == "executed"
        for r in records
    ), records


def test_wrap_warns_that_irreversible_writes_are_held_on_every_call(harness, capsys) -> None:
    """Told at wrap time, not discovered by calling one and waiting.

    `--admit-all` takes the proposal as-is, so an unannotated server's tools are
    admitted as irreversible writes and the broker holds every call to them. The
    wrap is where the human is present and the classification is still cheap to
    change, so that is where it is said.

    With `tegh approve` the honest statement is "held until you release it", not "will
    never execute" — a laptop now has a release path, and a warning that still
    said otherwise would send users back to the loose classification the review
    exists to prevent.
    """
    assert main(["init"]) == 0
    capsys.readouterr()
    assert wrap_admit_all(harness) == 0
    out = capsys.readouterr().out

    assert "IRREVERSIBLE WRITES" in out
    assert "get_entry" in out and "list_entries" in out
    assert "tegh approve" in out
    assert "once per call, not once for the tool" in out
    # The claim from before `tegh approve` must not survive: it is now false.
    assert "will NOT execute" not in out


def test_no_warning_when_the_classifications_were_corrected(harness, capsys) -> None:
    """The warning must be a signal, not decoration — silent when it does not apply."""
    assert main(["init"]) == 0
    capsys.readouterr()
    rc = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(harness["project"]),
                "--harness-home", str(harness["home"]),
            ]
        ),
        prompt=scripted(["e", "read", "", "y"] * 2),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "IRREVERSIBLE WRITES" not in out


# ---------------------------------------------------------------------------
# A literal config value is a credential only if a human says so
# ---------------------------------------------------------------------------


_MEMORY_COORDINATE = "local:ledger.env.MEMORY_FILE_PATH"
_HEADER_COORDINATE = "local:ledger.headers.X-Widget-Trace"


def _stranded_coordinates(out: str) -> list[str]:
    """The coordinates the wrap reported as classified-but-undelivered."""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if "NOT delivered to the wrapped" not in line:
            continue
        listed: list[str] = []
        for text in lines[index + 2:]:  # the sentence continues on the next line
            if not text.startswith("       "):
                break
            listed.append(text.strip())
        return listed
    return []


def test_a_value_called_configuration_reaches_the_server_tegh_spawns(
    harness, tmp_path, capsys
) -> None:
    """The wrap every real project needed and none could get.

    Before this, ANY literal value refused the wrap, and on a real machine
    almost every literal value is a path — six candidate projects, six
    `MEMORY_FILE_PATH`s, zero wrappable. Answering "configuration" has to do
    both halves or it is worse than the refusal: clear the gate AND carry the
    value into the manifest. A wrap that reported success while spawning the
    memory server without its file would silently point months of notes at a
    different file and call that a secured configuration.
    """
    memory_file = tmp_path / "claude-memory.json"
    memory_file.write_text("{}", encoding="utf-8")
    before = _give_ledger(harness, env={"MEMORY_FILE_PATH": str(memory_file)})

    assert main(["init"]) == 0
    capsys.readouterr()
    assert _wrap_answering(harness, scripted(["n"])) == 0
    out = capsys.readouterr().out

    # The decision was asked about a named coordinate, and the value tegh was
    # deciding about never reached the terminal.
    assert f"-> configuration  {_MEMORY_COORDINATE}" in out, out
    assert str(memory_file) not in out, "the value itself must never be printed"

    # It arrives where the base documents the static half of a child environment.
    store = _store(harness)
    # Both artifacts the refusal test asserts the ABSENCE of are present here,
    # which is what keeps that test from passing vacuously.
    assert store.snapshot_path(harness["project"], "ledger").exists()
    manifest = yaml.safe_load(
        store.manifest_path(harness["project"]).read_text(encoding="utf-8")
    )
    assert manifest["mcp_servers"]["ledger"]["env"] == {
        "MEMORY_FILE_PATH": str(memory_file)
    }

    # And carrying it costs the restore nothing: unwrap is still byte-for-byte.
    assert main(["unwrap", "--yes", "--project", str(harness["project"])]) == 0
    assert harness["claude_json"].read_bytes() == before


_SECRET = "not-a-real-credential-ledger-fixture"


def test_a_credential_is_MOVED_out_of_the_harness_config(harness, capsys) -> None:
    """The whole point of relocation, proven by the config's ABSENCE.

    The exit predicate for this phase is deliberately negative — "the key is
    absent from the harness config, verified by reading that config back, not
    by asserting it". Checking only that tegh's store holds the value would
    prove a COPY, which is what the wrap contract calls redecorating.

    So the load-bearing assertion here is the `not in` over the config's raw
    bytes. Everything after it describes where the value went instead.
    """
    _give_ledger(harness, env={"LEDGER_API_KEY": _SECRET})

    assert main(["init"]) == 0
    capsys.readouterr()
    # The empty answer means CREDENTIAL — still the safe default, but the safe
    # outcome is now a relocation rather than a refusal.
    assert _wrap_answering(harness, scripted([""])) == 0

    project = harness["project"]
    store = _store(harness)

    # 1. THE PREDICATE. Read the config back off disk; the secret is not in it.
    after = harness["claude_json"].read_text(encoding="utf-8")
    assert _SECRET not in after, "the credential is still in the harness config"
    assert "LEDGER_API_KEY" not in after

    # 2. It is in tegh's per-project store, under a BARE LEAF — the
    #    directory supplies the scope, so nothing qualifies the name.
    secrets_path = store.secrets_path(project)
    assert secrets_path == store.project_dir(project) / "secrets.json"
    stored = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert json.loads(stored["ledger"]) == {"LEDGER_API_KEY": _SECRET}
    assert oct(secrets_path.stat().st_mode)[-3:] == "600"

    # 3. The manifest delivers it at spawn through the sanctioned seam.
    manifest = yaml.safe_load(store.manifest_path(project).read_text(encoding="utf-8"))
    assert manifest["connector_auth"]["ledger"] == {
        "strategy": "static_secret",
        "env_map": {"LEDGER_API_KEY": "LEDGER_API_KEY"},
    }
    assert manifest["connector_secrets"]["ledger"] == "ledger"
    # The static half must NOT also carry it: the base refuses an overlap
    # between `env` and the env_map-delivered half at manifest load.
    assert "LEDGER_API_KEY" not in manifest["mcp_servers"]["ledger"].get("env", {})

    # 4. The BACKUP holds a reference, not the value. Interposition empties the
    #    harness block, so a backup carrying the literal would leave the
    #    credential in exactly the "second plaintext copy" the refusal used to
    #    name — a third storage location instead of a boundary.
    backup_raw = store.backup_path(project).read_text(encoding="utf-8")
    assert _SECRET not in backup_raw, "the backup kept a plaintext copy"
    backup = json.loads(backup_raw)
    entry = backup["sites"][0]["block"]["ledger"]["env"]["LEDGER_API_KEY"]
    assert entry == {
        interpose.SECRET_REFERENCE_KEY: {"leaf": "ledger", "field": "LEDGER_API_KEY"}
    }

    # 5. Still no CREDENTIAL decision on disk. Relocation did not create a
    #    reason to fingerprint a secret: the value is gone from the config, so
    #    the next wrap finds nothing at that coordinate to re-ask about.
    record = configvalues.load_record(store.config_decisions_path(project))
    assert record.decisions == {}


def test_unwrap_puts_a_relocated_credential_back(harness, capsys) -> None:
    """The round-trip has to survive relocation, or the wrap is not reversible."""
    before = _give_ledger(harness, env={"LEDGER_API_KEY": _SECRET})

    assert main(["init"]) == 0
    capsys.readouterr()
    assert _wrap_answering(harness, scripted([""])) == 0
    assert _SECRET not in harness["claude_json"].read_text(encoding="utf-8")

    assert main(["unwrap", "--yes", "--project", str(harness["project"])]) == 0
    assert harness["claude_json"].read_bytes() == before


def test_unwrap_refuses_and_writes_nothing_when_the_secret_is_gone(
    harness, capsys
) -> None:
    """A missing secret must abort, never restore a marker into a live config.

    `restore` resolves every site before writing any, so the failure direction
    is "nothing changed" rather than a config whose `env` holds tegh's internal
    reference object — which would read as a working server and would not be.
    """
    _give_ledger(harness, env={"LEDGER_API_KEY": _SECRET})
    assert main(["init"]) == 0
    capsys.readouterr()
    assert _wrap_answering(harness, scripted([""])) == 0

    store = _store(harness)
    wrapped = harness["claude_json"].read_bytes()
    store.secrets_path(harness["project"]).write_text("{}", encoding="utf-8")

    capsys.readouterr()
    rc = main(["unwrap", "--yes", "--project", str(harness["project"])])
    assert rc != 0
    assert "LEDGER_API_KEY" in capsys.readouterr().err
    assert harness["claude_json"].read_bytes() == wrapped, "the config was touched"
    assert store.backup_path(harness["project"]).exists(), "the backup was consumed"


def test_a_header_credential_still_refuses_before_anything_is_burned(
    harness, capsys
) -> None:
    """The remote leg is a follow-on, and the refusal has to arrive early.

    `header_map` exists in the base and tegh does not emit it, so a
    header credential is not relocatable by this path. That it refuses is the
    wrap contract; that it refuses BEFORE the manifest is written and before
    the first ceremony subprocess is what makes the question askable at all —
    asked last, the operator paid for a full review and admission pass to be
    told no.
    """
    before = _give_ledger(harness, headers={"X-Widget-Trace": _SECRET})

    assert main(["init"]) == 0
    capsys.readouterr()
    rc = _wrap_answering(harness, scripted([""]))
    captured = capsys.readouterr()

    assert rc != 0
    assert _HEADER_COORDINATE in captured.err, captured.err
    assert "header_map" in captured.err, "the refusal must say what would fix it"

    # Nothing was burned: key #1 is the ceremony's input, and the snapshot is
    # the first subprocess a wrap runs. Neither exists.
    store = _store(harness)
    assert not store.manifest_path(harness["project"]).exists()
    assert not store.snapshot_path(harness["project"], "ledger").exists()
    assert not (harness["project"] / "tegh.lock").exists()
    assert harness["claude_json"].read_bytes() == before

    # No CREDENTIAL decision is persisted — not even its hash. A refused wrap
    # needs nothing from a later run, and the file must never become a place
    # where a possible secret is fingerprinted for no benefit.
    record = configvalues.load_record(store.config_decisions_path(harness["project"]))
    assert record.decisions == {}


def test_a_classification_is_remembered_but_only_for_the_value_it_was_made_about(
    harness, tmp_path, capsys
) -> None:
    """A decision keyed on a NAME is the name heuristic, hand-trained.

    Remembering is what makes the second wrap non-interactive, and it is the
    only thing that does — `--admit-all` deliberately cannot supply this answer.
    But a record that cleared `MEMORY_FILE_PATH` forever would silently carry a
    value that had since become a credential, which is precisely the failure the
    whole approach exists to avoid. So the record is bound to a hash of the
    value, and a changed value is a new question.
    """
    project = str(harness["project"])
    first = tmp_path / "claude-memory.json"
    first.write_text("{}", encoding="utf-8")
    _give_ledger(harness, env={"MEMORY_FILE_PATH": str(first)})

    assert main(["init"]) == 0
    assert _wrap_answering(harness, scripted(["n"])) == 0
    assert main(["unwrap", "--yes", "--project", project]) == 0
    capsys.readouterr()

    # --- same value: asking again would be the bug ---------------------------
    # `scripted([])` raises on the first question it is asked, so this passing
    # IS the assertion that nothing was re-asked.
    assert _wrap_answering(harness, scripted([])) == 0
    out = capsys.readouterr().out
    assert "already classified as configuration" in out, out
    # ...and it was a whole wrap, not a wrap that admitted nothing and returned
    # 0 for a different reason.
    assert "INTERPOSED" in out, out
    assert main(["unwrap", "--yes", "--project", project]) == 0
    capsys.readouterr()

    # --- changed value: a new question, and it says why ----------------------
    second = tmp_path / "other-memory.json"
    second.write_text("{}", encoding="utf-8")
    _give_ledger(harness, env={"MEMORY_FILE_PATH": str(second)})

    asked: list[str] = []

    def _watching(text: str) -> str:
        asked.append(text)
        return "n"

    assert _wrap_answering(harness, _watching) == 0
    out = capsys.readouterr().out
    assert any("is MEMORY_FILE_PATH a CREDENTIAL?" in question for question in asked), asked
    assert "CHANGED since you last classified it" in out, out

    # The re-ask is not a ritual: the new answer carries the NEW value.
    manifest = yaml.safe_load(
        _store(harness).manifest_path(harness["project"]).read_text(encoding="utf-8")
    )
    assert manifest["mcp_servers"]["ledger"]["env"] == {"MEMORY_FILE_PATH": str(second)}


def test_a_cleared_value_with_nowhere_to_go_is_named_not_dropped(
    harness, tmp_path, capsys
) -> None:
    """Clearing the gate and reaching the server are two different things.

    A `headers` value has no static counterpart on the broker's server
    declaration, so classifying one as configuration lets the wrap proceed and
    delivers nothing. Silently dropping it would be the same silent-
    incompleteness failure the discovery half already refuses to commit: the
    operator would be left believing the wrapped server still receives what its
    config says it receives.

    The header sits on the stdio entry because the inventory is taken from the
    config BLOCK rather than from discovery's effective server list — what a
    wrap displaces is what tegh must account for — so this exercises the
    undelivered path without a network round-trip.
    """
    memory_file = tmp_path / "claude-memory.json"
    memory_file.write_text("{}", encoding="utf-8")
    _give_ledger(
        harness,
        env={"MEMORY_FILE_PATH": str(memory_file)},
        headers={"X-Widget-Trace": "a-literal-value"},
    )

    assert main(["init"]) == 0
    capsys.readouterr()
    # env sorts before headers, so: the path, then the header.
    assert _wrap_answering(harness, scripted(["n", "n"])) == 0
    out = capsys.readouterr().out

    assert "NOT delivered to the wrapped" in out, out
    # The stranded list is exactly the header — a report that named the
    # delivered value too would be no report at all.
    assert _stranded_coordinates(out) == [_HEADER_COORDINATE], out

    manifest = yaml.safe_load(
        _store(harness).manifest_path(harness["project"]).read_text(encoding="utf-8")
    )
    declaration = manifest["mcp_servers"]["ledger"]
    assert declaration["env"] == {"MEMORY_FILE_PATH": str(memory_file)}
    assert "headers" not in declaration
    assert "a-literal-value" not in yaml.safe_dump(manifest)


# ---------------------------------------------------------------------------
# Relocation's exit predicate: a credentialed server, called through the gateway
# ---------------------------------------------------------------------------

_CREDENTIALED_TOY = "tegh.tests.toys.credentialed_mcp_server"
_API_KEY = "not-a-real-credential-vendor-fixture"
_API_KEY_FINGERPRINT = hashlib.sha256(_API_KEY.encode("utf-8")).hexdigest()[:12]


@pytest.fixture
def credentialed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A project whose one server REFUSES to start without its API key.

    The distinction from the `harness` fixture is the whole point: that toy
    ignores its environment, so a wrap that delivered nothing would still pass.
    This one exits non-zero at startup without `LEDGER_API_KEY`, which is what
    makes the delivery assertion below mean something.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "widget"
    project.mkdir()

    claude_json = home / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "disableClaudeAiConnectors": True,
                "projects": {
                    str(project): {
                        "mcpServers": {
                            "vendor": {
                                "command": sys.executable,
                                "args": ["-m", _CREDENTIALED_TOY],
                                "env": {"LEDGER_API_KEY": _API_KEY},
                            }
                        },
                    }
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("TEGH_HOME", str(tmp_path / "tegh"))
    monkeypatch.chdir(REPO)
    for var in ("BROKER_STORE", "BROKER_SQLITE_PATH", "BROKER_MANIFEST", "BROKER_HMAC_KEY",
                "BROKER_AUDIT_PATH", "BROKER_SECRETS", "BROKER_SECRETS_FILE",
                "BROKER_GRANT_LOAD", "BROKER_ENVELOPE_LOAD", "BROKER_LOCAL_IDENTITY",
                "ISSUER_SIGNING_KEY_SECRET_ARN", "LEDGER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return {"home": home, "project": project, "claude_json": claude_json,
            "tegh_home": tmp_path / "tegh"}


def test_a_credentialed_server_is_wrapped_and_called_through_the_gateway(
    credentialed, capsys
) -> None:
    """Phase 6's exit predicate, end to end and on every axis at once.

    A server that *cannot start* without an API key is discovered, admitted and
    CALLED through the gateway, with the key absent from the harness config —
    and the proof that it arrived is the server's own report of the value it
    started with, not an assertion about tegh's manifest.

    Until this test existed, the strongest available claim was that the right
    words appeared in the right YAML file. That is exactly the shape of proof
    #1 was filed about: the chain was verified to the manifest and no further,
    and a silent delivery failure would have looked identical.
    """
    assert main(["init"]) == 0
    capsys.readouterr()
    # First the credential question (the empty answer is CREDENTIAL, which now
    # relocates), then the tool review. The tools are corrected to reads for the
    # reason correction exists: an unannotated tool proposes as an irreversible write,
    # which the broker HOLDS, and a held call would prove nothing about delivery.
    rc = wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(credentialed["project"]),
                "--harness-home", str(credentialed["home"]),
            ]
        ),
        prompt=scripted([""] + ["e", "read", "", "y"] * 2),
    )
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()

    # The predicate's negative half, read off disk rather than asserted.
    config_text = credentialed["claude_json"].read_text(encoding="utf-8")
    assert _API_KEY not in config_text
    servers = json.loads(config_text)["projects"][str(credentialed["project"])][
        "mcpServers"
    ]
    assert list(servers) == ["tegh"]
    entry = servers["tegh"]

    async def _drive() -> dict:
        params = StdioServerParameters(
            command=entry["command"], args=entry["args"], env=minimal_child_env()
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_CALL_TIMEOUT) as client:
                await client.initialize()
                listed = await client.list_tools()
                return {
                    "names": sorted(t.name for t in listed.tools),
                    "whoami": await client.call_tool("vendor__whoami", {}),
                }

    observed = asyncio.run(_drive())
    assert "vendor__whoami" in observed["names"], observed["names"]

    # The child STARTED, which alone proves a credential arrived — it exits 2
    # without one — and the fingerprint proves it was the right one. A wrap that
    # delivered some other value would reach here and fail on this line.
    answer = observed["whoami"]
    assert answer.isError is False, answer
    payload = answer.content[0].text
    assert _API_KEY_FINGERPRINT in payload, payload
    # ...and the server reported a fingerprint, never the credential itself.
    assert _API_KEY not in payload


_REGION = "eu-fixture-1"


def _classifying(by_name: dict[str, str], then):
    """Answer each credential question by the variable it NAMES, then defer.

    Keyed on the name in the prompt and not on position: the test is about what
    happens to a value classified one way or the other, and it should not also
    be a test of the order the questions are asked in.
    """

    def _prompt(text: str) -> str:
        for name, answer in by_name.items():
            if f"is {name} a CREDENTIAL?" in text:
                return answer
        return then(text)

    return _prompt


def _wrap_vendor_with_a_region(credentialed: dict) -> int:
    """Wrap the credentialed server with a second, configuration-classified value."""
    document = json.loads(credentialed["claude_json"].read_text(encoding="utf-8"))
    vendor = document["projects"][str(credentialed["project"])]["mcpServers"]["vendor"]
    vendor["env"]["LEDGER_REGION"] = _REGION
    credentialed["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return wrap_command(
        _parse_args(
            [
                "wrap", "claude",
                "--project", str(credentialed["project"]),
                "--harness-home", str(credentialed["home"]),
            ]
        ),
        prompt=_classifying(
            {"LEDGER_API_KEY": "", "LEDGER_REGION": "n"},
            then=scripted(["e", "read", "", "y"] * 2),
        ),
    )


def _whoami(credentialed: dict, capsys) -> dict:
    """Ask the wrapped server what it started with, through `tegh call`."""
    capsys.readouterr()
    status = main(
        ["call", "vendor__whoami", "--json", "--project", str(credentialed["project"])]
    )
    answer = json.loads(capsys.readouterr().out)
    assert status == 0 and answer["executed"] is True, answer
    # The text is the connector's whole MCP result as the broker marshalled it;
    # the server's typed report is its `structuredContent`.
    return json.loads(answer["text"])["structuredContent"]


def test_a_value_called_configuration_reaches_the_spawned_child(credentialed, capsys) -> None:
    """#1: the configuration half, proven at the child and not at the manifest.

    `test_a_value_called_configuration_reaches_the_server_tegh_spawns` above
    stops at the synthesized manifest, against a toy that ignores its
    environment. A wrap that wrote the value into the manifest and then spawned
    the server without it would pass that test. Here the server reports the
    value it actually started with, so the last hop is observed.

    The credential travels beside it by its own route (the secrets file and
    `env_map`), which is the point of using this server: the two halves of one
    `env` block are classified differently, carried differently, and both have
    to arrive in the same child.
    """
    assert main(["init"]) == 0
    assert _wrap_vendor_with_a_region(credentialed) == 0, capsys.readouterr().out

    report = _whoami(credentialed, capsys)
    assert report["region"] == _REGION
    # The credential still arrives, and still only as a fingerprint.
    assert report["fingerprint"] == _API_KEY_FINGERPRINT

    # The two values were carried by different routes, as they were classified.
    store = TeghStore(home=credentialed["tegh_home"])
    manifest_text = store.manifest_path(credentialed["project"]).read_text(encoding="utf-8")
    assert yaml.safe_load(manifest_text)["mcp_servers"]["vendor"]["env"] == {
        "LEDGER_REGION": _REGION
    }
    assert _API_KEY not in manifest_text


def test_the_child_reports_what_the_manifest_carries_not_what_the_wrap_saw(
    credentialed, capsys
) -> None:
    """The proof above has teeth: change what is carried and the child's report moves.

    Without this, `region == _REGION` could be satisfied by anything that put
    the right string in front of the server, including the test's own setup.
    Removing the value from the manifest after the wrap leaves everything else
    in place, and the server then reports that it started without it.
    """
    assert main(["init"]) == 0
    assert _wrap_vendor_with_a_region(credentialed) == 0, capsys.readouterr().out

    store = TeghStore(home=credentialed["tegh_home"])
    manifest_path = store.manifest_path(credentialed["project"])
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    del manifest["mcp_servers"]["vendor"]["env"]["LEDGER_REGION"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    assert _whoami(credentialed, capsys)["region"] == ""


def test_an_undeliverable_credential_says_so_instead_of_naming_a_taskgroup(
    credentialed, capsys
) -> None:
    """Relocation's other half: the failure diagnostic was useless, and it is the
    first thing a real operator meets.

    A `${VAR}` reference is the case to drive because tegh deliberately does not
    expand one, so nothing is delivered and the server dies at startup — the
    same shape as the real vendor wrap that prompted this, whose entire output was
    `unhandled errors in a TaskGroup (1 sub-exception)`.
    """
    document = json.loads(credentialed["claude_json"].read_text(encoding="utf-8"))
    document["projects"][str(credentialed["project"])]["mcpServers"]["vendor"]["env"] = {
        "LEDGER_API_KEY": "${MY_UNSET_VARIABLE}"
    }
    credentialed["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    assert main(["init"]) == 0
    capsys.readouterr()
    rc = main(
        ["wrap", "claude", "--project", str(credentialed["project"]),
         "--harness-home", str(credentialed["home"]), "--admit-all"]
    )
    out = capsys.readouterr().out

    assert rc != 0
    # The cause, in the server's own words, given top billing.
    assert "the server said:" in out, out
    assert "refusing to start" in out
    # The SDK's contentless wrapper is not presented as an explanation.
    assert "unhandled errors in a TaskGroup" not in out, out
    # And the inference is offered AS an inference, naming the variable.
    assert "LEDGER_API_KEY" in out
    assert "does not expand" in out


# ---------------------------------------------------------------------------
# The golden path's ENDING: a local release for a held call, and an audit tape a command names
# ---------------------------------------------------------------------------


def test_a_held_call_is_released_locally_and_lands_on_the_tape(harness, capfd) -> None:
    """The whole arc: held -> `tegh approve` -> executed -> visible via `tegh audit`.

    This is the ending the golden path did not have. The agent's write was
    stopped — the best moment in the demo — and then nothing on the machine could
    release it, so the intent sat until its TTL expired. Every step here goes
    through a `tegh` command, because "the user can reach this" is the claim, and
    reading the tape by a hand-typed path would prove something weaker.

    capfd rather than capsys: `approve` and `audit` are subprocesses, so their
    output never passes through Python's streams in this process.
    """
    import re

    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0
    capfd.readouterr()

    entry = json.loads(harness["claude_json"].read_text())["projects"][
        str(harness["project"])
    ]["mcpServers"]["tegh"]

    async def _call_and_get_held() -> str:
        params = StdioServerParameters(
            command=entry["command"], args=entry["args"], env=minimal_child_env()
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_CALL_TIMEOUT) as client:
                await client.initialize()
                result = await client.call_tool("ledger__get_entry", {"entry_id": "L-001"})
                return result.content[0].text

    held_text = asyncio.run(_call_and_get_held())
    assert "held for approval" in held_text and "has NOT executed" in held_text

    # The agent reports the intent id, which is the only handle the user has.
    match = re.search(r"(intent-[0-9a-f]+)", held_text)
    assert match, held_text
    intent_id = match.group(1)
    capfd.readouterr()

    # --- release it, with one command ---------------------------------------
    assert (
        main([
            "approve", intent_id,
            "--project", str(harness["project"]),
            "--yes",
        ])
        == 0
    )
    released = capfd.readouterr().out
    assert "RELEASED" in released
    assert "ledger.get_entry executed" in released
    # Attributed honestly: derived, marked solo, never claimed as authenticated.
    assert "local-solo:" in released

    # --- and the tape tells the story, reached through a tegh command --------
    assert (
        main([
            "audit", "--verify",
            "--project", str(harness["project"]),
        ])
        == 0
    )
    tape = capfd.readouterr().out

    assert str(harness["tegh_home"]) in tape, "audit must NAME the tape it read"
    assert "require_approval/held" in tape
    assert "allow/executed" in tape
    assert "approved by local-solo:" in tape
    assert tape.count(intent_id) == 2, "the hold and the release share an intentId"
    assert "CHAIN CONSISTENT" in tape
    # ...and says exactly what that is worth.
    assert "NOT tamper-evidence" in tape


def test_releasing_the_same_intent_twice_is_refused(harness, capfd) -> None:
    """An intent is actioned exactly once — the second release has nothing to do.

    Worth pinning because the natural user move after a release is to tell the
    agent to retry, and a second release that silently re-executed the stored
    call would double the side effect the hold existed to control.
    """
    import re

    assert main(["init"]) == 0
    assert wrap_admit_all(harness) == 0

    entry = json.loads(harness["claude_json"].read_text())["projects"][
        str(harness["project"])
    ]["mcpServers"]["tegh"]

    async def _call() -> str:
        params = StdioServerParameters(
            command=entry["command"], args=entry["args"], env=minimal_child_env()
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=_CALL_TIMEOUT) as client:
                await client.initialize()
                result = await client.call_tool("ledger__get_entry", {"entry_id": "L-001"})
                return result.content[0].text

    intent_id = re.search(r"(intent-[0-9a-f]+)", asyncio.run(_call())).group(1)
    args = ["approve", intent_id, "--project", str(harness["project"]), "--yes"]

    assert main(args) == 0
    capfd.readouterr()
    assert main(args) == 2  # REFUSED
    assert "not pending" in capfd.readouterr().err
