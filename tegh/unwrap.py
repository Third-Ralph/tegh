"""unwrap.py — what `tegh unwrap` will change, said before it changes it.

`interpose.py` owns how a config site is read, written and compared, and
`store.py` owns the credential map. This module is the order between them, and
the two things a person reads: the plan before the prompt and the report after.

## One list, read twice

The plan and the report are rendered from the SAME list of actions, in two
tenses. A change is on the list only when the disk differs from what the
unwrap would leave, so neither can name a file the unwrap does not touch: a
wrap records every site a harness has, and most of them never held a block.

## Only what the wrap put there comes out

An unwrap removes the gateway entry and puts the pre-wrap servers back. The
gateway entry is whichever entry runs this project's gateway, under any name
and at any scope: one renamed or moved since the wrap is removed and the list
says so, and a server that is only called `tegh` is not it. A
server added to a wrapped scope SINCE the wrap is kept, and the list names it
(`keep`), because it is the one line that says "this is not yours to lose"
[ruling: maintainer, 2026-10-04]. A kept entry under the name of a server being
restored is refused before anything changes: `interpose.RestoreCollision`.

A config FILE the wrap created goes the same way. When the harness had no file
for the gateway entry, the wrap made one, and the backup records that. The
unwrap removes that file if, with the wrap's entries out, nothing is left in
it, and the list says so. Anything added to it since keeps the file, holding
just that: the empty entry the wrap made for the project goes, on a line of its
own. A file that was there before the wrap is never removed.

## The credential ends in exactly one place, and never in none

A relocated credential lives in tegh's store while the project is wrapped. An
unwrap puts it back into the harness config and removes it from the store
[ruling: maintainer, 2026-10-03]: leaving it would be the second plaintext copy
the wrap contract exists to refuse, kept by the tool that refused it.

The order fails toward keeping the credential. The harness config is written,
then READ BACK and compared, and only then is the store's copy removed. A
restore that fails or does not verify leaves the store and the backup as they
were. A removal that fails after a good restore leaves the backup too, so
running `tegh unwrap` again retries the removal and nothing else. An unwrap
that died after the removal and before the backup went finds the credential
already in the config, and finishes without asking for it again.

No credential value is printed, logged or put in an error here. Every message
names a server, a field and a file.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Callable, Optional

from tegh import interpose
from tegh.interpose import RelocatedReference, SiteRestore, WrapBackup
from tegh.store import TeghStore, TeghStoreError

EXIT_OK = 0
EXIT_NOT_UNWRAPPED = 1  # the developer declined, or a step failed part-way
EXIT_REFUSED = 2  # nothing to unwrap, or it cannot be planned
EXIT_INTERRUPTED = 130  # Ctrl-C while the plan was being carried out

Prompt = Callable[[str], str]

QUESTION = "Proceed? [Y/n] "
_YES = ("", "y", "yes")


@dataclass(frozen=True)
class Action:
    """One change, in the tense the plan uses and the tense the report uses."""

    planned: str
    done: str
    what: str


@dataclass(frozen=True)
class UnwrapPlan:
    project: Path
    backup: WrapBackup
    backup_path: Path
    secrets_path: Path
    audit_path: Path
    sites: list[SiteRestore]
    credentials: list[RelocatedReference]

    @property
    def already_back(self) -> list[RelocatedReference]:
        """Credentials the config already holds and the store no longer does."""
        in_place = {
            (step.site.label, *coordinate)
            for step in self.sites
            for coordinate in step.in_place
        }
        return [
            credential
            for credential in self.credentials
            if (
                credential.site_label,
                credential.server_id,
                credential.block,
                credential.field_name,
            )
            in in_place
        ]

    @property
    def actions(self) -> list[Action]:
        actions: list[Action] = []
        for step in self.sites:
            label = step.site.label
            # The gateway entry where the wrap put it and under the name the
            # wrap gave it is covered by the lines below. One found anywhere
            # else, or under another name, gets a line of its own.
            as_written = step.gateway_name in step.gateway_found
            elsewhere = sorted(
                name for name in step.gateway_found if name != step.gateway_name
            )
            if step.changes and step.block_existed:
                names = ", ".join(step.servers) or "an empty mcpServers block"
                actions.append(Action("restore", "restored", f"{label}  ({names})"))
            elif step.changes and (as_written or not elsewhere):
                # The scope had no block before the wrap, so there is nothing
                # to restore there. What the unwrap does is take out what the
                # wrap put in, and it says that: the whole block, or only the
                # gateway entry when something else has been added beside it.
                added = (
                    f"the {step.gateway_name} entry the wrap added"
                    if step.kept
                    else "the mcpServers block the wrap added"
                )
                actions.append(Action("remove", "removed", f"{label}  ({added})"))
            # Recognised by the command it runs and not by its name, so a
            # gateway entry renamed or moved since the wrap goes with the wrap.
            # The line says why an entry the reader may not know as tegh's is
            # being taken out.
            actions.extend(
                Action(
                    "remove",
                    "removed",
                    f"{label}  ({name}), which runs tegh's gateway for this project",
                )
                for name in elsewhere
            )
            if step.prunes_parents:
                # Its own line, and not folded into the one above: it is the
                # only line this file gets when the block is already out.
                actions.append(
                    Action(
                        "remove",
                        "removed",
                        f"{label}  (the empty {step.site.pointer[0]} entry the "
                        "wrap created for this project)",
                    )
                )
            # Listed whether or not the site is written: a kept entry is a
            # thing the reader might have expected to lose.
            actions.extend(
                Action("keep", "kept", f"{label}  ({name}), added since the wrap")
                for name in sorted(step.kept)
            )
        # After the sites, since it is what is left of them: a file the wrap
        # had to create, with nothing in it once the lines above are done.
        actions.extend(
            Action(
                "remove",
                "removed",
                f"{path}  (the file itself, which the wrap created and nothing "
                "else has been added to)",
            )
            for path in dict.fromkeys(
                step.site.path for step in self.sites if step.removes_file
            )
        )
        written = {step.site.label for step in self.sites if step.changes}
        already_back = self.already_back
        for credential in self.credentials:
            if credential in already_back:
                continue
            name = _credential_name(credential)
            # Not listed when the site already holds it, which is the state a
            # re-run finds after a removal that failed: the put-back happened
            # then, and only the removal is left to do.
            if credential.site_label in written:
                actions.append(
                    Action(
                        "put back", "put back", f"{name}, into {credential.site_label}"
                    )
                )
            actions.append(
                Action(
                    "remove",
                    "removed",
                    f"{name}, from tegh's store at {self.secrets_path}",
                )
            )
        actions.append(
            Action("remove", "removed", f"tegh's wrap backup at {self.backup_path}")
        )
        return actions


def _credential_name(credential: RelocatedReference) -> str:
    return f"credential {credential.server_id} {credential.block}.{credential.field_name}"


def build_plan(store: TeghStore, project: Path) -> UnwrapPlan:
    """Read the backup, the store and every site. Writes nothing.

    Raises `InterposeError` when the unwrap cannot be carried out as planned: no
    backup, a backup or a config tegh cannot read back, a server added since
    the wrap under a restored server's name, or a credential that is in neither
    the store nor the config. All of those are found here, before the developer
    is asked anything. `TeghStoreError` and `OSError` from a file that cannot be
    read pass through; `run` turns each into a refusal.
    """
    backup_path = store.backup_path(project)
    if not backup_path.exists():
        raise interpose.BackupMissing(
            f"no wrap backup for {project} at {backup_path} — nothing to "
            "restore. (A wrap run with --no-rewrite never touched the config.)"
        )
    try:
        backup = WrapBackup.from_json(backup_path.read_text(encoding="utf-8"))
    except interpose.BackupUnreadable as exc:
        raise interpose.BackupUnreadable(
            f"tegh cannot restore from the wrap backup at {backup_path}: {exc}. "
            "Nothing was changed."
        ) from exc
    secrets_path = store.secrets_path(project)

    # Read on the first reference and once only, so a backup with no credential
    # never opens the credential map.
    stored_secrets = cache(lambda: store.read_secrets(project))

    def resolve_secret(leaf: str, field_name: str) -> str:
        """Put a relocated credential back where the wrap took it from.

        Raises rather than substituting anything on a miss: every site is
        resolved before any is written, so a credential missing from the store
        aborts the unwrap with the harness untouched — far better than writing
        back a config whose `env` holds a marker object instead of a value.
        `CredentialNotStored` is the miss `interpose.plan_restore` answers by
        looking in the config itself.
        """
        try:
            fields = _json_object(stored_secrets()[leaf])
            return str(fields[field_name])
        except KeyError:
            raise interpose.CredentialNotStored(f"{leaf}.{field_name}") from None
        except ValueError as exc:
            raise interpose.InterposeError(
                f"the entry stored under {leaf!r} in {secrets_path} is not a JSON "
                "object of fields, so tegh cannot tell whether it still holds "
                f"the credential {leaf}.{field_name}. Nothing was changed."
            ) from exc

    try:
        sites = interpose.plan_restore(backup, resolve=resolve_secret)
    except interpose.CredentialUnavailable as exc:
        # The value is gone and tegh never had a second copy to offer. The one
        # place this may send a credential is the harness config, where it
        # lived before the wrap: never the store, and never the backup, which
        # would each be a new plaintext copy.
        name = f"{exc.server_id} {exc.block}.{exc.field_name}"
        raise interpose.InterposeError(
            f"the credential this wrap moved out of your config ({name}) is no "
            f"longer in tegh's store at {secrets_path}, and {exc.site_label} "
            "does not hold it either. Nothing was changed, and tegh has no "
            f"copy of the value to put back. Add server {exc.server_id} back to "
            "that harness config by hand with the field set (the rest of its "
            f"definition is in {backup_path}, which never held the value), "
            "then run `tegh unwrap` again to finish."
        ) from exc
    except interpose.RestoreCollision as exc:
        raise interpose.RestoreCollision(
            f"{exc} The wrap backup is at {backup_path}."
        ) from exc

    return UnwrapPlan(
        project=project,
        backup=backup,
        backup_path=backup_path,
        secrets_path=secrets_path,
        audit_path=store.audit_path(project),
        sites=sites,
        credentials=interpose.relocated_references(backup),
    )


def _json_object(raw: str) -> dict:
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("the stored leaf is not a JSON object of fields")
    return loaded


def _lines(actions: list[Action], *, done: bool) -> list[str]:
    verbs = [action.done if done else action.planned for action in actions]
    width = max(len(verb) for verb in verbs)
    return [
        f"  {verb.ljust(width)}  {action.what}" for verb, action in zip(verbs, actions)
    ]


#: Asked, never checked: tegh does not look for a running session and cannot
#: tell whether one exists. `docs/references/harnesses/claude-code.md` §7
#: ("Corrected by observation") records that a running session rewrites
#: `~/.claude.json`, the file two of the three scopes live in. Whether such a
#: rewrite carries the session's own idea of `mcpServers` over a restored block
#: is not established there, so the reader is told how to rule it out.
QUIT_THE_AGENT = (
    "Quit the coding agent in this project before you proceed: a session that "
    "is still running can rewrite its config from memory after the restore."
)


def render_plan(plan: UnwrapPlan) -> str:
    already_back = [
        f"Already restored: {credential.site_label} holds a value for "
        f"{_credential_name(credential)} and tegh's store no longer does, so it "
        "is not moved again."
        for credential in plan.already_back
    ]
    return "\n".join(
        [
            f"tegh unwrap will change, for {plan.project}:",
            "",
            *_lines(plan.actions, done=False),
            "",
            *(line for note in already_back for line in (note, "")),
            "It leaves untouched:",
            "",
            "  tegh.lock and the admitted rows, so re-wrapping does not re-run "
            "the ceremony",
            f"  the audit tape at {plan.audit_path}",
            "",
            QUIT_THE_AGENT,
            "",
        ]
    )


def render_report(plan: UnwrapPlan) -> str:
    count = len(plan.credentials)
    moved = (
        f" {count} relocated credential value(s) are back in the harness config "
        "and no longer in tegh's store."
        if count
        else ""
    )
    return "\n".join(
        [
            *_lines(plan.actions, done=True),
            "",
            f"unwrapped {plan.project} (wrapped {plan.backup.wrapped_at}). The "
            f"harness reaches its original servers directly again.{moved} "
            "tegh.lock, the admitted rows and the audit tape are untouched, so "
            "re-wrapping does not re-run the ceremony.",
        ]
    )


def _is_terminal(stream) -> bool:
    """False for a closed descriptor too: Python leaves `sys.stdin` as None."""
    try:
        return stream is not None and stream.isatty()
    except ValueError:  # a stream object that has been closed
        return False


def _ask_on_the_terminal(question: str) -> str:
    # Written to stdout by hand, and not passed to `input`: with a terminal on
    # both ends the interpreter may print an `input` prompt on stderr, and
    # stdout is the stream `confirm` checked a person can see.
    sys.stdout.write(question)
    sys.stdout.flush()
    return input()


def confirm(question: str, *, prompt: Optional[Prompt] = None) -> bool:
    """Ask on the terminal. Anything short of a terminal is a NO, never a yes.

    The same contract as the prompt `tegh approve` reaches in the base
    (`safe_agents.broker.approval.release_cli`): where nobody can type, silence
    means no human agreed, and `--yes` is how one says so in advance. Every
    negative answer prints one line saying nothing was changed.

    BOTH ends have to be a terminal. With stdout sent to a file the plan and
    the question go into the file, the person sees nothing, and their Enter
    would be a yes to a plan they never read.

    `prompt` stands in for the terminal when a caller supplies one.
    """
    if sys.stdout is not None:
        sys.stdout.flush()
    if prompt is None:
        missing = [
            name
            for name, stream in (("stdin", sys.stdin), ("stdout", sys.stdout))
            if not _is_terminal(stream)
        ]
        if missing:
            print(
                f"REFUSED: {' and '.join(missing)} "
                f"{'is not a terminal' if len(missing) == 1 else 'are not terminals'}, so "
                "nobody can both read the plan and answer this prompt. Nothing "
                "was changed. Pass --yes to state the approval up front.",
                file=sys.stderr,
            )
            return False
        prompt = _ask_on_the_terminal
    try:
        answer = prompt(question)
    except (EOFError, KeyboardInterrupt):
        print(
            "\nNOT UNWRAPPED: no answer was given. Nothing was changed. Pass "
            "--yes to skip the prompt.",
            file=sys.stderr,
        )
        return False
    if answer.strip().lower() in _YES:
        return True
    print("NOT UNWRAPPED: nothing was changed.", file=sys.stderr)
    return False


def _site_progress(plan: UnwrapPlan) -> tuple[list[str], list[str]]:
    """The sites this unwrap had to write, split by what the DISK holds now.

    Read back, and not remembered: after a failure the question is where a
    credential is, and only the files answer that.
    """
    restored: list[str] = []
    not_restored: list[str] = []
    for step in plan.sites:
        if not (step.changes or step.removes_file):
            continue
        try:
            landed = interpose.site_restored(step)
        except (interpose.InterposeError, OSError):
            landed = False
        (restored if landed else not_restored).append(step.site.label)
    return restored, not_restored


def _unfinished_restore(plan: UnwrapPlan, before: str, after: str) -> str:
    """What to say when the restore stopped: per site, then where the credential is.

    The first line is `before`, then "not restored" or "only partly restored",
    then `after`.
    """
    restored, not_restored = _site_progress(plan)
    lines = [f"{before}{'only partly' if restored else 'not'} restored{after}"]
    if restored:
        lines += [f"  restored      {label}" for label in restored]
        lines += [f"  not restored  {label}" for label in not_restored]
    both = [
        f"{_credential_name(credential)}, in {credential.site_label}"
        for credential in plan.credentials
        if credential.site_label in restored
    ]
    if plan.credentials:
        held = (
            f"The store copy was kept: every credential of this wrap is still "
            f"in tegh's store at {plan.secrets_path}. "
        )
        if both:
            # The sentence the old message left out: a restored site means the
            # value is in two places until the unwrap is run to the end.
            lead = "This one is" if len(both) == 1 else "These are"
            held += f"{lead} now in the harness config as well: {'; '.join(both)}. "
    else:
        held = "Nothing was removed from tegh's store. "
    lines.append(
        f"{held}The wrap backup is still at {plan.backup_path}. Run `tegh "
        "unwrap` again once that is fixed; it finishes the job from what is on "
        "disk."
    )
    return "\n".join(lines)


def _restore_sites(plan: UnwrapPlan) -> Optional[str]:
    try:
        # Merges against the files as they are now, writes, then reads every
        # site back and compares. Returning is the verification; see
        # `interpose.apply_restore`.
        interpose.apply_restore(plan.sites)
    except (interpose.InterposeError, OSError) as exc:
        return _unfinished_restore(
            plan, "FAILED: the harness config was ", f": {exc}"
        )
    return None


def _remove_from_store(store: TeghStore, plan: UnwrapPlan) -> Optional[str]:
    fields_by_leaf: dict[str, list[str]] = {}
    for credential in plan.credentials:
        fields_by_leaf.setdefault(credential.leaf, []).append(credential.field_name)
    try:
        store.remove_secret_fields(plan.project, fields_by_leaf)
        # Read back, for the reason the restore is: the claim the report makes
        # is about the file, and a call that returned is not the file.
        remaining = store.read_secrets(plan.project)
        left = [
            _credential_name(credential)
            for credential in plan.credentials
            if credential.field_name in _json_object(remaining.get(credential.leaf, "{}"))
        ]
        if left:
            raise TeghStoreError(f"still present after the write: {', '.join(left)}")
    except (TeghStoreError, ValueError, OSError) as exc:
        names = ", ".join(_credential_name(c) for c in plan.credentials)
        return (
            "FAILED: the harness config is restored, but a credential could "
            f"not be removed from tegh's store: {exc}\n"
            f"{plan.secrets_path} still holds a copy of {names}. Run `tegh "
            "unwrap` again to retry the removal (the wrap backup at "
            f"{plan.backup_path} is kept for that), or delete those entries "
            "from that file by hand."
        )
    return None


def _remove_backup(plan: UnwrapPlan) -> Optional[str]:
    try:
        plan.backup_path.unlink()
    except OSError as exc:
        return (
            "FAILED: the harness config is restored, but the wrap backup at "
            f"{plan.backup_path} could not be removed: {exc}\nDelete that file "
            "by hand. A second `tegh unwrap` would try to restore from it."
        )
    return None


_RESTORING, _REMOVING, _FINISHING = range(3)


def _interrupted(plan: UnwrapPlan, stage: int) -> str:
    """What was and was not done when Ctrl-C arrived, by the stage it reached."""
    stopped = "INTERRUPTED: tegh unwrap was stopped before it finished."
    if stage == _RESTORING:
        return _unfinished_restore(plan, f"{stopped} The harness config was ", ".")
    rerun = (
        f"The wrap backup is still at {plan.backup_path}. Run `tegh unwrap` "
        "again; it finishes the job from what is on disk."
    )
    if stage == _REMOVING:
        return (
            f"{stopped} The harness config is restored. The removal of this "
            f"wrap's credentials from tegh's store at {plan.secrets_path} had "
            f"started and may not have happened. {rerun}"
        )
    return (
        f"{stopped} The harness config is restored and tegh's store no longer "
        f"holds this wrap's credentials. The wrap backup at {plan.backup_path} "
        "may still be there; if it is, run `tegh unwrap` again to remove it."
    )


def apply_plan(store: TeghStore, plan: UnwrapPlan) -> int:
    """Carry the plan out in the order that cannot lose a credential.

    Each stage returns the message of its own failure, and the next stage does
    not start. Ctrl-C at any point reports what the disk holds and how far the
    order got, and exits 130.
    """
    stage = _RESTORING
    try:
        failure = _restore_sites(plan)
        if failure is None and plan.credentials:
            stage = _REMOVING
            failure = _remove_from_store(store, plan)
        if failure is None:
            stage = _FINISHING
            failure = _remove_backup(plan)
    except KeyboardInterrupt:
        print(_interrupted(plan, stage), file=sys.stderr)
        return EXIT_INTERRUPTED
    if failure is not None:
        print(failure, file=sys.stderr)
        return EXIT_NOT_UNWRAPPED
    print(render_report(plan))
    return EXIT_OK


def cannot_read(exc: OSError, *, needed_by: str) -> str:
    """What to say when a file could not be read before anything was written.

    The file's name and the system's reason. Never `str(exc)` of something that
    could carry contents. `tegh wrap` says the same sentence for the same
    failure, so it is here for both.
    """
    where = f" {exc.filename}" if exc.filename else f" a file this {needed_by} needs"
    return f"tegh cannot read{where} ({exc.strerror}). Nothing was changed."


def run(
    store: TeghStore, project: Path, *, yes: bool, prompt: Optional[Prompt] = None
) -> int:
    """`tegh unwrap`: plan, show, ask, carry out. Returns the exit status.

    Every way the plan can fail to be made is one line on stderr and exit 2,
    with nothing changed and no value in the message.
    """
    try:
        plan = build_plan(store, project)
    except (interpose.InterposeError, TeghStoreError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"REFUSED: {cannot_read(exc, needed_by='unwrap')}", file=sys.stderr)
        return EXIT_REFUSED
    print(render_plan(plan))
    if not yes and not confirm(QUESTION, prompt=prompt):
        return EXIT_NOT_UNWRAPPED
    return apply_plan(store, plan)
