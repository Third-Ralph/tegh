"""How `tegh gateway` gets spawned: its argv, and the environment a harness gives it.

Two callers start the gateway for a wrapped project, and they must start the SAME
process:

- the harness, from the entry `tegh wrap` wrote into its config
  (`harnesses/*.py::gateway_entry`), and
- `tegh call` (`call.py`), which drives one call through the gateway the way the
  harness's own client would.

Both build the command from `gateway_argv` here, so a change to how the gateway
is invoked reaches the written config and `tegh call` together. Two spellings of
one command is how the one a test drives stops being the one a user's harness
runs.

This module imports nothing from the base and nothing from the rest of tegh. It
is plumbing that `cli.py`, the harness adapters and `call.py` all stand on, and
anything it imported would be an import cycle waiting for one of them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

#: The variables `tegh call` passes to the gateway it spawns, and no others.
#:
#: Copied from the MCP Python SDK, `mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS`
#: (the non-Windows list, read at mcp 1.30.0), which is what its stdio client
#: hands a server when the config entry names no `env` block. It is the published
#: default for an MCP stdio client and stands in for "what a harness passes":
#: `docs/references/harnesses/claude-code.md` records only that Claude Code
#: injects `CLAUDE_PROJECT_DIR`, and does not establish which other variables it
#: gives a stdio child, so this is the floor and not a claim about that harness.
#: Copied by value and
#: not imported: the `mcp` SDK is not something tegh's product code stands on, and
#: six names are cheaper to own than a dependency edge is to explain.
#:
#: POSIX only. The SDK's Windows list is a different twelve names; tegh does not
#: carry it, so a Windows `tegh call` would start its child without `SYSTEMROOT`.
HARNESS_SPAWN_ENV_VARS: tuple[str, ...] = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")

#: The variables the broker's own processes inherit from whatever started tegh:
#: the gateway, and each ceremony command `tegh` runs. `TeghStore.gateway_env` and
#: `TeghStore.ceremony_env` (`store.py`) start from these names and add the values
#: tegh sets, so configuration tegh did not choose cannot arrive from a shell (#5).
#:
#: One list, extended, and not a second one: the first six names ARE
#: `HARNESS_SPAWN_ENV_VARS`. They are what the broker's MCP client reads out of
#: its own environment to build a wrapped stdio server's, so dropping `PATH` here
#: is a server whose `npx` cannot be found, and `LOGNAME`/`USER` are where a
#: ceremony record's operator name comes from.
#:
#: The rest is why the two lists differ. `harness_spawn_env` models what a harness
#: hands over, to reproduce its spawn. This one decides what a process tegh starts
#: itself may keep, and that process also runs from an operator's terminal, where
#: three more kinds of variable are the machine's and not the broker's
#: configuration:
#:
#: - locale, which decides how the child decodes text;
#: - the temporary directory;
#: - proxy and certificate settings, without which snapshotting or calling a
#:   REMOTE server fails behind a corporate proxy. Both spellings of the proxy
#:   names, because the HTTP client reads either. These carry authority and are
#:   passed knowing it: the shell's proxy is where the broker's requests to a
#:   remote server go, credential headers included, and the shell's trust store
#:   is whose certificates it accepts.
#:
#: Everything else is dropped, on purpose. No `BROKER_*`, signing or AWS name is
#: here, and none may be added: a value the broker should run under is set by
#: tegh, by name. `PYTHONPATH` and the other `PYTHON*` names are dropped with the
#: rest, so a shell's value for one does not reach the child; the child is this
#: interpreter, which finds the base without them. The dynamic loader's names
#: (`LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`) are dropped the same way.
BROKER_INHERITED_ENV_VARS: tuple[str, ...] = (
    *HARNESS_SPAWN_ENV_VARS,
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

#: The interpreter option every Python process tegh starts is given: with it,
#: the directory tegh is run from is not on the child's import path. A child
#: finds the base and its dependencies where they are installed, whatever
#: directory `tegh` was typed in.
#:
#: An option on the command line and not the `PYTHONSAFEPATH` variable, because
#: the environments tegh builds drop every `PYTHON*` name on purpose
#: (`BROKER_INHERITED_ENV_VARS`), and because the launcher form below is
#: started by a harness with an environment tegh does not build at all.
SAFE_PATH_FLAG = "-P"

#: How long `tegh call` waits for each reply from the gateway before giving up.
#: Generous on purpose: the first call starts the connector behind the tool, and a
#: cold `npx` connector downloads its package before it can answer.
DEFAULT_CALL_TIMEOUT_SECONDS = 120.0


def python_module_argv(module: str, *args: str) -> list[str]:
    """`<this interpreter> -P -m <module> <args>`: the one way tegh starts Python.

    Every Python child goes through here: the ceremony and grant commands, the
    gateway, the release, the store audit, the tape reader, and tegh itself in
    the launcher form `tegh_launcher` writes into a harness config. One
    spelling, so that `SAFE_PATH_FLAG` is on all of them or on none.
    """
    return [sys.executable, SAFE_PATH_FLAG, "-m", module, *args]


def tegh_launcher() -> list[str]:
    """The argv that runs `tegh` for the harness to spawn.

    ABSOLUTE, never a bare name: the harness spawns MCP children with a minimal
    environment, so a `tegh` that relies on PATH is a server that fails to start
    with a diagnostic no one can act on. Two forms, in order:

    1. the installed console script, when it exists on disk beside this
       interpreter (the `pip install tegh` case), and
    2. `<this interpreter> -P -m tegh.cli` otherwise, which is what an editable
       checkout without the console script on disk has. `tegh` is a top-level
       package, so this form works wherever `tegh` is installed for that
       interpreter. It does not look for `tegh` in the directory the harness
       starts it in (`SAFE_PATH_FLAG`); the console script never did.

    Preferring the console script is not cosmetic: it survives the interpreter
    path changing under a venv rebuild, which the `-m` form does not.
    """
    scripted = Path(sys.executable).with_name("tegh")
    if scripted.exists():
        return [str(scripted)]
    return python_module_argv("tegh.cli")


def gateway_argv(
    project: Path | str, *, launcher: Sequence[str], home: Path | str
) -> list[str]:
    """The full command line that runs the gateway for one wrapped project.

    `launcher` is a full argv prefix because tegh is not always reachable as a
    console script (see `tegh_launcher`). `home` is named EXPLICITLY for the
    same reason the launcher is absolute: a spawned stdio child gets a minimal
    environment, so a `TEGH_HOME` exported in the operator's shell is not there
    at spawn time, and a gateway left to resolve the default would look in
    `~/.tegh`, find no manifest for this project, and refuse for a reason that is
    nowhere near the cause. Every resolution input is on this line; nothing is
    inherited.

    Both paths are resolved to absolute form, so the command means the same
    thing whatever directory the spawning process happens to be in.
    """
    if not launcher:
        raise ValueError("the gateway command needs a launcher argv")
    return [
        *launcher,
        "gateway",
        "--project",
        str(Path(project).expanduser().resolve()),
        "--home",
        str(Path(home).expanduser().resolve()),
    ]


def gateway_home_in(argv: Sequence[str], *, project: Path | str) -> Optional[str]:
    """The tegh home `argv` runs `project`'s gateway from, or None if it does not.

    The reading of what `gateway_argv` writes, kept beside it so the two cannot
    drift: `tegh wrap` uses it to recognise an entry it wrote itself. The match
    is on everything after the launcher, which is the part that says what the
    command does. The launcher is not compared, because it is the part that
    changes between two installs of tegh (a console script in one virtualenv,
    `python -P -m tegh.cli` in another) and between two versions of it (0.1.1
    wrote that form without the `-P`), and an entry written by any of them is
    still this project's gateway. Neither is the entry's name: a server of the user's
    own called `tegh` is not the gateway, and a renamed gateway still is.

    The home is returned and not compared. A project wrapped from another tegh
    home is wrapped all the same, and the caller needs the home to say where.
    """
    if len(argv) < 6:  # five words of `gateway_argv`, after at least one of launcher
        return None
    subcommand, project_flag, named_project, home_flag, home = argv[-5:]
    if (subcommand, project_flag, home_flag) != ("gateway", "--project", "--home"):
        return None
    if named_project != str(Path(project).expanduser().resolve()):
        return None
    return home


def inherited_env(
    names: Sequence[str], environ: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    """The named variables as `environ` holds them, and nothing else.

    The one way tegh starts a child's environment: from a list of names, never
    from a copy of the parent. A name `environ` does not hold is left out and not
    invented. A value beginning `()` is an exported shell function and is
    skipped, as the SDK skips it.
    """
    source = os.environ if environ is None else environ
    return {
        name: source[name]
        for name in names
        if name in source and not source[name].startswith("()")
    }


def harness_spawn_env(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment a harness gives a spawned stdio child, built from `environ`.

    Only `HARNESS_SPAWN_ENV_VARS`, and never the whole parent environment. A
    harness gives a spawned stdio child almost nothing, so anything the gateway
    picks up from the invoking shell is something it will not have when the
    harness starts it. That is a defect this project has already shipped once:
    the gateway entry named no tegh home and resolved `TEGH_HOME` from the
    spawning process, which passed in a test that leaked its own environment and
    failed for the first operator whose harness passed nothing. A caller that
    spawns the gateway with this environment fails where the harness's spawn
    would fail, which is the only reason to spawn it by hand at all.
    """
    return inherited_env(HARNESS_SPAWN_ENV_VARS, environ)
