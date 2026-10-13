"""hooksite.py: the hook entries a wrap adds to a harness's settings, and takes out again.

`interpose.py` rewrites the blocks a harness loads MCP servers from. This module
is its sibling for the second thing `tegh wrap claude` writes into a harness
config: one command, `tegh hook` (`hook.py`), registered under two events. Under
`PreToolUse` it asks the broker before each of the harness's built-in tool calls
and carries `"onFailure": "block"`, so a run of it that fails, times out or
never starts refuses the call instead of letting it through. Under `PostToolUse`
it reports the call after it ran, and its entry is as 0.3.0 wrote it. The
adapter supplies WHERE (:class:`HookSite`); the entries, and how the settings
file is read, edited, backed up and restored, are here.

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

**Only tegh's entries are touched.** One matcher group is appended to
`hooks.PreToolUse` and one to `hooks.PostToolUse`, each only when no group
under that event already runs the command. No existing hook, matcher or key is
read into the change or written back differently. A file holding only the
`PostToolUse` group (a project wrapped by 0.3.0) gets the `PreToolUse` group
added and nothing else.

**Absent is put back as absent.** The backup records whether the file, the
`hooks` object, each event's list and the file's directory existed, and which
events the wrap added to, so an unwrap that takes tegh's entries out leaves no
`{}`, no empty list and no empty file or directory that the wrap made. With
nothing written in between, that is the file byte for byte as it was.

**Only what the wrap added comes out.** An entry is found by its command string,
which the backup records, under the events the backup says the wrap added to.
A hook the person or the harness added since the wrap is kept, beside it or in
the same group.

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

#: The event whose run asks the broker before the call (`hook.py`).
PRE_TOOL_USE = "PreToolUse"

#: The event whose run reports the call after it ran (`hook.py`).
POST_TOOL_USE = "PostToolUse"

#: The events a wrap registers tegh's command under, in the order written.
HOOK_EVENTS: tuple[str, ...] = (PRE_TOOL_USE, POST_TOOL_USE)

#: The observing event alone. Still named by the wrap's report.
HOOK_EVENT = POST_TOOL_USE

#: Claude Code's `onFailure` value that refuses the call when the hook fails,
#: times out or cannot start (Claude Code 2.1.295 and later; `hook.py`, "When no
#: decision can be had"). Set on the `PreToolUse` entry only: a `PostToolUse`
#: run that fails has nothing left to refuse.
ON_FAILURE_BLOCK = "block"

#: The key a settings file keeps its hooks under, by event.
_HOOKS_KEY = "hooks"

#: How long the harness may wait for one `tegh hook` run, in seconds. The hook's
#: own decision request gives up after five and its report after two, and a cold
#: interpreter start is the rest.
HOOK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class HookSite:
    """One settings file a harness reads hooks from."""

    path: Path
    events: tuple[str, ...] = HOOK_EVENTS

    @property
    def label(self) -> str:
        return f"hooks:{self.path}"


def hook_entry(command: str, event: str) -> dict[str, Any]:
    """The matcher group a wrap appends under `event`: every tool, one command hook.

    The `PreToolUse` handler carries `"onFailure": "block"`; the `PostToolUse`
    one does not, and is the entry 0.3.0 wrote.
    """
    handler: dict[str, Any] = {
        "type": "command",
        "command": command,
        "timeout": HOOK_TIMEOUT_SECONDS,
    }
    if event == PRE_TOOL_USE:
        handler["onFailure"] = ON_FAILURE_BLOCK
    return {"matcher": "", "hooks": [handler]}


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
        for event in site.events:
            groups = hooks.get(event)
            if groups is not None and not isinstance(groups, list):
                raise InterposeError(
                    f"{site.path}: `{_HOOKS_KEY}.{event}` is not a list — refusing "
                    "to add a hook to a settings file shaped in a way this adapter "
                    "does not model."
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


def _events_holding(document: Optional[Mapping[str, Any]], events, command: str) -> tuple:
    """The events, of `events`, under which a group runs `command`, in that order."""
    return tuple(
        event
        for event in events
        if any(_runs(group, command) for group in _groups(document, event))
    )


def events_holding(site: HookSite, command: str) -> tuple[str, ...]:
    """The site's events under which the file holds a hook that runs `command`."""
    return _events_holding(_load(site).document, site.events, command)


