"""One store database per project, and the cut from the layout that had one per home.

The base keys an admitted tool by server name and tool name. While tegh named
one database for a whole home, two projects that each called a server `ledger`
shared one admitted row for each of its tools: wrapping the second replaced
what the first had admitted, and the first project's signed lock went on
pinning a definition its gateway no longer served. The first test here is that
case, driven through real wraps and real gateways.

The rest is the cut. A project wrapped by a tegh that kept the shared database
is not served by this one, and every command that would need its admissions
says so and names the way out. Each of those tests builds the earlier state
from a real wrap (`_as_an_earlier_tegh_left_it`) and makes the earlier
database unreadable first, so a command that passes has also shown that tegh
never opened that file, and each ends by checking that it is the same file
with the same bytes.

Nothing here stands in for the gateway, the broker or the ceremony. The unit
tests of the layout marker and of the two environments are in
`test_lockfile.py`.
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh import cli  # noqa: E402
from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.lockfile import lock_paths  # noqa: E402
from tegh.store import TeghStore, project_slug  # noqa: E402
from tegh.tests.wrapping import (  # noqa: E402
    ADMIT_AS_READ,
    REPO,
    TOY_TOOL_COUNT,
    VERSIONED_TOY,
    as_fingerprinted,
    store_of,
    wrap_argv,
)

_TOOL = "ledger__get_entry"
_ENTRY = '{"entry_id": "L-001"}'
#: Two definitions of one coordinate, `ledger/get_entry`, each reviewed in a
#: project of its own.
_FIRST_SAYS = "Return one ledger entry by id. As the first project reviewed it."
_SECOND_SAYS = "Return one ledger entry by id, newest first. As the second project reviewed it."
_EARLIER_LAYOUT = "was wrapped by an earlier version of tegh"


def _serving(harness: dict, name: str, description: str) -> dict:
    """A project of this harness home whose `ledger` advertises `description`.

    The server name is the same for every project on purpose. Returns the
    harness as that project sees it: the same harness home and the same tegh
    home, another project path.
    """
    root = harness["home"].parent
    project = root / name
    project.mkdir(exist_ok=True)
    said = root / f"{name}.description"
    said.write_text(description, encoding="utf-8")
    document = json.loads(harness["claude_json"].read_text(encoding="utf-8"))
    document["projects"][str(project)] = {
        "mcpServers": {
            "ledger": {"command": sys.executable, "args": ["-m", VERSIONED_TOY, str(said)]}
        }
    }
    harness["claude_json"].write_text(
        json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {**harness, "project": project}


def _wrap_as_reads(harness: dict, *flags: str, prompt=None) -> int:
    """Wrap with a human correcting both tools to reads, so a call executes."""
    return wrap_command(
        _parse_args(wrap_argv(harness, *flags)),
        prompt=prompt or _scripted_reads(),
    )


def _scripted_reads():
    answers = list(ADMIT_AS_READ) * TOY_TOOL_COUNT

    def _prompt(text: str) -> str:
        assert answers, f"the review asked more than the script answers: {text!r}"
        return answers.pop(0)

    return _prompt


def _tegh(harness: dict, *argv: str) -> subprocess.CompletedProcess:
    """One tegh command for the harness's project, as a process of its own.

    A process and not `main`, because `tegh gateway` ends in an exec: a gateway
    that this file expects to be refused, and was not, must not take the test
    run's place. No stdin, so one that did start ends at once.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "tegh.cli", *argv, "--project", str(harness["project"])],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(REPO),
            "HOME": str(harness["home"]),
            "TEGH_HOME": str(harness["tegh_home"]),
        },
        cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        check=False, timeout=120,
    )


def _database_files(store: TeghStore, project: Path) -> bytes:
    """Every byte sqlite holds for one project: the database and any file beside it."""
    directory = store.project_dir(project)
    return b"".join(
        path.read_bytes() for path in sorted(directory.glob("store.db*")) if path.is_file()
    )


def _principal(project: Path) -> bytes:
    return f"tegh-{project_slug(project)}".encode()


