"""The release's posture-review check, one refusal at a time.

`scripts/check_posture_review.py` is what stops a release whose posture claims
nobody recorded reading. A rule that quietly stopped matching would let one
through with a green check, so each case pins one record it must accept or one
it must refuse, and what the refusal says.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_posture_review.py"


def _load():
    # `scripts/` is not a package, so the module is loaded by path, and it has
    # to be in sys.modules before it executes for its dataclass to resolve.
    spec = importlib.util.spec_from_file_location("check_posture_review", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load()

_FIRST_RUN = "## 2026-01-01\n\nRelease: none\nPlatform: safe-agents 1.0.0\n"
_FOR_RELEASE = "## 2026-02-01\n\nRelease: 1.2.0\nPlatform: safe-agents 1.1.0\n"


@pytest.mark.parametrize(
    ("record", "version", "platform", "refusal"),
    [
        (_FOR_RELEASE + _FIRST_RUN, "1.2.0", "1.1.0", None),
        (_FIRST_RUN, "1.2.0", "1.0.0", "no entry with `Release: 1.2.0`"),
        ("# Posture claim reviews\n", "1.2.0", "1.1.0", "no entry with `Release: 1.2.0`"),
        # A heading that is not a dated entry holds no review, whatever it says.
        (
            "## How a review is done\n\nRelease: 1.2.0\nPlatform: safe-agents 1.1.0\n",
            "1.2.0",
            "1.1.0",
            "no entry with `Release: 1.2.0`",
        ),
        (_FOR_RELEASE, "1.2.0", "1.3.0", "names platform 1.1.0"),
        ("## 2026-03-01\n\nRelease: 1.2.0\n", "1.2.0", "1.1.0", "names platform none"),
        # A version mentioned in an entry's prose is not that entry's release.
        (_FIRST_RUN + "\nRead after Release: 1.2.0 was cut.\n", "1.2.0", "1.0.0", "no entry"),
    ],
)
def test_a_release_needs_an_entry_naming_it_and_its_platform(
    record, version, platform, refusal
):
    reason = check.missing_review(check.parse_entries(record), version, platform)
    if refusal is None:
        assert reason is None
    else:
        assert refusal in reason


@pytest.mark.parametrize(
    ("dependencies", "platform"),
    [
        (["safe-agents[mcp]==0.75.0"], "0.75.0"),
        (["pyyaml>=6", "safe-agents == 1.0.0"], "1.0.0"),
    ],
)
def test_the_platform_version_is_read_from_the_exact_pin(dependencies, platform):
    pyproject = {"project": {"version": "1.2.0", "dependencies": dependencies}}
    assert check.project_versions(pyproject) == ("1.2.0", platform)


def test_a_range_is_not_a_platform_version():
    pyproject = {"project": {"version": "1.2.0", "dependencies": ["safe-agents>=0.75"]}}
    with pytest.raises(check.ReviewCheckError, match="pins no exact"):
        check.project_versions(pyproject)


def test_the_tracked_record_parses_and_every_entry_names_a_release_and_platform():
    entries = check.parse_entries(check.RECORD.read_text("utf-8"))
    assert entries, "docs/posture-review.md holds no review entry"
    for entry in entries:
        assert entry.release, f"entry {entry.title!r} has no `Release:` line"
        assert entry.platform, f"entry {entry.title!r} has no `Platform:` line"
