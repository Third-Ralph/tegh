#!/usr/bin/env python3
"""Check that the release being cut has a recorded posture claim review.

`tegh posture` prints claims, and `tegh/tests/test_posture_citations.py` proves
each one's citation resolves. Whether the claim is true is a person's reading
of it beside the code, recorded in `docs/posture-review.md`. This script is
the release's check that a reading was recorded for this version, on this
platform pin:

    python scripts/check_posture_review.py

It passes when the record has an entry whose `Release:` line names the version
in pyproject.toml and whose `Platform:` line names the `safe-agents` version
pyproject.toml pins. The exit code is 1 otherwise.

What it does not establish: that the review was done well, or at all. It
establishes that someone put a dated entry for this release in a tracked file,
where `git blame` names them.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "posture-review.md"
PYPROJECT = ROOT / "pyproject.toml"

PLATFORM_PACKAGE = "safe-agents"

#: One entry per review, opened by a second-level heading that starts with its
#: date. The record's other headings are not entries.
_ENTRY_HEADING = re.compile(r"^## (?P<title>\d{4}-\d{2}-\d{2}.*)$", re.MULTILINE)
_RELEASE = re.compile(r"^Release:\s*(?P<value>\S+)", re.MULTILINE)
_PLATFORM = re.compile(
    rf"^Platform:\s*{re.escape(PLATFORM_PACKAGE)}\s+(?P<value>\S+)", re.MULTILINE
)
_PIN = re.compile(rf"^{re.escape(PLATFORM_PACKAGE)}(?:\[[^\]]*\])?==(?P<value>\S+)$")


class ReviewCheckError(Exception):
    """The record or the project metadata cannot answer the question."""


@dataclass(frozen=True)
class Entry:
    title: str
    release: str | None
    platform: str | None


def parse_entries(record: str) -> list[Entry]:
    """Every review entry in the record, in file order."""
    headings = list(_ENTRY_HEADING.finditer(record))
    entries = []
    for heading, following in zip(headings, [*headings[1:], None]):
        body = record[heading.end() : following.start() if following else len(record)]
        release = _RELEASE.search(body)
        platform = _PLATFORM.search(body)
        entries.append(
            Entry(
                title=heading.group("title").strip(),
                release=release.group("value") if release else None,
                platform=platform.group("value") if platform else None,
            )
        )
    return entries


def project_versions(pyproject: dict) -> tuple[str, str]:
    """The version being released and the platform version it pins."""
    project = pyproject["project"]
    for dependency in project.get("dependencies", ()):
        pin = _PIN.match(dependency.replace(" ", ""))
        if pin:
            return project["version"], pin.group("value")
    raise ReviewCheckError(
        f"pyproject.toml pins no exact `{PLATFORM_PACKAGE}==` version, so there is "
        "no platform version for a review to name"
    )


def missing_review(entries: list[Entry], version: str, platform: str) -> str | None:
    """Why this release has no recorded review, or None when it has one."""
    for_release = [entry for entry in entries if entry.release == version]
    if not for_release:
        return (
            f"docs/posture-review.md has no entry with `Release: {version}`. Read "
            "each posture claim beside the code it describes, record the result "
            "there, and run this again."
        )
    if not any(entry.platform == platform for entry in for_release):
        named = ", ".join(sorted({entry.platform or "none" for entry in for_release}))
        return (
            f"the review recorded for {version} names platform {named}, and "
            f"pyproject.toml pins {PLATFORM_PACKAGE} {platform}. Citations into the "
            "platform move with the pin, so the review is repeated on the new one."
        )
    return None


def main() -> int:
    try:
        version, platform = project_versions(tomllib.loads(PYPROJECT.read_text("utf-8")))
        record = RECORD.read_text("utf-8")
    except (OSError, KeyError, tomllib.TOMLDecodeError, ReviewCheckError) as exc:
        print(f"check_posture_review: {exc}", file=sys.stderr)
        return 1
    reason = missing_review(parse_entries(record), version, platform)
    if reason:
        print(f"check_posture_review: {reason}", file=sys.stderr)
        return 1
    print(f"posture review recorded for {version} on {PLATFORM_PACKAGE} {platform}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