def _assert_posture_audits_the_projects_own_database(harness: dict, capsys) -> None:
    """`tegh posture` audited this project's database, and counted one project's rows.

    The audit line names the file the base was pointed at, and its counts are
    one grant and one admitted row per tool. The report as a whole never names
    the database of the earlier layout: for a project this tegh wrapped that
    file is no part of its posture, whether or not the home still holds it.
    """
    store, project = store_of(harness), harness["project"]
    assert main(
        ["posture", "--json", "--project", str(project), "--harness-home", str(harness["home"])]
    ) == 0
    said = capsys.readouterr().out
    audited = json.loads(said)["store"]
    assert audited[0]["holds"] == "yes", audited
    assert audited[0]["source"].endswith(f"--sqlite {store.db_path(project)}"), audited
    assert (
        f"over {TOY_TOOL_COUNT} grant(s), 0 registry violation(s) over "
        f"{TOY_TOOL_COUNT} admitted-tool row(s)"
    ) in audited[0]["claim"], audited
    assert str(store.shared_db_path) not in said, said


# ---------------------------------------------------------------------------
# The defect: two projects, one server name, one home
# ---------------------------------------------------------------------------


def test_two_projects_naming_one_server_each_keep_what_they_admitted(harness, capsys) -> None:
    """Each project's gateway serves the definition that project reviewed.

    Both projects name the server `ledger`, and its `get_entry` is a different
    definition in each. The first is wrapped, then the second, and the first is
    asked first: with one database for the home, the second wrap's admission of
    `ledger/get_entry` is the only one left, the first project's server no
    longer matches it, and the call below is refused.

    The toy answers with the description it runs under, so an executed call
    says which definition served it. `tegh diff` then compares each lock with
    what that project's server advertises.
    """
    assert main(["init"]) == 0
    first = _serving(harness, "widget", _FIRST_SAYS)
    second = _serving(harness, "gadget", _SECOND_SAYS)
    assert _wrap_as_reads(first) == 0
    assert _wrap_as_reads(second) == 0
    capsys.readouterr()

    for project, says, other_says in (
        (first, _FIRST_SAYS, _SECOND_SAYS),
        (second, _SECOND_SAYS, _FIRST_SAYS),
    ):
        where = ["--project", str(project["project"])]
        lock = lock_paths(project["project"])[0].read_text(encoding="utf-8")
        assert says in lock and other_says not in lock

        assert main(["call", _TOOL, "--args", _ENTRY, *where]) == 0, capsys.readouterr()
        assert f"served under: {says}" in capsys.readouterr().out

        assert main(["diff", *where]) == 0, capsys.readouterr()
        assert f"DRIFT=0 WITHDRAWN=0 UNCHANGED={TOY_TOOL_COUNT}" in capsys.readouterr().out


def test_nothing_one_wrap_stores_is_in_the_other_projects_database(harness, capsys) -> None:
    """The store itself, read as bytes: no row of one project is in the other's file.

    An admitted row carries the tool's description, and a grant, its ledger
    record and every proposal carry the principal, which tegh derives from the
    project path. Neither project's file holds the other's of either, no
    database was written for the home as a whole, and the audit of each file
    counts one project's rows.
    """
    assert main(["init"]) == 0
    first = _serving(harness, "widget", _FIRST_SAYS)
    second = _serving(harness, "gadget", _SECOND_SAYS)
    assert _wrap_as_reads(first) == 0
    assert _wrap_as_reads(second) == 0
    capsys.readouterr()
    store = store_of(harness)

    in_first = _database_files(store, first["project"])
    in_second = _database_files(store, second["project"])
    assert _FIRST_SAYS.encode() in in_first and _SECOND_SAYS.encode() in in_second
    assert _SECOND_SAYS.encode() not in in_first and _FIRST_SAYS.encode() not in in_second
    assert _principal(first["project"]) in in_first
    assert _principal(second["project"]) in in_second
    assert _principal(second["project"]) not in in_first
    assert _principal(first["project"]) not in in_second
    assert not store.shared_db_path.exists()

    # And as the base's own audit counts it, through `tegh posture`: each
    # project's database holds that project's grants and admitted rows, one of
    # each per tool, and not both projects' together.
    for project in (first, second):
        _assert_posture_audits_the_projects_own_database(project, capsys)


