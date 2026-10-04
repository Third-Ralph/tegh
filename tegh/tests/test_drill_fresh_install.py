"""Run the fresh-install drill under pytest, so the suite notices when it rots.

`drill_fresh_install.py` is a standalone script because the macOS job runs it
against a non-editable install with no pytest. This runs the same script against
the `tegh` beside this interpreter. Every assertion is the script's own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

DRILL = Path(__file__).with_name("drill_fresh_install.py")


def test_the_fresh_install_drill_passes() -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(DRILL)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
