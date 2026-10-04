#!/usr/bin/env python3
"""The fresh-install drill: the quickstart's chain, run against an INSTALLED `tegh`.

    python tegh/tests/drill_fresh_install.py --tegh /path/to/venv/bin/tegh

It drives the `tegh` console script as subprocesses through the flow of
`docs/tegh-quickstart.md` sections 3 to 8: init, wrap with a reviewed admission,
a call that executes, a call that is held and then released, a call that is
denied, the tape read back, and the unwrap. Each step asserts on what the
command did, and the first step that fails prints the command, its exit status
and its output, then exits 1.

**What a green run proves.** That this path works from the package as installed,
on this machine, with nothing inherited from a developer's shell: the default
tegh home (`~/.tegh`), the default harness home, and a server that cannot start
unless the credential tegh relocated reaches it.

**What it does not prove.** How long any of this takes a person. The review
answers are piped, so nobody reads a classification or decides anything, and the
review is where a person's time goes. The elapsed figure printed at the end is
machine time and is labelled as such. The measurement with a person in it is
#2. It also says nothing about Claude Code itself: no harness is started, and
`tegh call` stands in for the harness's MCP client.

**Why a script and not a pytest file.** It has to run against a non-editable
install on a pristine runner, where there is no pytest and where `tegh.tests` is
not importable (the tests are excluded from the built distribution). So this
file is stdlib-only and imports nothing from `tegh`, `safe_agents` or `mcp`.
`test_drill_fresh_install.py` runs it under pytest so that the suite notices
when it rots.

Exit status: 0 every step passed; 1 a step failed; 2 the drill could not start
(no `tegh` at the path given, or no interpreter beside it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

EXIT_PASSED = 0
EXIT_STEP_FAILED = 1
EXIT_COULD_NOT_START = 2

#: The fixture server. It exits 2 at startup without `LEDGER_API_KEY`, which is
#: why it is the fixture: a server that cannot fail would show that the flow
#: completes, and nothing about whether the relocated credential arrived.
#: Launched BY PATH from this checkout, because the toys are excluded from the
#: built distribution and `-m tegh.tests.toys...` does not resolve against a
#: non-editable install.
TOY_SERVER = Path(__file__).resolve().parent / "toys" / "credentialed_mcp_server.py"

SERVER_ID = "vendor"
GATEWAY_SERVER_ID = "tegh"
CREDENTIAL_VAR = "LEDGER_API_KEY"
#: Obviously not a credential, and the same string the pytest suite uses.
FIXTURE_KEY = "not-a-real-credential-vendor-fixture"
#: What the toy's `whoami` reports in place of the key: 12 hex of its SHA-256.
FIXTURE_KEY_FINGERPRINT = hashlib.sha256(FIXTURE_KEY.encode("utf-8")).hexdigest()[:12]

#: The review, in the order `tegh wrap` asks. Tools are presented alphabetically.
REVIEW_ANSWERS: tuple[str, ...] = (
    # The credential question. Empty is the default, which relocates the value.
    "",
    # get_entry: admitted as proposed. The toy advertises no annotations, so the
    # proposal is an irreversible external write, which the broker HOLDS. That
    # is the hold leg of the drill.
    "y",
    # whoami: edit, effect=read, keep egress_arg, admit. A read executes.
    "e",
    "read",
    "",
    "y",
)

READ_TOOL = f"{SERVER_ID}__whoami"
HELD_TOOL = f"{SERVER_ID}__get_entry"
#: Never admitted, and the toy does not have it: the position of an injected
#: agent guessing at a name.
UNADMITTED_TOOL = f"{SERVER_ID}__delete_entry"
UNADMITTED_OP = "delete_entry"
ENTRY_ARGS = '{"entry_id":"L-001"}'

#: The handle a person copies out of the held answer. The MCP wire carries no
#: structured field for it, so the text is the only place it exists.
INTENT_ID_PATTERN = re.compile(r"intent-[0-9a-f]+")

SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
#: Copied through when set, so `tegh approve` can name the operator it records.
IDENTITY_VARS = ("USER", "LOGNAME")
INTERPRETER_NAMES = ("python", "python3")

#: Per command. Far above what any step needs; it exists so that a hung child
#: fails one step with its output, and does not sit until the CI job is killed.
COMMAND_TIMEOUT_SECONDS = 180.0

TIMED_STEPS = "steps 2 to 10"
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"


@dataclass(frozen=True)
class Ran:
    """One finished `tegh` subprocess, kept so a failure can show it."""

    argv: tuple[str, ...]
    returncode: Optional[int]
    stdout: str
    stderr: str


class StepFailed(Exception):
    """An assertion did not hold. Carries the command it was made about."""

    def __init__(self, why: str, ran: Optional[Ran] = None) -> None:
        super().__init__(why)
        self.why = why
        self.ran = ran


class Drill:
    """The temp tree, the environment, and the steps that run in it."""

    def __init__(self, tegh: Path, interpreter: Path, root: Path) -> None:
        self.tegh = tegh
        self.interpreter = interpreter
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.config = self.home / ".claude.json"
        self.original_config = b""
        self.intent_id = ""
        self.last: Optional[Ran] = None
        self.home.mkdir()
        self.project.mkdir()
        # Built from scratch, never from `os.environ`. Passing the parent
        # environment is what hid a real defect in this product once: the
        # gateway entry named no tegh home and resolved it from the spawning
        # process, which worked wherever the test's own environment leaked in
        # and failed for the first operator whose harness passed nothing.
        #
        # HOME points into the temp tree, so `~/.tegh` and `~/.claude.json` are
        # the DEFAULTS a first run uses, and never the developer's real ones.
        # No `--home` or `--harness-home` is passed anywhere for the same
        # reason: naming them would test a path a stranger does not take.
        self.env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join([str(tegh.parent), *SYSTEM_PATH]),
            **{name: os.environ[name] for name in IDENTITY_VARS if name in os.environ},
        }

    # -- running and asserting ----------------------------------------------

    def run(self, *args: str, stdin: str = "") -> Ran:
        """Run `tegh <args>` in the drill's environment and remember the result.

        The working directory is the temp project and never this checkout.
        tegh starts its children as `python -m ...`, which puts the working
        directory first on `sys.path`. From the checkout that makes the source
        tree's `tegh` importable ahead of the installed one, and a drill that
        can reach the source tree can pass on a package that is missing a file.

        stdin is always a pipe, empty unless the step has answers to give, so a
        command that unexpectedly prompts reads end-of-file and does not wait.
        """
        argv = (str(self.tegh), *args)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                env=self.env,
                cwd=self.project,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.last = Ran(argv, None, _text(exc.stdout), _text(exc.stderr))
            raise StepFailed(
                f"no exit within {COMMAND_TIMEOUT_SECONDS:g}s", self.last
            ) from None
        except OSError as exc:
            self.last = Ran(argv, None, "", "")
            raise StepFailed(f"could not be started: {exc}", self.last) from None
        self.last = Ran(argv, done.returncode, done.stdout, done.stderr)
        return self.last

    def expect(self, holds: bool, why: str) -> None:
        if not holds:
            raise StepFailed(why, self.last)

    def expect_exit(self, ran: Ran, status: int) -> None:
        self.expect(ran.returncode == status, f"expected exit status {status}")

    def files_holding_the_key(self) -> list[Path]:
        """Every file under the drill's tree whose bytes contain the fixture key."""
        needle = FIXTURE_KEY.encode("utf-8")
        return [
            path
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
        ]

    def call(self, tool: str, *args: str) -> tuple[Ran, dict]:
        """`tegh call <tool> --json`, with its one-line JSON answer parsed."""
        ran = self.run("call", tool, *args, "--json", "--project", str(self.project))
        try:
            answer = json.loads(ran.stdout)
        except json.JSONDecodeError:
            raise StepFailed("stdout is not the JSON answer --json prints", ran) from None
        self.expect(isinstance(answer, dict), "the --json answer is not an object")
        return ran, answer

    # -- the steps ----------------------------------------------------------

    def write_harness_config(self) -> None:
        document = {
            # Set so the wrap is complete without `--accept-gaps`: claude.ai
            # connectors live cloud-side and survive any local rewrite.
            "disableClaudeAiConnectors": True,
            "projects": {
                str(self.project): {
                    "mcpServers": {
                        SERVER_ID: {
                            "command": str(self.interpreter),
                            "args": [str(TOY_SERVER)],
                            "env": {CREDENTIAL_VAR: FIXTURE_KEY},
                        }
                    }
                }
            },
        }
        # These two arguments are tegh's own serializer settings. It refuses to
        # rewrite a config that does not round-trip through them.
        text = json.dumps(document, indent=2, ensure_ascii=False)
        self.original_config = text.encode("utf-8")
        self.config.write_bytes(self.original_config)

    def init(self) -> None:
        self.expect_exit(self.run("init"), 0)
        self.expect((self.home / ".tegh").is_dir(), "no tegh home at the default ~/.tegh")

    def wrap(self) -> None:
        # Never `--admit-all`: the quickstart forbids it, and a person deciding
        # per tool is the path being drilled.
        ran = self.run(
            "wrap", "claude", "--project", str(self.project),
            stdin="".join(f"{answer}\n" for answer in REVIEW_ANSWERS),
        )
        self.expect_exit(ran, 0)
        self.expect("INTERPOSED" in ran.stdout, "stdout does not say INTERPOSED")
        rewritten = self.config.read_text(encoding="utf-8")
        servers = json.loads(rewritten)["projects"][str(self.project)]["mcpServers"]
        self.expect(
            list(servers) == [GATEWAY_SERVER_ID],
            f"the rewritten config's servers are {sorted(servers)}, "
            f"expected only {GATEWAY_SERVER_ID!r}",
        )
        self.expect(
            FIXTURE_KEY not in rewritten,
            "the credential is still in the harness config after the wrap",
        )
        # The config check alone cannot tell a relocated credential from one the
        # reviewer called configuration: the rewrite removes the server entry
        # either way, and a value carried as configuration lands in the
        # project's manifest, which other users can read. So look at where the
        # value is at rest, anywhere in the tree, and require owner-only files.
        exposed = [
            str(path.relative_to(self.root))
            for path in self.files_holding_the_key()
            if stat.S_IMODE(path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        ]
        self.expect(
            not exposed,
            f"the credential is at rest in file(s) that are not owner-only: {exposed}",
        )

    def call_the_read(self) -> None:
        ran, answer = self.call(READ_TOOL)
        self.expect_exit(ran, 0)
        self.expect(answer.get("executed") is True, "`executed` is not true")
        text = str(answer.get("text", ""))
        # The server started, so a credential arrived; the fingerprint says it
        # was the one that was relocated.
        self.expect(
            FIXTURE_KEY_FINGERPRINT in text,
            f"the answer does not carry the credential's fingerprint "
            f"{FIXTURE_KEY_FINGERPRINT}",
        )
        self.expect(FIXTURE_KEY not in text, "the answer carries the credential itself")

    def call_the_held_write(self) -> None:
        ran, answer = self.call(HELD_TOOL, "--args", ENTRY_ARGS)
        self.expect_exit(ran, 1)
        self.expect(answer.get("executed") is False, "`executed` is not false")
        text = str(answer.get("text", ""))
        self.expect("held for approval" in text, "the answer does not say held for approval")
        found = INTENT_ID_PATTERN.search(text)
        self.expect(found is not None, "the held answer names no intent id")
        assert found is not None
        self.intent_id = found.group(0)

    def approve(self) -> None:
        ran = self.run("approve", self.intent_id, "--yes", "--project", str(self.project))
        self.expect_exit(ran, 0)
        self.expect("RELEASED" in ran.stdout, "stdout does not say RELEASED")
        self.expect("local-solo:" in ran.stdout, "the release names no local-solo approver")

    def call_the_unadmitted_tool(self) -> None:
        # Without `--json`: this is the line the quickstart shows a person.
        ran = self.run(
            "call", UNADMITTED_TOOL, "--args", ENTRY_ARGS, "--project", str(self.project)
        )
        self.expect_exit(ran, 1)
        self.expect(
            "refused by the broker" in ran.stdout, "stdout does not say refused by the broker"
        )

    def verify_the_chain(self) -> None:
        ran = self.run("audit", "--verify", "--project", str(self.project))
        self.expect_exit(ran, 0)
        self.expect("CHAIN CONSISTENT" in ran.stdout, "stdout does not say CHAIN CONSISTENT")

    def read_the_tape(self) -> None:
        # `tegh audit --json` and not the file: it is the supported way to read
        # the tape, and it carries every field asserted on here.
        ran = self.run("audit", "--json", "--project", str(self.project))
        self.expect_exit(ran, 0)
        try:
            records = json.loads(ran.stdout)["records"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise StepFailed("stdout is not the JSON tape --json prints", ran) from None

        denied = [
            record
            for record in records
            if (record.get("decision"), record.get("outcome"), record.get("op"))
            == ("deny", "denied", UNADMITTED_OP)
        ]
        self.expect(
            len(denied) == 1, f"{len(denied)} deny/denied record(s) for {UNADMITTED_OP}, expected 1"
        )
        self.expect(bool(denied[0].get("reason")), "the deny record carries no reason")

        of_the_intent = [r for r in records if r.get("intentId") == self.intent_id]
        verdicts = sorted((r.get("decision"), r.get("outcome")) for r in of_the_intent)
        self.expect(
            verdicts == [("allow", "executed"), ("require_approval", "held")],
            f"records carrying {self.intent_id} are {verdicts}, "
            "expected one require_approval/held and one allow/executed",
        )
        released = next(r for r in of_the_intent if r.get("decision") == "allow")
        self.expect(bool(released.get("approvedBy")), "the release record names no approver")

    def unwrap(self) -> None:
        # `--yes` because nobody is at this stdin: unwrap prints what it will
        # change and asks, and without the flag a pipe is refused.
        ran = self.run("unwrap", "--yes", "--project", str(self.project))
        self.expect_exit(ran, 0)
        self.expect(
            self.config.read_bytes() == self.original_config,
            "the harness config is not byte-identical to the original",
        )
        # The credential went back into the config, so tegh must not keep its
        # own copy. Searched for across the whole tree, as after the wrap: the
        # store is one place a copy could be left, and not the only one.
        elsewhere = [
            str(path.relative_to(self.root))
            for path in self.files_holding_the_key()
            if path != self.config
        ]
        self.expect(
            not elsewhere,
            f"the credential is still at rest outside the harness config: {elsewhere}",
        )
        self.expect(
            FIXTURE_KEY not in ran.stdout + ran.stderr,
            "unwrap printed the credential",
        )


def _text(captured: object) -> str:
    """What a timed-out child had written, whichever type subprocess hands back."""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", errors="replace")
    return captured if isinstance(captured, str) else ""


def _steps(drill: Drill) -> list[tuple[str, Callable[[], None]]]:
    return [
        ("harness config written: one server that needs a credential", drill.write_harness_config),
        ("tegh init", drill.init),
        ("tegh wrap claude, review answers piped: INTERPOSED, credential relocated", drill.wrap),
        (f"tegh call {READ_TOOL}: executed with the relocated credential", drill.call_the_read),
        (f"tegh call {HELD_TOOL}: held for approval", drill.call_the_held_write),
        ("tegh approve: RELEASED by a local-solo approver", drill.approve),
        (f"tegh call {UNADMITTED_TOOL}: refused by the broker", drill.call_the_unadmitted_tool),
        ("tegh audit --verify: CHAIN CONSISTENT", drill.verify_the_chain),
        ("tape records: one deny, and the hold and its release share an intent", drill.read_the_tape),
        (
            "tegh unwrap: harness config restored byte for byte, credential gone from tegh's store",
            drill.unwrap,
        ),
    ]


def _report_failure(number: int, title: str, failure: StepFailed) -> None:
    print(f"\nFAILED at step {number} ({title}): {failure.why}", file=sys.stderr)
    ran = failure.ran
    if ran is None:
        return
    status = "none (did not exit)" if ran.returncode is None else str(ran.returncode)
    print(f"  command:     {' '.join(ran.argv)}", file=sys.stderr)
    print(f"  exit status: {status}", file=sys.stderr)
    for label, captured in (("stdout", ran.stdout), ("stderr", ran.stderr)):
        print(f"  --- {label} ---", file=sys.stderr)
        print(captured.rstrip("\n") or "(empty)", file=sys.stderr)


def _append_step_summary(passed: list[str], timing: str) -> None:
    """Put the timing line and the step list on the workflow run's summary page."""
    path = os.environ.get(STEP_SUMMARY_ENV)
    if not path:
        return
    lines = ["### tegh fresh-install drill", "", timing, "", *passed, ""]
    with open(path, "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


def run_drill(tegh: Path, interpreter: Path, *, keep: bool) -> int:
    # Resolved, because on macOS the temp directory sits behind a symlink
    # (`/var` is `/private/var`) and `tegh wrap` resolves the project path. An
    # unresolved path here would key the harness config under a name the wrap
    # never looks up, and it would discover no servers.
    root = Path(tempfile.mkdtemp(prefix="tegh-drill-")).resolve()
    passed: list[str] = []
    try:
        drill = Drill(tegh, interpreter, root)
        started = 0.0
        for number, (title, step) in enumerate(_steps(drill), start=1):
            if number == 2:
                started = time.monotonic()
            try:
                step()
            except StepFailed as failure:
                _report_failure(number, title, failure)
                return EXIT_STEP_FAILED
            passed.append(f"{number}. {title}")
            print(passed[-1], flush=True)
        elapsed = time.monotonic() - started

        # Machine time, said in full on one line so that the figure cannot be
        # lifted without its qualifier. It is not a claim about a person.
        timing = (
            f"Machine time with piped review answers, {TIMED_STEPS}: {elapsed:.1f}s. "
            "No person read or decided anything in this run, so this is not how "
            "long the walk takes a person (#2)."
        )
        print(f"{len(passed) + 1}. {timing}")
        _append_step_summary(passed, timing)
        return EXIT_PASSED
    finally:
        if keep:
            print(f"kept the drill's tree at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def _interpreter_beside(tegh: Path) -> Optional[Path]:
    """The interpreter of the environment `tegh` is installed in.

    Not `sys.executable`: the drill may be started by any Python, and the toy
    server needs the one whose environment holds the MCP SDK. Not resolved
    either, because a venv's `python` is a symlink out of the venv and following
    it would leave the environment behind.
    """
    for name in INTERPRETER_NAMES:
        candidate = tegh.with_name(name)
        if candidate.exists():
            return candidate
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive an installed `tegh` through the quickstart's chain and assert each step.",
        epilog=(
            "exit status: 0 every step passed; 1 a step failed; 2 the drill could not start."
        ),
    )
    parser.add_argument(
        "--tegh",
        default=str(Path(sys.executable).with_name("tegh")),
        help="the `tegh` console script to drill (default: the one beside this interpreter)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the temp tree in place for inspection"
    )
    args = parser.parse_args(argv)

    # Made absolute without following symlinks: the commands run from a temp
    # directory, and the launcher's neighbours are found relative to it.
    tegh = Path(os.path.abspath(os.path.expanduser(args.tegh)))
    if not (tegh.is_file() and os.access(tegh, os.X_OK)):
        print(f"REFUSED: no executable `tegh` at {tegh}. Pass --tegh.", file=sys.stderr)
        return EXIT_COULD_NOT_START
    interpreter = _interpreter_beside(tegh)
    if interpreter is None:
        print(
            f"REFUSED: no Python interpreter beside {tegh} to launch the fixture "
            f"server with (looked for {', '.join(INTERPRETER_NAMES)}).",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_START
    return run_drill(tegh, interpreter, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
