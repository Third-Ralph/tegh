"""hooksite.py — the one hook entry a wrap adds to a harness's settings, and takes out again.

`interpose.py` rewrites the blocks a harness loads MCP servers from. This module
is its sibling for the second thing `tegh wrap claude` writes into a harness
config: one `PostToolUse` command hook that runs `tegh hook` after each of the
harness's built-in tool calls (`hook.py`). The adapter supplies WHERE
(:class:`HookSite`) and the entry; how the settings file is read, edited, backed
up and restored is here.

The wrap contract of `interpose.py` holds here unchanged, clause for clause:

**The file is round-trip checked before any edit.** The settings file is parsed
and re-serialized with `interpose`'s one serializer, unmodified, and compared
with what is on disk; a mismatch refuses with `ConfigFormatUnreproducible`. One
allowance, and only for this file: a single trailing newline after the
serialized document is accepted and kept. Claude Code writes its settings files
that way (every settings file written by a current release that was checked on
one developer machine on 2026-10-06 ended in exactly `}` and one newline, and
each reproduced byte for byte under that rule), so refusing it would refuse the
file the harness itself writes. A file tegh creates gets the same ending.

**Only tegh's one entry is touched.** One matcher group is appended to
`hooks.PostToolUse`. No existing hook, matcher or key is read into the change or
written back differently. An identical entry already there is not added twice.

**Absent is put back as absent.** The backup records whether the file, the
`hooks` object, the `PostToolUse` list and the file's directory existed, so an
unwrap that takes tegh's entry out leaves no `{}`, no empty list and no empty
file or directory that the wrap made. With nothing written in between, that is
the file byte for byte as it was.

**Only what the wrap added comes out.** The entry is found by its command string,
which the backup records. A hook the person or the harness added since the wrap
is kept, beside it or in the same group.

It is the harness's LOCAL settings file on purpose: the project's
`.claude/settings.json` is committable, and a hook naming one developer's tegh
home and launcher does not belong in a shared repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from tegh.interpose import (
    ConfigChangedSincePlan,
    ConfigFormatUnreproducible,
    InterposeError,
    RestoreUnverified,
    _render,
    _replace_file,
)

#: The one event tegh hooks. The hook observes after the fact; nothing in tegh
#: registers for any event that runs before a tool call.
HOOK_EVENT = "PostToolUse"

#: The key a settings file keeps its hooks under, by event.
_HOOKS_KEY = "hooks"

#: How long the harness may wait for one `tegh hook` run, in seconds. The hook's
#: own post gives up after two, and a cold interpreter start is the rest.
HOOK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class HookSite:
    """One settings file a harness reads hooks from."""

    path: Path
    event: str = HOOK_EVENT

    @property
    def label(self) -> str:
        return f"hooks:{self.path}"


def hook_entry(command: str) -> dict[str, Any]:
    """The matcher group a wrap appends: every tool, one command hook, observe only."""
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}],
    }


# ---------------------------------------------------------------------------
# Reading, with the format guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """A settings file as read: absent (`document is None`), or parsed and checked."""

    document: Optional[dict[str, Any]]
    newline: bool = True


def _load(site: HookSite) -> _Loaded:
    """Read the settings file, refusing one tegh could not write back unchanged."""
    if not site.path.exists():
        return _Loaded(None)
    try:
        raw = site.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InterposeError(
            f"{site.path} is not UTF-8 text (byte {exc.start} does not decode) — "
            "refusing to add a hook to a settings file tegh cannot read."
        ) from None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InterposeError(
            f"{site.path} is not valid JSON ({exc.msg}, line {exc.lineno}) — "
            "refusing to add a hook to a settings file tegh cannot parse."
        ) from None
    rendered = _render(document)
    if raw not in (rendered, rendered + "\n"):
        raise ConfigFormatUnreproducible(
            f"{site.path} does not round-trip through tegh's JSON serializer "
            "byte-for-byte, so writing it back would reformat the whole file to "
            "add one hook. Refusing. (tegh writes `indent=2, ensure_ascii=False`, "
            "with or without one trailing newline; this file uses something "
            "else.) Pass --no-hooks to wrap without the hook."
        )
    if not isinstance(document, dict):
        raise InterposeError(f"{site.path} is not a JSON object — refusing to add a hook to it.")
    hooks = document.get(_HOOKS_KEY)
    if hooks is not None and not isinstance(hooks, dict):
        raise InterposeError(
            f"{site.path}: `{_HOOKS_KEY}` is not an object — refusing to add a hook "
            "to a settings file shaped in a way this adapter does not model."
        )
    if isinstance(hooks, dict):
        groups = hooks.get(site.event)
        if groups is not None and not isinstance(groups, list):
            raise InterposeError(
                f"{site.path}: `{_HOOKS_KEY}.{site.event}` is not a list — refusing "
                "to add a hook to a settings file shaped in a way this adapter does "
                "not model."
            )
    return _Loaded(document, newline=raw.endswith("\n"))


def _write(site: HookSite, loaded: _Loaded) -> None:
    assert loaded.document is not None
    site.path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(site.path, _render(loaded.document) + ("\n" if loaded.newline else ""))


def _groups(document: Optional[Mapping[str, Any]], event: str) -> list[Any]:
    if document is None:
        return []
    hooks = document.get(_HOOKS_KEY)
    if not isinstance(hooks, Mapping):
        return []
    groups = hooks.get(event)
    return list(groups) if isinstance(groups, list) else []


def _runs(group: Any, command: str) -> bool:
    """Whether a matcher group holds a hook that runs exactly `command`."""
    if not isinstance(group, Mapping) or not isinstance(group.get("hooks"), list):
        return False
    return any(
        isinstance(hook, Mapping) and hook.get("command") == command for hook in group["hooks"]
    )


def holds_command(site: HookSite, command: str) -> bool:
    """Whether the site's file holds a hook for this event that runs `command`."""
    return any(_runs(group, command) for group in _groups(_load(site).document, site.event))


