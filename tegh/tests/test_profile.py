"""The managed settings profile, and the page that shows it to a person.

`tegh/profile.py` defines the document; `docs/managed-profile.md` prints it and
says what each key does. The page is what someone copies into a managed file by
hand, so the two are held equal here rather than by review.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tegh.harnesses.claude_code import hook_command, hook_site
from tegh.hooksite import plan_hook, write_hook
from tegh.profile import POST_TOOL_USE, PRE_TOOL_USE, managed_profile

DOC = Path(__file__).resolve().parents[2] / "docs" / "managed-profile.md"
COMMAND = "/Users/you/venv/bin/tegh hook --project /Users/you/tegh-demo --home /Users/you/.tegh"

#: The six policy keys and the values the matrix cells tested, at the placement
#: they were tested in (see the module docstring of `tegh.profile`).
LOCKS = {
    "allowManagedHooksOnly": True,
    "allowManagedPermissionRulesOnly": True,
    "disableAutoMode": "disable",
    "permissions": {"disableBypassPermissionsMode": "disable"},
    "allowedHttpHookUrls": [],
    "httpHookAllowedEnvVars": [],
}


def test_the_six_policy_keys_are_present_with_the_tested_values():
    profile = managed_profile(COMMAND)

    assert {key: profile[key] for key in LOCKS} == LOCKS
    assert set(profile) == {"hooks", *LOCKS}


def test_only_the_pre_tool_use_entry_blocks_on_failure():
    hooks = managed_profile(COMMAND)["hooks"]

    assert set(hooks) == {PRE_TOOL_USE, POST_TOOL_USE}
    (pre,) = hooks[PRE_TOOL_USE]
    (post,) = hooks[POST_TOOL_USE]
    for group in (pre, post):
        (hook,) = group["hooks"]
        assert hook["type"] == "command" and hook["command"] == COMMAND
    assert pre["hooks"][0]["onFailure"] == "block"
    assert "onFailure" not in post["hooks"][0]


def test_the_allowlists_carry_what_the_caller_names():
    profile = managed_profile(
        COMMAND,
        decider_urls=("http://127.0.0.1:8720/*",),
        token_env_vars=("TEGH_DECIDER_TOKEN",),
    )

    assert profile["allowedHttpHookUrls"] == ["http://127.0.0.1:8720/*"]
    assert profile["httpHookAllowedEnvVars"] == ["TEGH_DECIDER_TOKEN"]


def test_the_profile_carries_the_entries_a_wrap_writes(tmp_path):
    """The profile builds its two groups itself, so this is what keeps them from
    drifting away from the entries `tegh wrap` puts in the local settings file:
    under `allowManagedHooksOnly` the wrap's entries are dropped and only these
    run."""
    project = tmp_path / "widget"
    project.mkdir()
    command = hook_command(project, launcher=["tegh"], home=tmp_path / "tegh")
    site = hook_site(project)
    write_hook(plan_hook(site, command))

    written = json.loads(site.path.read_text(encoding="utf-8"))["hooks"]

    assert written == managed_profile(command)["hooks"]


def test_the_page_shows_exactly_what_the_function_returns():
    blocks = re.findall(r"```json\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 1, "the page should show the profile once"
    (shown,) = blocks
    command = json.loads(shown)["hooks"][PRE_TOOL_USE][0]["hooks"][0]["command"]

    assert command == COMMAND
    assert shown == json.dumps(managed_profile(command), indent=2)
