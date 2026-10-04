"""How tegh replaces a harness config file: same mode, and no temporary left behind.

A harness config can hold a credential, before a wrap and again after an unwrap.
Two properties of the write follow from that, and each has a way to be false:

1. **The file keeps its permission bits.** Replacing a file by writing a new one
   and renaming it gives the new file whatever mode it was created with. Created
   under the umask, a 0600 `~/.claude.json` comes back 0644 holding the
   credential the unwrap just put in it. Every case here sets a umask under
   which that way of writing produces the WRONG mode, so the test cannot pass by
   the umask happening to agree.
2. **A write that fails leaves no `<config>.tegh-tmp`.** On an unwrap that file
   holds the resolved credential.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from tegh import interpose, unwrap
from tegh.tests.wrapping import (
    LEDGER_FIELD,
    LEDGER_SECRET,
    files_holding,
    give_ledger,
    interposed,
    scripted,
    store_of,
    unwrap_cli,
    wrap_answering,
)

_LEDGER = {"ledger": {"command": "ledger-server", "env": {"LEDGER_API_KEY": "not-a-real-credential-one"}}}
_VENDOR = {"vendor": {"command": "vendor-server", "env": {"VENDOR_TOKEN": "not-a-real-credential-two"}}}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask():
    """Set the process umask for one test, and put it back."""
    previous = os.umask(0o022)

    def _set(mask: int) -> None:
        os.umask(mask)

    yield _set
    os.umask(previous)


def _temporaries(root: Path) -> list[Path]:
    return sorted(root.rglob("*.tegh-tmp"))


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "mask"),
    [
        # The case that matters: owner-only, under the usual umask.
        pytest.param(0o600, 0o022, id="0600-stays-0600"),
        # Preserved, and not merely tightened: a strict umask must not narrow it.
        pytest.param(0o644, 0o077, id="0644-stays-0644"),
        pytest.param(0o664, 0o022, id="0664-stays-0664"),
    ],
)
def test_a_config_keeps_its_mode_through_wrap_and_unwrap(
    tmp_path, monkeypatch, capsys, umask, mode: int, mask: int
) -> None:
    umask(mask)
    state = interposed(
        tmp_path, monkeypatch, local=_LEDGER, project_scope=_VENDOR, config_mode=mode
    )
    configs = (state["claude_json"], state["project_mcp"])
    # The wrap rewrote both files: the credentials are gone from them.
    assert not [path for path in configs if b"not-a-real-credential" in path.read_bytes()]
    assert [_mode(path) for path in configs] == [mode, mode], "the wrap changed a mode"

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err

    assert [path.read_bytes() for path in configs] == list(state["originals"].values())
    assert [_mode(path) for path in configs] == [mode, mode], "the unwrap changed a mode"


def test_a_real_wrap_and_unwrap_leave_an_owner_only_config_owner_only(
    harness, capsys, umask
) -> None:
    """The same claim through the real `tegh wrap`, ceremony and all."""
    pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")
    from tegh.cli import main  # noqa: PLC0415 - after the skip, as the wrap needs it

    before = give_ledger(harness, env={LEDGER_FIELD: LEDGER_SECRET})
    harness["claude_json"].chmod(0o600)
    umask(0o022)

    assert main(["init"]) == 0
    assert wrap_answering(harness, scripted([""])) == 0
    assert LEDGER_SECRET not in harness["claude_json"].read_text(encoding="utf-8")
    assert _mode(harness["claude_json"]) == 0o600

    assert unwrap_cli(harness, "--yes") == unwrap.EXIT_OK
    assert harness["claude_json"].read_bytes() == before
    assert _mode(harness["claude_json"]) == 0o600
    assert _mode(store_of(harness).secrets_path(harness["project"])) == 0o600


def test_a_config_tegh_has_to_create_is_owner_only(
    tmp_path, monkeypatch, capsys, umask
) -> None:
    """A `.mcp.json` deleted while wrapped is written back, credential and all.

    There is no mode to preserve, so it is 0600, and under a umask of zero that
    is tegh's doing and nobody else's.
    """
    state = interposed(tmp_path, monkeypatch, local=_LEDGER, project_scope=_VENDOR)
    state["project_mcp"].unlink()
    umask(0o000)

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err

    assert state["project_mcp"].read_bytes() == state["originals"][state["project_mcp"]]
    assert _mode(state["project_mcp"]) == 0o600


# ---------------------------------------------------------------------------
# The temporary file
# ---------------------------------------------------------------------------


def _replace_fails_for(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    """The rename of `config`'s temporary file fails; every other rename is real."""
    real = os.replace

    def _replace(source, target, **kwargs):
        if Path(target) == config:
            raise OSError(errno.EIO, "Input/output error", str(target))
        return real(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", _replace)


def test_a_write_that_fails_leaves_no_temporary_file(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    _replace_fails_for(monkeypatch, config)

    with pytest.raises(OSError, match="Input/output error"):
        interpose._replace_file(config, '{"mcpServers": {}}')

    assert config.read_text(encoding="utf-8") == "{}"
    assert _temporaries(tmp_path) == []


def test_a_failed_unwrap_leaves_no_temporary_file_holding_the_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    (secret,) = state["secrets"]
    _replace_fails_for(monkeypatch, state["claude_json"])

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_NOT_UNWRAPPED
    captured = capsys.readouterr()

    assert _temporaries(tmp_path) == []
    # One copy, where it was before the attempt.
    assert files_holding(tmp_path, secret) == [state["store"].secrets_path(state["project"])]
    assert "the harness config was not restored" in captured.err
    assert secret not in captured.out + captured.err


def test_a_stale_temporary_file_does_not_block_the_next_write(
    tmp_path, monkeypatch, capsys
) -> None:
    """An earlier tegh died between creating its temporary file and renaming it.

    The exclusive create that sets the mode would refuse over that file on
    every later run, so it is removed first.
    """
    state = interposed(tmp_path, monkeypatch, local=_LEDGER)
    stale = state["claude_json"].with_name(state["claude_json"].name + ".tegh-tmp")
    stale.write_text("left by a process that died", encoding="utf-8")

    assert unwrap_cli(state, "--yes") == unwrap.EXIT_OK, capsys.readouterr().err

    assert state["claude_json"].read_bytes() == state["originals"][state["claude_json"]]
    assert _temporaries(tmp_path) == []