def commands_at(site: HookSite) -> list[str]:
    """Every command hook string for the site's event, for a reader that matches them.

    Read-only, so read leniently: the round-trip check guards a WRITE, and a
    report of what the file holds needs only that it parses. An absent file
    holds none. Raises `InterposeError` for a file that does not parse, and
    OSError for one that cannot be read, so a caller can say "unknown".
    """
    if not site.path.exists():
        return []
    try:
        document = json.loads(site.path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InterposeError(f"{site.path} does not parse as JSON") from exc
    if not isinstance(document, dict):
        return []
    found: list[str] = []
    for group in _groups(document, site.event):
        if isinstance(group, Mapping) and isinstance(group.get("hooks"), list):
            found.extend(
                hook["command"]
                for hook in group["hooks"]
                if isinstance(hook, Mapping) and isinstance(hook.get("command"), str)
            )
    return found


def check_site(site: HookSite) -> None:
    """Read the site, so a format refusal comes before a wrap writes anything."""
    _load(site)


# ---------------------------------------------------------------------------
# Wrap: plan, backup, write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookInstall:
    """The hook a wrap is about to add, computed before anything is written."""

    site: HookSite
    command: str
    entry: dict[str, Any] = field(default_factory=dict)
    #: The file already holds a hook running this exact command, so the wrap
    #: adds none and its unwrap takes none out.
    present: bool = False


def plan_hook(site: HookSite, command: str) -> HookInstall:
    """Read the site and say what a wrap would add there. Writes nothing."""
    return HookInstall(
        site=site,
        command=command,
        entry=hook_entry(command),
        present=holds_command(site, command),
    )


@dataclass(frozen=True)
class HookBackup:
    """What the settings file looked like where the wrap touched it.

    Existence flags and the command, never the file: the file is live state
    the harness and the person keep editing, and an unwrap that put back a
    whole-file snapshot would roll their edits back with the wrap's.
    """

    path: str
    event: str
    command: str
    #: False when an identical hook was already there and the wrap added none.
    added: bool
    file_existed: bool
    hooks_existed: bool
    event_existed: bool
    #: Whether the file's directory was there. A wrap that had to make it for
    #: the file removes it again on unwrap, once it is empty.
    dir_existed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "event": self.event,
            "command": self.command,
            "added": self.added,
            "file_existed": self.file_existed,
            "hooks_existed": self.hooks_existed,
            "event_existed": self.event_existed,
            "dir_existed": self.dir_existed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HookBackup":
        """Parse the backup's `hook` object. Raises KeyError/TypeError on a bad shape."""
        if not isinstance(data, Mapping):
            raise TypeError("the hook record is not an object")
        values = {
            "path": data["path"],
            "event": data["event"],
            "command": data["command"],
        }
        flags = {
            name: data[name]
            for name in (
                "added", "file_existed", "hooks_existed", "event_existed", "dir_existed"
            )
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise TypeError("a hook record field is not a string")
        if not all(isinstance(value, bool) for value in flags.values()):
            raise TypeError("a hook record flag is not a boolean")
        return cls(**values, **flags)

    @property
    def site(self) -> HookSite:
        return HookSite(path=Path(self.path), event=self.event)


def backup_of_hook(install: HookInstall) -> HookBackup:
    """Record the site as it is now, before the wrap's write."""
    document = _load(install.site).document
    hooks = document.get(_HOOKS_KEY) if document is not None else None
    return HookBackup(
        path=str(install.site.path),
        event=install.site.event,
        command=install.command,
        added=not install.present,
        file_existed=document is not None,
        hooks_existed=isinstance(hooks, dict),
        event_existed=isinstance(hooks, dict) and install.site.event in hooks,
        dir_existed=install.site.path.parent.is_dir(),
    )


def write_hook(install: HookInstall) -> None:
    """Append the wrap's one entry, unless a hook running the command is there.

    Re-read here rather than trusted from the plan: the harness can write its
    settings while a wrap waits at a question, and what is appended to is
    what is there now.
    """
    if install.present:
        return
    loaded = _load(install.site)
    if any(_runs(group, install.command) for group in _groups(loaded.document, install.site.event)):
        return
    document = dict(loaded.document) if loaded.document is not None else {}
    hooks = dict(document.get(_HOOKS_KEY) or {})
    hooks[install.site.event] = [*_groups(document, install.site.event), dict(install.entry)]
    document[_HOOKS_KEY] = hooks
    _write(install.site, replace(loaded, document=document))


# ---------------------------------------------------------------------------
# Unwrap: plan, apply, verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRestore:
    """What the unwrap does to the settings file, read off the disk. Writes nothing."""

    backup: HookBackup
    #: tegh's hook is in the file now, so the unwrap takes it out.
    found: bool
    #: The file would hold nothing but what the wrap made, so it goes.
    removes_file: bool
    #: The directory the wrap made for the file goes with it, once empty.
    removes_dir: bool
    #: Groups the wrap's entry was found inside beside other hooks, which stay.
    shared: bool = False

    @property
    def site(self) -> HookSite:
        return self.backup.site

    @property
    def changes(self) -> bool:
        return self.found or self.removes_file


def _without_command(document: dict[str, Any], backup: HookBackup) -> tuple[dict, bool, bool]:
    """The document with every hook running the wrap's command taken out.

    Returns `(document, found, shared)`. A group that held only tegh's hook
    goes whole; one that holds other hooks beside it keeps them. What the
    backup says did not exist is then removed again if it is empty: the list,
    and the `hooks` object.
    """
    out = dict(document)
    hooks = out.get(_HOOKS_KEY)
    if not isinstance(hooks, dict) or not backup.added:
        return out, False, False
    kept: list[Any] = []
    found = shared = False
    for group in _groups(out, backup.event):
        if not _runs(group, backup.command):
            kept.append(group)
            continue
        found = True
        others = [
            hook
            for hook in group["hooks"]
            if not (isinstance(hook, Mapping) and hook.get("command") == backup.command)
        ]
        if others:
            shared = True
            kept.append({**group, "hooks": others})
    hooks = dict(hooks)
    if backup.event in hooks:
        hooks[backup.event] = kept
        if not kept and not backup.event_existed:
            del hooks[backup.event]
    out[_HOOKS_KEY] = hooks
    if not hooks and not backup.hooks_existed:
        del out[_HOOKS_KEY]
    return out, found, shared


def _lenient_holds(site: HookSite, command: str) -> bool:
    """Whether a file tegh cannot reproduce still holds the command, read leniently.

    Read only to decide whether there is anything to take out. A file that does
    not parse at all is treated as holding it, which sends the person to look.
    """
    try:
        document = json.loads(site.path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(document, dict):
        return True
    return any(_runs(group, command) for group in _groups(document, site.event))


def plan_hook_restore(backup: HookBackup) -> HookRestore:
    """Compare the file with what the unwrap would leave. Reads, never writes.

    A file that no longer round-trips (reformatted since the wrap, by hand or by
    the harness) is not rewritten. If tegh's hook is still in it, the unwrap
    refuses and says to take that one entry out by hand; once it is gone there
    is nothing left for tegh to do in the file, and the next unwrap goes on.
    """
    try:
        loaded = _load(backup.site)
    except InterposeError:
        if backup.added and _lenient_holds(backup.site, backup.command):
            raise InterposeError(
                f"{backup.site.path} holds tegh's {backup.event} hook, and tegh cannot "
                "rewrite that file without reformatting it (it no longer round-trips "
                "through tegh's serializer, or does not parse). Nothing was changed. "
                f"Remove the {backup.event} entry whose command is `{backup.command}` "
                "by hand, then run `tegh unwrap` again."
            ) from None
        return HookRestore(backup, found=False, removes_file=False, removes_dir=False)
    if loaded.document is None:
        return HookRestore(backup, found=False, removes_file=False, removes_dir=False)
    target, found, shared = _without_command(loaded.document, backup)
    removes_file = not backup.file_existed and target == {}
    return HookRestore(
        backup,
        found=found,
        removes_file=removes_file,
        removes_dir=removes_file and not backup.dir_existed,
        shared=shared,
    )


def hook_restored(step: HookRestore) -> bool:
    """Whether the file holds no hook running the wrap's command, read off the disk."""
    if step.removes_file and step.site.path.exists():
        return False
    return not step.backup.added or not holds_command(step.site, step.backup.command)


def apply_hook_restore(step: HookRestore) -> None:
    """Take the wrap's hook out, then read the file back and check it is gone.

    Planned again against the file as it is now, because the harness can write
    its settings while the person reads the plan. If what would happen to the
    FILE is no longer what the plan said, nothing is written.
    """
    fresh = plan_hook_restore(step.backup)
    if fresh.removes_file != step.removes_file and step.site.path.exists():
        raise ConfigChangedSincePlan(
            f"{step.site.path} changed while tegh unwrap was waiting: the plan "
            f"showed tegh {'removing' if step.removes_file else 'keeping'} this "
            "file, and that is no longer what would happen. Nothing was changed. "
            "Run `tegh unwrap` again for a plan of what is there now."
        )
    if fresh.removes_file:
        step.site.path.unlink()
        if fresh.removes_dir:
            try:
                step.site.path.parent.rmdir()
            except OSError:
                pass  # not empty: it holds something the wrap did not write
    elif fresh.found:
        loaded = _load(step.site)
        assert loaded.document is not None
        target, _, _ = _without_command(loaded.document, step.backup)
        _write(step.site, replace(loaded, document=target))
    if not hook_restored(fresh):
        raise RestoreUnverified(
            f"{step.site.path} still holds tegh's {step.backup.event} hook after "
            "the unwrap wrote it (read back and compared). Something else is "
            "writing this file, or the write did not land."
        )
