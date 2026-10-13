"""The managed settings profile: the Claude Code policy under which tegh's hooks hold.

`tegh wrap` puts its two hook entries in the project's local settings file
(`tegh/hooksite.py`), where anything the developer's user can write can switch
them off or let a call around them. A managed settings file is the one place a
lower scope cannot reach, and this module DEFINES the document tegh would put
there. Nothing installs it yet: writing the managed file needs root, and the
phase that deploys it to a fleet is a later one. `docs/managed-profile.md`
explains each key for the person who installs it by hand.

Every key below is here because a cell of a matrix run on a disposable macOS
runner on 2026-10-12 (Claude Code 2.1.295) showed what it does, and its comment
names the cells. The matrix and its verdict table are recorded in
`docs/references/harnesses/claude-code.md` §4, "Managed settings, observed
2026-10-12". The keys sit where the matrix put them, which is not always where
the vendor's pages put them: the matrix wrote `allowManagedPermissionRulesOnly`
and `disableAutoMode` at the top level and `disableBypassPermissionsMode` under
`permissions`, and those are the placements the cells tested.

What the profile does not do: it carries no `managed-mcp.json` (that file takes
exclusive control of MCP servers, c07, and would unload the gateway a wrap
points at), it is not delivered as server-managed settings or by an MDM
profile (neither was tested), and it does not stop the developer's user from
overwriting the tegh launcher the hook command runs, which sits outside the
managed directory.
"""

from __future__ import annotations

from typing import Any, Sequence

from tegh.hooksite import POST_TOOL_USE, PRE_TOOL_USE, hook_entry


def _group(command: str, event: str) -> dict[str, Any]:
    """The matcher group a wrap writes under `event`, from the one builder a wrap uses.

    Only the `PreToolUse` handler carries `"onFailure": "block"`: a hook that
    fails (crashes, hangs past its timeout) would otherwise let the call
    proceed. With that key the call was refused when the decider was down and
    when it hung (c08b, c08d); without it the call ran (c08a, c08c). It acts
    only on a hook that runs.
    """
    return hook_entry(command, event)


def managed_profile(
    hook_command: str,
    *,
    decider_urls: Sequence[str] = (),
    token_env_vars: Sequence[str] = (),
) -> dict[str, Any]:
    """The managed settings document for one project's `tegh hook` command.

    `hook_command` is the string a wrap writes into the project's local
    settings (`tegh.harnesses.claude_code.hook_command`), so the profile as
    defined names one project and one tegh home. A profile for a fleet of
    projects is the deploying phase's to define.

    `allowedHttpHookUrls` and `httpHookAllowedEnvVars` are set even though
    tegh's hook is a command hook, which neither list governs (an HTTP hook runs
    only if its URL matches the merged allowlist; the reference's §3, "HTTP
    hooks"). When the managed file leaves `allowedHttpHookUrls` unset, a list in
    a lower scope that misses the decider blocks a managed HTTP hook (c06b to
    c06d) and `allowManagedHooksOnly` does not prevent it (c06e); a lower
    `httpHookAllowedEnvVars` that omits the token's variable strips the header
    (c06g). When the managed file sets both arrays itself they merge with any
    lower list as a union, and a hostile lower list neither blocks nor strips
    (c06h, c06i). So a decider reached over HTTP is named in `decider_urls`, its
    token's variable in `token_env_vars`, and both keys belong to this file from
    the start. Both default to empty, and an empty managed list was not among
    the cells.
    """
    return {
        # The wrap's two entries, carried by the managed file so that no lower
        # scope can remove them. The matrix's managed hook was an HTTP hook; it
        # stayed in place under `disableAllHooks` in the project file, the local
        # file and `--settings` (c05a to c05c), under `--bare` and `--safe-mode`
        # (c11a, c11b), and the runner's user could not overwrite, add to or
        # rename the policy file (c09). A managed COMMAND hook was not tested.
        "hooks": {
            PRE_TOOL_USE: [_group(hook_command, PRE_TOOL_USE)],
            POST_TOOL_USE: [_group(hook_command, POST_TOOL_USE)],
        },
        # Drops a project or user hook and keeps the managed one (c04a to c04d).
        # Under it the wrap's own entries in the project's local file are
        # dropped too, which is why the profile carries them above.
        "allowManagedHooksOnly": True,
        # Removes every allow rule from `--allowedTools`, the local, project and
        # user files and `--settings` (the ten c02 cells), so the broker's answer
        # is the only approval a call gets from below the managed file.
        "allowManagedPermissionRulesOnly": True,
        # With no decider and no allow rule, `auto` ran the call on haiku and on
        # sonnet (c01d, c01g); this key refused it (c01e, c01h).
        "disableAutoMode": "disable",
        "permissions": {
            # `--dangerously-skip-permissions`, `--permission-mode
            # bypassPermissions` and a project `defaultMode` are ignored and the
            # session starts in `default` (c03b to c03d; without it, c03a).
            "disableBypassPermissionsMode": "disable",
        },
        "allowedHttpHookUrls": list(decider_urls),
        "httpHookAllowedEnvVars": list(token_env_vars),
    }
