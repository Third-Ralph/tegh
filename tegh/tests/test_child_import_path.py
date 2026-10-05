"""No Python process tegh starts imports from the directory `tegh` was run in.

Every Python child is started with `-P` (`launch.SAFE_PATH_FLAG`), through one
helper: the directory tegh is run from is not on the child's import path.

The end-to-end test runs the real CLI from INSIDE a project directory that
holds two marker modules, one under a standard-library name and one under the
name of a dependency, both of which the children import. A marker that is
imported writes a file and then fails the import, so a child that reached one
is loud twice. The flow is the quickstart's: init, wrap, a call that executes,
a call that is held, approve, posture, audit and diff.

The wrapped server is started the same way, by an absolute interpreter path
with `-P`. Its command is the user's configuration and not tegh's to change;
giving the toy the flag keeps a marker it imported from being counted against
tegh.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tegh import cli, launch, posture
from tegh.tests.wrapping import TOY

#: One standard-library module and one dependency. Every child tegh starts
#: imports the first; every child but the tape reader imports the second.
_SHADOWED = ("argparse", "yaml")

_MARKER = """\
import os
import sys

with open(os.path.join({seen!r}, "{name}-%d" % os.getpid()), "w") as seen:
    seen.write(" ".join(sys.orig_argv))
raise ImportError("{name} was imported from the directory tegh was run in")
"""

#: get_entry admitted as proposed, which the broker holds; list_entries
#: corrected to a read, which executes.
_REVIEW = "y\ne\nread\n\ny\n"
_INTENT = re.compile(r"intent-[0-9a-f]+")


def _console_script() -> list[str]:
    return [str(Path(sys.executable).with_name("tegh"))]


def _module_launcher() -> list[str]:
    """tegh as `tegh_launcher` spells it where there is no console script."""
    return launch.python_module_argv("tegh.cli")


@pytest.fixture
def workplace(tmp_path: Path) -> dict:
    """A home, and a project that holds the markers and is the working directory."""
    home, project, seen = tmp_path / "home", tmp_path / "widget", tmp_path / "seen"
    for directory in (home, project, seen):
        directory.mkdir()
    for name in _SHADOWED:
        (project / f"{name}.py").write_text(
            _MARKER.format(seen=str(seen), name=name), encoding="utf-8"
        )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "disableClaudeAiConnectors": True,
                "projects": {
                    str(project): {
                        "mcpServers": {
                            # The toy with the same flag, so that what it
                            # imports is not what this test measures.
                            "ledger": {"command": sys.executable, "args": ["-P", "-m", TOY]}
                        }
                    }
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {"home": home, "project": project, "seen": seen, "tegh_home": tmp_path / "tegh"}


def _tegh(workplace: dict, launcher: list[str], *argv: str, stdin: str = ""):
    """One `tegh` command, run from inside the project, with a harness's environment."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(workplace["home"]),
        "TEGH_HOME": str(workplace["tegh_home"]),
        "USER": "someone",
    }
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*launcher, *argv], cwd=workplace["project"], env=env, input=stdin,
        capture_output=True, text=True, check=False, timeout=180,
    )
    imported = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(workplace["seen"].iterdir())
    }
    assert not imported, (
        f"`tegh {' '.join(argv)}` imported a module from the directory it was run in: "
        f"{imported}\n{done.stdout}\n{done.stderr}"
    )
    return done


@pytest.mark.parametrize("launcher", [_console_script, _module_launcher])
def test_no_tegh_process_imports_from_the_directory_tegh_is_run_in(workplace, launcher) -> None:
    pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")
    tegh = launcher()
    if not Path(tegh[0]).exists():
        pytest.skip("no tegh console script beside this interpreter")

    def run(*argv: str, exits: int, stdin: str = ""):
        done = _tegh(workplace, tegh, *argv, stdin=stdin)
        assert done.returncode == exits, f"tegh {' '.join(argv)}\n{done.stdout}\n{done.stderr}"
        return done

    run("init", exits=0)
    # `--project` is left to its default everywhere: the directory tegh is in.
    assert "INTERPOSED" in run("wrap", "claude", exits=0, stdin=_REVIEW).stdout
    listed = run("call", "ledger__list_entries", "--args", '{"limit": 1}', exits=0)
    assert "L-001" in listed.stdout
    held = run("call", "ledger__get_entry", "--args", '{"entry_id": "L-001"}', exits=1)
    intent = _INTENT.search(held.stdout)
    assert intent, held.stdout
    assert "RELEASED" in run("approve", intent.group(0), "--yes", exits=0).stdout
    # The store audit ran and was read: its skipped-rules line is there, and
    # none of the lines that say it could not run or be parsed.
    report = run("posture", exits=0).stdout
    assert "audit rule(s) did not run" in report and "The store audit" not in report, report
    assert "CHAIN CONSISTENT" in run("audit", "--verify", exits=0).stdout
    run("diff", exits=0)


def test_every_python_child_is_started_through_the_one_helper(monkeypatch, tmp_path) -> None:
    """The argv each spawn site is built from carries the flag, before `-m`."""
    assert launch.python_module_argv("some.module", "--flag") == [
        sys.executable, launch.SAFE_PATH_FLAG, "-m", "some.module", "--flag",
    ]
    assert launch.SAFE_PATH_FLAG == "-P"
    for argv in (cli._CEREMONY, posture._AUDIT_COMMAND):
        assert argv[:3] == [sys.executable, "-P", "-m"], argv
    # The launcher form, which is what a harness config holds when there is no
    # console script beside the interpreter.
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    assert launch.tegh_launcher() == [str(tmp_path / "python"), "-P", "-m", "tegh.cli"]


def test_the_tape_reader_is_given_the_named_environment_and_nothing_else(
    monkeypatch, tmp_path
) -> None:
    """`tegh audit` used to hand its child the whole shell."""
    monkeypatch.setenv("TEGH_HOME", str(tmp_path / "tegh"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("BROKER_AUDIT_PATH", "/somewhere/else.jsonl")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    started: list[tuple[list[str], dict]] = []

    def run(argv, *, env, check):
        started.append((list(argv), dict(env)))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["audit", "--project", str(tmp_path)]) == 0
    ((argv, env),) = started
    assert argv[:4] == [sys.executable, "-P", "-m", "safe_agents.broker.auditor.tape_cli"]
    assert set(env) <= set(launch.BROKER_INHERITED_ENV_VARS), sorted(env)
    assert env["LANG"] == "en_US.UTF-8"
