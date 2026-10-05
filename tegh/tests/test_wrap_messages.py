"""What `tegh wrap` prints is followed word for word, so it has to work that way.

One thing a message can get wrong without any file being wrong: a ceremony's
failure arrives as a Python traceback folded into a sentence.

It is driven through the real command.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="a real wrap snapshots a real MCP server")

from tegh.cli import _parse_args, main, wrap_command  # noqa: E402
from tegh.tests.wrapping import scripted, store_of, wrap_argv  # noqa: E402
from tegh.transaction import WrapTransaction  # noqa: E402

# ---------------------------------------------------------------------------
# A ceremony that fails for a real reason
# ---------------------------------------------------------------------------


def test_a_ceremony_that_crashes_is_one_line_and_its_traceback_is_shown_apart(
    harness, capsys
) -> None:
    """A store database that cannot be written, which no ceremony expects.

    The base's command dies with a traceback. The line a stopped wrap prints
    carries the error that traceback ends in and says where the rest is; the
    rest is on stdout, indented, with everything else the wrap printed.
    """
    assert main(["init"]) == 0
    assert main(wrap_argv(harness, "--admit-all", "--no-rewrite")) == 0
    database = store_of(harness).db_path
    database.chmod(0o444)
    capsys.readouterr()
    try:
        status = wrap_command(_parse_args(wrap_argv(harness, "--admit-all")), prompt=scripted([]))
    finally:
        database.chmod(0o644)
    said = capsys.readouterr()

    failure = said.err.strip()
    assert status == 1 and failure.startswith("FAILED: "), failure
    assert len(failure.splitlines()) == 1, failure
    assert "Traceback" not in failure and 'File "' not in failure, failure
    assert "readonly database (all it said is printed above)" in failure
    assert "are as they were before this command" in failure
    shown = said.out.split("failed. It said:")[1]
    assert "     Traceback (most recent call last):" in shown
    assert "readonly database" in shown


# ---------------------------------------------------------------------------
# A process the rollback could not stop
# ---------------------------------------------------------------------------


def test_a_process_that_could_not_be_stopped_is_named_in_the_one_line(
    harness, capsys, monkeypatch
) -> None:
    """`test_transaction_signals.py` shows the rollback surviving it; this is the report."""
    assert main(["init"]) == 0
    monkeypatch.setattr(
        WrapTransaction, "stop_children", lambda self: self.not_stopped.extend([4242, 4243])
    )

    def _nobody_answers(_question: str) -> str:
        raise EOFError

    status = wrap_command(_parse_args(wrap_argv(harness)), prompt=_nobody_answers)

    failure = capsys.readouterr().err.strip()
    assert status == 2 and len(failure.splitlines()) == 1, failure
    assert (
        "tegh could not stop every process this wrap started, and these may still "
        "be running: pid 4242, 4243." in failure
    )