# ---------------------------------------------------------------------------
# The cut: a project wrapped under the earlier layout
# ---------------------------------------------------------------------------


def _named_like(path: Path) -> list[str]:
    """`path` and whatever sqlite would put beside it: `-wal`, `-shm`, `-journal`."""
    return sorted(entry.name for entry in path.parent.glob(path.name + "*"))


def _sealed(path: Path) -> dict:
    """Make `path` unreadable, and return what `_assert_untouched` holds it to."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.chmod(0o000)
    status = path.stat()
    return {
        "path": path,
        "sha256": digest,
        "file": (status.st_ino, status.st_size, status.st_mtime_ns, stat.S_IMODE(status.st_mode)),
        "beside": _named_like(path),
    }


def _assert_untouched(sealed: dict) -> None:
    """The same file (by inode), unwritten, with nothing new beside it, and the same bytes.

    The inode is what says it was not moved aside and put back, or replaced by
    a copy. `_named_like` is what says sqlite never opened it for writing: that
    leaves a `-wal` or a `-journal` beside the file.
    """
    path = sealed["path"]
    status = path.stat()
    assert (
        status.st_ino, status.st_size, status.st_mtime_ns, stat.S_IMODE(status.st_mode)
    ) == sealed["file"]
    assert _named_like(path) == sealed["beside"]
    path.chmod(0o600)
    try:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sealed["sha256"]
    finally:
        path.chmod(0o000)


def _as_an_earlier_tegh_left_it(harness: dict) -> dict:
    """Turn a project this tegh wrapped into one a tegh with a shared database wrapped.

    That tegh wrote the same files to the same places with two differences: the
    base's rows went to `<home>/tegh.db`, and there was no layout marker. So
    the database the wrap just wrote is moved there, which makes it a file that
    really does hold this project's admissions and grants, and the marker is
    removed. Returns the earlier database, sealed.
    """
    store, project = store_of(harness), harness["project"]
    database = store.db_path(project)
    assert sorted(p.name for p in database.parent.glob("store.db*")) == ["store.db"]
    database.rename(store.shared_db_path)
    store.layout_path(project).unlink()
    return _sealed(store.shared_db_path)


def _tegh_home_files(harness: dict, sealed: dict) -> dict[str, str]:
    """Every file of the tegh home but the sealed one, by content and mode."""
    root = harness["tegh_home"]
    return {
        str(path.relative_to(root)): as_fingerprinted(
            path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != sealed["path"]
    }


@pytest.fixture
def earlier(harness, capsys) -> dict:
    """A project wrapped under the earlier layout, its config rewritten."""
    assert main(["init"]) == 0
    project = _serving(harness, "widget", _FIRST_SAYS)
    before = project["claude_json"].read_bytes()
    assert _wrap_as_reads(project) == 0
    sealed = _as_an_earlier_tegh_left_it(project)
    capsys.readouterr()
    return {**project, "sealed": sealed, "config_before": before}


def _both_commands(harness: dict) -> tuple[str, str]:
    where = f"--project {harness['project']} --home {harness['tegh_home']}"
    return f"`tegh unwrap {where}`", f"`tegh wrap claude {where}`"


@pytest.mark.parametrize(
    ("argv", "status", "said_on", "starts"),
    [
        pytest.param(["gateway"], 2, "stderr", "REFUSED: ", id="gateway"),
        pytest.param(["call", _TOOL, "--args", _ENTRY], 2, "stderr", "REFUSED: ", id="call"),
        pytest.param(
            ["approve", "intent-0000000000000000", "--yes"], 2, "stderr", "REFUSED: ",
            id="approve",
        ),
        pytest.param(["diff"], 2, "stderr", "REFUSED: ", id="diff"),
        pytest.param(["status"], 0, "stdout", None, id="status"),
        pytest.param(["posture"], 0, "stdout", None, id="posture"),
    ],
)
def test_a_project_of_the_earlier_layout_is_refused_in_words(
    earlier, argv, status, said_on, starts
) -> None:
    """Every command that needs the project's admissions says where they went.

    The four that would serve, call, release or compare refuse, on one line of
    stderr and with nothing on stdout. `status` and `posture` are reports and
    still report, with the same words in them. All six name both commands of
    the way out, write nothing anywhere in the tegh home, and leave the
    earlier database alone.
    """
    files_before = _tegh_home_files(earlier, earlier["sealed"])

    done = _tegh(earlier, *argv)

    said = getattr(done, said_on)
    assert done.returncode == status, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    assert _EARLIER_LAYOUT in said, done.stdout + done.stderr
    for command in _both_commands(earlier):
        assert command in said, said
    if starts is not None:
        assert said.startswith(starts) and len(said.strip().splitlines()) == 1, said
        assert done.stdout == ""
    assert _tegh_home_files(earlier, earlier["sealed"]) == files_before
    assert not store_of(earlier).db_path(earlier["project"]).exists()
    _assert_untouched(earlier["sealed"])


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gateway"], id="gateway"),
        pytest.param(["call", _TOOL, "--args", _ENTRY], id="call"),
        pytest.param(["diff"], id="diff"),
    ],
)
def test_a_project_whose_marker_cannot_be_read_is_refused_and_not_served(
    harness, capsys, argv
) -> None:
    """With the marker unreadable tegh cannot tell the layout, and serves nothing on a guess.

    The project is one this tegh wrapped and would serve: its database is
    there with its admissions in it. A directory is put where the marker goes,
    and the gateway, a call and a diff each refuse on one line that names the
    file, where a tegh that took "cannot tell" for "this layout" would execute
    the call. Nothing in the tegh home is written.
    """
    assert main(["init"]) == 0
    project = _serving(harness, "widget", _FIRST_SAYS)
    assert _wrap_as_reads(project) == 0
    capsys.readouterr()
    store = store_of(project)
    marker = store.layout_path(project["project"])
    marker.unlink()
    marker.mkdir()
    files_before = _tegh_home_files(project, {"path": None})

    done = _tegh(project, *argv)

    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr, done.stderr
    assert done.stderr.startswith("REFUSED: "), done.stderr
    assert len(done.stderr.strip().splitlines()) == 1, done.stderr
    assert f"{marker} could not be read" in done.stderr, done.stderr
    assert done.stdout == ""
    assert _tegh_home_files(project, {"path": None}) == files_before


def test_unwrap_then_wrap_is_the_way_out_of_the_earlier_layout(earlier, capsys) -> None:
    """The two commands the refusal names, run in its order, end in a served project.

    The unwrap needs nothing from any database: it restores the harness config
    from the wrap backup, byte for byte. With the backup gone the refusal
    names the wrap alone, and says why. The wrap then reviews and admits into
    a database of the project's own, and a call executes under the definition
    just reviewed. The tape, which was always the project's, can be read
    throughout. The earlier database is untouched at every step.
    """
    store, project = store_of(earlier), earlier["project"]
    assert _tegh(earlier, "audit", "--verify").returncode == 0

    unwrapped = _tegh(earlier, "unwrap", "--yes")
    assert unwrapped.returncode == 0, unwrapped.stderr
    assert earlier["claude_json"].read_bytes() == earlier["config_before"]
    _assert_untouched(earlier["sealed"])
    # The refusal sent the developer here to be told what comes next. Neither
    # the plan nor the report may say the admissions carry over to that wrap:
    # they are in a database this tegh does not read, and both say so.
    plan_said, report_said = unwrapped.stdout.split("Quit the coding agent")
    assert "does not re-run" not in unwrapped.stdout, unwrapped.stdout
    assert "the admitted rows" not in unwrapped.stdout, unwrapped.stdout
    assert "a store database this tegh does not read" in plan_said, plan_said
    assert "nothing is carried over" in report_said, report_said
    assert _both_commands(earlier)[1] in report_said, report_said

    still = _tegh(earlier, "call", _TOOL, "--args", _ENTRY)
    assert still.returncode == 2 and _EARLIER_LAYOUT in still.stderr, still.stderr
    assert "left no backup" in still.stderr
    assert _both_commands(earlier)[1] in still.stderr

    assert _wrap_as_reads(earlier) == 0
    capsys.readouterr()
    assert store.layout_refusal(project) is None
    assert store.db_path(project).exists()
    assert main(["call", _TOOL, "--args", _ENTRY, "--project", str(project)]) == 0
    assert f"served under: {_FIRST_SAYS}" in capsys.readouterr().out
    # The earlier database is still in the home, and it is a real one holding
    # this project's earlier rows. The posture is the new database's alone.
    _assert_posture_audits_the_projects_own_database(earlier, capsys)
    _assert_untouched(earlier["sealed"])


def _stop_at_the_review(_monkeypatch) -> dict:
    def _interrupted(_question: str) -> str:
        raise KeyboardInterrupt

    return {"prompt": _interrupted}


def _stop_after_the_proposals(monkeypatch) -> dict:
    """Let the review and every proposal through, and fail the first file of the commit."""

    def _no_lock(*_args, **_kwargs) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "write_lock", _no_lock)
    return {}


@pytest.mark.parametrize(
    ("stop", "leaves_a_database"),
    [
        pytest.param(_stop_at_the_review, False, id="at-the-review"),
        pytest.param(_stop_after_the_proposals, True, id="after-the-proposals"),
    ],
)
def test_a_stopped_wrap_leaves_a_project_of_the_earlier_layout_as_it_was(
    harness, capsys, monkeypatch, stop, leaves_a_database
) -> None:
    """A wrap that is rolled back does not make the project one this tegh serves.

    The wrap has by then written its own manifest and the marker. The rollback
    puts the earlier manifest back and takes the marker away with it, so the
    project is refused in the same words as before.

    Stopped at the review, nothing has been written to any database and the
    project has none of its own. Stopped after the proposals it has one: the
    ceremony created it, and a rollback withdraws proposals and removes no
    database. That is the state the marker exists for. The file is there, it
    holds no admission and no grant, and it is not what says the project is
    served: every command still refuses in words, where one that took the
    database for the marker would start a gateway on it and answer each call
    as if the tool had never been admitted.
    """
    assert main(["init"]) == 0
    project = _serving(harness, "widget", _FIRST_SAYS)
    # `--no-rewrite`, so no gateway entry makes the next wrap an already-wrapped one.
    assert _wrap_as_reads(project, "--no-rewrite") == 0
    sealed = _as_an_earlier_tegh_left_it(project)
    store = store_of(project)
    manifest_before = store.manifest_path(project["project"]).read_bytes()
    capsys.readouterr()

    with monkeypatch.context() as patched:
        status = _wrap_as_reads(project, "--no-rewrite", **stop(patched))

    stopped = capsys.readouterr()
    assert status != 0 and "Nothing was wrapped" in stopped.err, stopped.err
    assert ("proposed  ledger/get_entry" in stopped.out) is leaves_a_database, stopped.out
    assert store.db_path(project["project"]).exists() is leaves_a_database
    assert store.manifest_path(project["project"]).read_bytes() == manifest_before
    assert not store.layout_path(project["project"]).exists()
    for argv in (["gateway"], ["call", _TOOL, "--args", _ENTRY], ["diff"]):
        refused = _tegh(project, *argv)
        assert refused.returncode == 2, (argv, refused.stdout, refused.stderr)
        assert refused.stderr.startswith("REFUSED: "), (argv, refused.stderr)
        assert _EARLIER_LAYOUT in refused.stderr, (argv, refused.stderr)
        assert refused.stdout == "", (argv, refused.stdout)
    _assert_untouched(sealed)


def test_a_new_project_wraps_normally_beside_an_earlier_database(harness, capsys) -> None:
    """An earlier database in the home is not a reason to refuse anything.

    What marks a project is its own manifest and marker. A project wrapped for
    the first time under a home that still holds `tegh.db` gets its own
    database and is served from it, and the earlier file is not opened: here it
    is not a database at all, and could not have been read as one.
    """
    assert main(["init"]) == 0
    shared = store_of(harness).shared_db_path
    shared.write_bytes(b"left here by an earlier tegh, and not a database")
    sealed = _sealed(shared)
    project = _serving(harness, "widget", _FIRST_SAYS)

    assert _wrap_as_reads(project) == 0
    capsys.readouterr()

    assert main(["call", _TOOL, "--args", _ENTRY, "--project", str(project["project"])]) == 0
    assert f"served under: {_FIRST_SAYS}" in capsys.readouterr().out
    assert store_of(harness).db_path(project["project"]).exists()
    _assert_posture_audits_the_projects_own_database(project, capsys)
    _assert_untouched(sealed)


# ---------------------------------------------------------------------------
# The cut, with the earlier database left readable
# ---------------------------------------------------------------------------
#
# Every test above seals the earlier database first, so a tegh that read it
# would fail on the open. That proves the file is not needed. It does not prove
# the file is not used when it can be, which is the state a real home is in.


def _left_readable(sealed: dict) -> dict:
    """Give the earlier database back its ordinary mode, and say what it is now."""
    path = sealed["path"]
    path.chmod(0o600)
    return _as_it_stands(path)


def _as_it_stands(path: Path) -> dict:
    status = path.stat()
    return {
        "file": (status.st_ino, status.st_size, status.st_mtime_ns, stat.S_IMODE(status.st_mode)),
        "beside": _named_like(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gateway"], id="gateway"),
        pytest.param(["call", _TOOL, "--args", _ENTRY], id="call"),
        pytest.param(["diff"], id="diff"),
    ],
)
def test_an_earlier_database_that_can_be_read_is_still_not_served_from(earlier, argv) -> None:
    """Readable, and holding this very project's admissions, it is refused all the same.

    The earlier database here is a real one: it is the file this project's wrap
    wrote, moved to where an earlier tegh kept it. A tegh that fell back to it
    when it could be opened would serve the call, and the project would look
    as if it had been carried over. It is refused, no database is opened for
    the project, and the earlier one is the same file with nothing beside it.
    """
    stands = _left_readable(earlier["sealed"])

    done = _tegh(earlier, *argv)

    assert done.returncode == 2, done.stdout + done.stderr
    assert done.stdout == "" and _EARLIER_LAYOUT in done.stderr, done.stderr
    assert not store_of(earlier).db_path(earlier["project"]).exists()
    assert _as_it_stands(earlier["sealed"]["path"]) == stands


def test_no_row_of_a_readable_earlier_database_reaches_another_projects(earlier, capsys) -> None:
    """A project wrapped beside a readable earlier database starts with nothing from it.

    The earlier database holds the first project's admitted definition and
    grants under the first project's principal. A second project, naming the
    same server, is wrapped for the first time in the same home. Its database
    holds its own definition and principal and no byte of the first project's
    of either, so no row was copied across, wholesale or one at a time.
    """
    stands = _left_readable(earlier["sealed"])
    carried = earlier["sealed"]["path"].read_bytes()
    assert _FIRST_SAYS.encode() in carried and _principal(earlier["project"]) in carried
    second = _serving(earlier, "gadget", _SECOND_SAYS)

    assert _wrap_as_reads(second) == 0
    capsys.readouterr()

    in_second = _database_files(store_of(second), second["project"])
    assert _SECOND_SAYS.encode() in in_second and _principal(second["project"]) in in_second
    assert _FIRST_SAYS.encode() not in in_second
    assert _principal(earlier["project"]) not in in_second
    assert main(["call", _TOOL, "--args", _ENTRY, "--project", str(second["project"])]) == 0
    assert f"served under: {_SECOND_SAYS}" in capsys.readouterr().out
    assert _as_it_stands(earlier["sealed"]["path"]) == stands


def test_the_layout_marker_is_written_whole_or_not_at_all(tmp_path, monkeypatch) -> None:
    """A wrap stopped while writing the marker leaves no marker, never part of one.

    The rename is the write. With it failing, the project has no marker and is
    refused as an earlier wrap would be, which is the state the refusal's own
    commands get it out of. A name left by a tegh that died here does not stop
    the next one.
    """
    store = TeghStore(home=tmp_path / "tegh-home")
    project = tmp_path / "project"
    marker = store.layout_path(project)

    def _fails(*_args, **_kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr("tegh.store.os.replace", _fails)
    with pytest.raises(OSError):
        store.mark_layout(project)
    assert not marker.exists()
    monkeypatch.undo()

    store.mark_layout(project)
    assert marker.read_text(encoding="utf-8") == "2\n"
    assert sorted(entry.name for entry in marker.parent.iterdir()) == ["layout"]