def holds_command(site: HookSite, command: str) -> bool:
    """Whether the site's file holds a hook running `command` under any of its events."""
    return bool(events_holding(site, command))


def commands_by_event(site: HookSite) -> dict[str, list[str]]:
    """Every command hook string under each of the site's events, for a reader.

    Read-only, so read leniently: the round-trip check guards a WRITE, and a
    report of what the file holds needs only that it parses. Every event in
    `site.events` is a key, holding `[]` when it has no command hook; an absent
    file is `{}`. Raises `InterposeError` for a file that does not parse, and
    OSError for one that cannot be read, so a caller can say "unknown".
    """
    if not site.path.exists():
        return {}
    try:
        document = json.loads(site.path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InterposeError(f"{site.path} does not parse as JSON") from exc
    if not isinstance(document, dict):
        document = None
    found: dict[str, list[str]] = {}
    for event in site.events:
        commands = found.setdefault(event, [])
        for group in _groups(document, event):
            if isinstance(group, Mapping) and isinstance(group.get("hooks"), list):
                commands.extend(
                    hook["command"]
                    for hook in group["hooks"]
                    if isinstance(hook, Mapping) and isinstance(hook.get("command"), str)
                )
    return found


def commands_at(site: HookSite) -> list[str]:
    """Every `PostToolUse` command hook string at the site (`commands_by_event`).

    Read and raised the same way.
    """
    return commands_by_event(site).get(POST_TOOL_USE, [])


def check_site(site: HookSite) -> None:
    """Read the site, so a format refusal comes before a wrap writes anything."""
    _load(site)


# ---------------------------------------------------------------------------
# Wrap: plan, backup, write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookInstall:
    """The hook entries a wrap is about to add, computed before anything is written."""

    site: HookSite
    command: str
    #: The matcher group for each of the site's events.
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The events under which the file already holds a hook running this exact
    #: command. The wrap adds none under those, and its unwrap takes none out.
    present_events: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        """Every event already holds the command, so the wrap adds nothing."""
        return set(self.site.events) <= set(self.present_events)

    @property
    def adds(self) -> tuple[str, ...]:
        """The events the wrap adds an entry under, in the site's order."""
        return tuple(event for event in self.site.events if event not in self.present_events)


def plan_hook(site: HookSite, command: str) -> HookInstall:
    """Read the site and say what a wrap would add there. Writes nothing."""
    return HookInstall(
        site=site,
        command=command,
        entries={event: hook_entry(command, event) for event in site.events},
        present_events=events_holding(site, command),
    )


@dataclass(frozen=True)
class EventBackup:
    """What one event's list looked like before the wrap, and whether it added to it."""

    #: Whether `hooks.<event>` was in the file.
    existed: bool
    #: False when an identical hook was already there and the wrap added none.
    added: bool

    def to_dict(self) -> dict[str, bool]:
        return {"existed": self.existed, "added": self.added}


@dataclass(frozen=True)
class HookBackup:
    """What the settings file looked like where the wrap touched it.

    Existence flags and the command, never the file: the file is live state
    the harness and the person keep editing, and an unwrap that put back a
    whole-file snapshot would roll their edits back with the wrap's.
    """

    path: str
    command: str
    file_existed: bool
    hooks_existed: bool
    #: Whether the file's directory was there. A wrap that had to make it for
    #: the file removes it again on unwrap, once it is empty.
    dir_existed: bool
    #: Per event the wrap registered under, in the order written.
    events: dict[str, EventBackup] = field(default_factory=dict)

    @property
    def added(self) -> bool:
        """The wrap added an entry under at least one event."""
        return any(event.added for event in self.events.values())

    @property
    def added_events(self) -> tuple[str, ...]:
        return tuple(name for name, event in self.events.items() if event.added)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "command": self.command,
            "file_existed": self.file_existed,
            "hooks_existed": self.hooks_existed,
            "dir_existed": self.dir_existed,
            "events": {name: event.to_dict() for name, event in self.events.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HookBackup":
        """Parse the backup's `hook` object. Raises KeyError/TypeError on a bad shape.

        Reads the shape 0.3.0 wrote too (one `event`, with `event_existed` and
        `added` beside it) as a backup of that one event, so a project wrapped
        by 0.3.0 unwraps cleanly on this version.
        """
        if not isinstance(data, Mapping):
            raise TypeError("the hook record is not an object")
        values = {"path": data["path"], "command": data["command"]}
        flags = {name: data[name] for name in ("file_existed", "hooks_existed", "dir_existed")}
        if "events" in data:
            raw_events = data["events"]
            if not isinstance(raw_events, Mapping) or not raw_events:
                raise TypeError("the hook record's events are not a non-empty object")
            pairs = [
                (name, event["existed"], event["added"]) if isinstance(event, Mapping)
                else (name, None, None)
                for name, event in raw_events.items()
            ]
        else:
            pairs = [(data["event"], data["event_existed"], data["added"])]
        if not all(isinstance(value, str) for value in values.values()) or not all(
            isinstance(name, str) for name, _, _ in pairs
        ):
            raise TypeError("a hook record field is not a string")
        if not all(isinstance(value, bool) for value in flags.values()) or not all(
            isinstance(existed, bool) and isinstance(added, bool) for _, existed, added in pairs
        ):
            raise TypeError("a hook record flag is not a boolean")
        events = {name: EventBackup(existed=existed, added=added) for name, existed, added in pairs}
        return cls(**values, **flags, events=events)

    @property
    def site(self) -> HookSite:
        return HookSite(path=Path(self.path), events=tuple(self.events))


def backup_of_hook(install: HookInstall) -> HookBackup:
    """Record the site as it is now, before the wrap's write."""
    document = _load(install.site).document
    hooks = document.get(_HOOKS_KEY) if document is not None else None
    return HookBackup(
        path=str(install.site.path),
        command=install.command,
        file_existed=document is not None,
        hooks_existed=isinstance(hooks, dict),
        dir_existed=install.site.path.parent.is_dir(),
        events={
            event: EventBackup(
                existed=isinstance(hooks, dict) and event in hooks,
                added=event in install.adds,
            )
            for event in install.site.events
        },
    )


def write_hook(install: HookInstall) -> None:
    """Append the wrap's entry under each event the plan adds to, unless one is there.

    Only the events the plan said it adds to, so the backup's `added` flags stay
    true of what the wrap wrote. Re-read here rather than trusted from the plan:
    the harness can write its settings while a wrap waits at a question, and
    what is appended to is what is there now.
    """
    if not install.adds:
        return
    loaded = _load(install.site)
    missing = [
        event
        for event in install.adds
        if not any(_runs(group, install.command) for group in _groups(loaded.document, event))
    ]
    if not missing:
        return
    document = dict(loaded.document) if loaded.document is not None else {}
    hooks = dict(document.get(_HOOKS_KEY) or {})
    for event in missing:
        hooks[event] = [*_groups(document, event), dict(install.entries[event])]
    document[_HOOKS_KEY] = hooks
    _write(install.site, replace(loaded, document=document))


# ---------------------------------------------------------------------------
# Unwrap: plan, apply, verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRestore:
    """What the unwrap does to the settings file, read off the disk. Writes nothing."""

    backup: HookBackup
    #: The events under which tegh's hook is in the file now, and which the
    #: wrap added to, so the unwrap takes it out from under them.
    events_found: tuple[str, ...]
    #: The file would hold nothing but what the wrap made, so it goes.
    removes_file: bool
    #: The directory the wrap made for the file goes with it, once empty.
    removes_dir: bool
    #: Groups the wrap's entry was found inside beside other hooks, which stay.
    shared: bool = False

    @property
    def found(self) -> bool:
        """tegh's hook is in the file under at least one event the wrap added to."""
        return bool(self.events_found)

    @property
    def site(self) -> HookSite:
        return self.backup.site

    @property
    def changes(self) -> bool:
        return self.found or self.removes_file


def _without_command(
    document: dict[str, Any], backup: HookBackup
) -> tuple[dict, tuple[str, ...], bool]:
    """The document with every hook running the wrap's command taken out.

    Under each event the backup says the wrap added to, and no other. Returns
    `(document, events_found, shared)`. A group that held only tegh's hook goes
    whole; one that holds other hooks beside it keeps them. What the backup
    says did not exist is then removed again if it is empty: each event's list,
    and the `hooks` object.
    """
    out = dict(document)
    hooks = out.get(_HOOKS_KEY)
    if not isinstance(hooks, dict) or not backup.added:
        return out, (), False
    hooks = dict(hooks)
    found: list[str] = []
    shared = False
    for event in backup.added_events:
        kept: list[Any] = []
        for group in _groups(out, event):
            if not _runs(group, backup.command):
                kept.append(group)
                continue
            if event not in found:
                found.append(event)
            others = [
                hook
                for hook in group["hooks"]
                if not (isinstance(hook, Mapping) and hook.get("command") == backup.command)
            ]
            if others:
                shared = True
                kept.append({**group, "hooks": others})
        if event in hooks:
            hooks[event] = kept
            if not kept and not backup.events[event].existed:
                del hooks[event]
    out[_HOOKS_KEY] = hooks
    if not hooks and not backup.hooks_existed:
        del out[_HOOKS_KEY]
    return out, tuple(found), shared


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
    return bool(_events_holding(document, site.events, command))


def plan_hook_restore(backup: HookBackup) -> HookRestore:
    """Compare the file with what the unwrap would leave. Reads, never writes.

    A file that no longer round-trips (reformatted since the wrap, by hand or by
    the harness) is not rewritten. If tegh's hook is still in it, the unwrap
    refuses and says to take that one entry out by hand; once it is gone there
    is nothing left for tegh to do in the file, and the next unwrap goes on.
    """
    added = HookSite(path=backup.site.path, events=backup.added_events)
    try:
        loaded = _load(backup.site)
    except InterposeError:
        if backup.added and _lenient_holds(added, backup.command):
            events = " and ".join(backup.added_events)
            raise InterposeError(
                f"{backup.site.path} holds tegh's {events} hook, and tegh cannot "
                "rewrite that file without reformatting it (it no longer round-trips "
                "through tegh's serializer, or does not parse). Nothing was changed. "
                f"Remove the {events} entries whose command is `{backup.command}` "
                "by hand, then run `tegh unwrap` again."
            ) from None
        return HookRestore(backup, events_found=(), removes_file=False, removes_dir=False)
    if loaded.document is None:
        return HookRestore(backup, events_found=(), removes_file=False, removes_dir=False)
    target, found, shared = _without_command(loaded.document, backup)
    removes_file = not backup.file_existed and target == {}
    return HookRestore(
        backup,
        events_found=found,
        removes_file=removes_file,
        removes_dir=removes_file and not backup.dir_existed,
        shared=shared,
    )


def hook_restored(step: HookRestore) -> bool:
    """Whether the file holds no hook running the wrap's command under any event
    the wrap added to, read off the disk."""
    if step.removes_file and step.site.path.exists():
        return False
    if not step.backup.added:
        return True
    added = HookSite(path=step.site.path, events=step.backup.added_events)
    return not holds_command(added, step.backup.command)


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
        events = " and ".join(step.backup.added_events)
        raise RestoreUnverified(
            f"{step.site.path} still holds tegh's {events} hook after "
            "the unwrap wrote it (read back and compared). Something else is "
            "writing this file, or the write did not land."
        )
