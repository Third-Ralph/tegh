"""A wrap's backup is on disk whole, or it is not there.

The backup is written before the first byte of the harness config changes, and
it is what a wrap that dies between the two is recovered by: the next `tegh
wrap` sees a backup and names `tegh unwrap`, which restores from it. Both
commands read the same file, so a backup that exists and does not parse is the
one state neither leads out of: the wrap refuses because there is a backup, and
the unwrap refuses because it cannot read it.

A file written in place goes through that state: it is created empty and filled
afterwards. So this stops the write at that moment, both ways it can stop. The
wrap is killed outright as the file its backup goes into is opened, in a real
process, and what a person does next is then done. And the write is made to
fail before it is finished, in process.
"""

from __future__ import annotations

import errno
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tegh import interpose
from tegh.cli import main
from tegh.interpose import WrapBackup
from tegh.tests.wrapping import (
    REPO,
    _wrap_process_env,
    store_of,
    unwrap_cli,
    wrap_admit_all,
    wrap_argv,
)

#: `tegh wrap` in a process that kills itself as the file its backup's bytes go
#: into is opened for writing: created, and still empty. Whichever file that
#: is, the backup's own or one beside it, is the write's business; a file
#: opened by descriptor is known by the path the descriptor was opened on.
_KILLED_AS_THE_BACKUP_IS_OPENED = """
import io, os, signal, sys
from pathlib import Path
from tegh.cli import main

opened, os_open, by_descriptor = io.open, os.open, {}

def _os_open(path, *args, **kwargs):
    descriptor = os_open(path, *args, **kwargs)
    by_descriptor[descriptor] = str(path)
    return descriptor

def _open(file, mode="r", *args, **kwargs):
    stream = opened(file, mode, *args, **kwargs)
    name = by_descriptor.get(file, "") if isinstance(file, int) else str(file)
    if "w" in mode and Path(name).name.startswith("wrap-backup.json"):
        os.kill(os.getpid(), signal.SIGKILL)
    return stream

io.open, os.open = _open, _os_open
raise SystemExit(main(sys.argv[1:]))
"""


def _assert_whole_or_absent(backup_path: Path) -> None:
    if not backup_path.exists():
        return
    raw = backup_path.read_text(encoding="utf-8")
    try:
        WrapBackup.from_json(raw)
    except interpose.BackupUnreadable as exc:
        pytest.fail(
            f"the wrap left a backup of {len(raw)} byte(s) that cannot be read "
            f"({exc}): `tegh wrap` refuses because it is there, and `tegh unwrap` "
            "refuses because it does not parse"
        )


def test_a_wrap_killed_as_it_writes_its_backup_leaves_none_and_can_be_run_again(
    harness, capsys
) -> None:
    pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")
    assert main(["init"]) == 0
    config_before = harness["claude_json"].read_bytes()
    backup_path = store_of(harness).backup_path(harness["project"])

    killed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _KILLED_AS_THE_BACKUP_IS_OPENED,
         *wrap_argv(harness, "--admit-all")],
        # Unbuffered: a killed process takes what it had not yet written with it.
        env={**_wrap_process_env(harness), "PYTHONUNBUFFERED": "1"}, cwd=REPO,
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=False, timeout=120,
    )

    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert "proposed  ledger/list_entries" in killed.stdout, "killed before the backup"
    assert harness["claude_json"].read_bytes() == config_before
    _assert_whole_or_absent(backup_path)

    # What a person does next: wrap again, unwrapping first if the wrap says to.
    capsys.readouterr()
    status = wrap_admit_all(harness)
    if status == 2:
        refusal = capsys.readouterr().err
        assert "tegh unwrap --project" in refusal, refusal
        assert unwrap_cli(harness, "--yes") == 0, capsys.readouterr().err
        status = wrap_admit_all(harness)
    assert status == 0, capsys.readouterr().err
    WrapBackup.from_json(backup_path.read_text(encoding="utf-8"))
    assert sorted(path.name for path in backup_path.parent.glob("wrap-backup*")) == [
        backup_path.name
    ], "the killed wrap's temporary file outlived the wrap that followed it"


def _backup(wrapped_at: str) -> WrapBackup:
    return WrapBackup(project="/work/widget", harness="claude-code", wrapped_at=wrapped_at, sites=[])


@pytest.mark.parametrize("earlier", [None, "2026-10-04T00:00:00+00:00"], ids=["none", "an-earlier-one"])
def test_a_backup_write_that_fails_leaves_what_was_there(tmp_path, monkeypatch, earlier) -> None:
    """Stopped at the last step of the write: no backup, or the complete earlier one."""
    backup_path = tmp_path / "projects" / "widget" / "wrap-backup.json"
    if earlier:
        interpose.write_backup(backup_path, _backup(earlier))
    before = backup_path.read_bytes() if earlier else None

    def _full(source, target):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as patched:
        patched.setattr(interpose.os, "replace", _full)
        with pytest.raises(OSError):
            interpose.write_backup(backup_path, _backup("2026-10-05T00:00:00+00:00"))
    assert (backup_path.read_bytes() if backup_path.exists() else None) == before
    assert sorted(path.name for path in backup_path.parent.iterdir()) == (
        [backup_path.name] if earlier else []
    )

    interpose.write_backup(backup_path, _backup("2026-10-05T00:00:00+00:00"))
    written = WrapBackup.from_json(backup_path.read_text(encoding="utf-8"))
    assert written.wrapped_at == "2026-10-05T00:00:00+00:00"
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
