"""unwrap.py — what `tegh unwrap` will change, said before it changes it.

`interpose.py` owns how a config site is read, written and compared, and
`store.py` owns the credential map. This module is the order between them, and
the two things a person reads: the plan before the prompt and the report after.

## One list, read twice

The plan and the report are rendered from the SAME list of actions, in two
tenses. An action is on the list only when the disk differs from what the
unwrap would leave, so neither can name a file the unwrap does not touch: a
wrap records every site a harness has, and most of them never held a block.

## The credential ends in exactly one place, and never in none

A relocated credential lives in tegh's store while the project is wrapped. An
unwrap puts it back into the harness config and removes it from the store
[ruling: maintainer, 2026-10-03]: leaving it would be the second plaintext copy
the wrap contract exists to refuse, kept by the tool that refused it.

The order fails toward keeping the credential. The harness config is written,
then READ BACK and compared, and only then is the store's copy removed. A
restore that fails or does not verify leaves the store and the backup as they
were. A removal that fails after a good restore leaves the backup too, so
running `tegh unwrap` again retries the removal and nothing else.

No credential value is printed, logged or put in an error here. Every message
names a server, a field and a file.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tegh import interpose
from tegh.interpose import RelocatedReference, SiteRestore, WrapBackup
from tegh.store import TeghStore, TeghStoreError

EXIT_OK = 0
EXIT_NOT_UNWRAPPED = 1  # the developer declined, or a step failed part-way
EXIT_REFUSED = 2  # nothing to unwrap, or it cannot be planned

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
    def actions(self) -> list[Action]:
        actions: list[Action] = []
        for step in self.sites:
            if not step.changes:
                continue
            if step.block_existed:
                names = ", ".join(step.servers) or "an empty mcpServers block"
                actions.append(
                    Action("restore", "restored", f"{step.site.label}  ({names})")
                )
            else:
                # The scope had no block before the wrap, so there is nothing
                # to restore there. What the unwrap does is take out the block
                # the wrap put in, and it says that.
                actions.append(
                    Action(
                        "remove",
                        "removed",
                        f"{step.site.label}  (the mcpServers block the wrap added)",
                    )
                )
        written = {step.site.label for step in self.sites if step.changes}
        for credential in self.credentials:
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
    backup, a config tegh cannot reproduce, or a credential that is no longer in
    the store. All of those are found here, before the developer is asked
    anything.
    """
    backup_path = store.backup_path(project)
    if not backup_path.exists():
        raise interpose.BackupMissing(
            f"no wrap backup for {project} at {backup_path} — nothing to "
            "restore. (A wrap run with --no-rewrite never touched the config.)"
        )
    backup = WrapBackup.from_json(backup_path.read_text(encoding="utf-8"))
    secrets_path = store.secrets_path(project)

    def resolve_secret(leaf: str, field_name: str) -> str:
        """Put a relocated credential back where the wrap took it from.

        Raises rather than substituting anything on a miss: every site is
        resolved before any is written, so a credential missing from the store
        aborts the unwrap with the harness untouched — far better than writing
        back a config whose `env` holds a marker object instead of a value.
        """
        try:
            stored = _json_object(store.read_secrets(project)[leaf])
            return str(stored[field_name])
        except (KeyError, ValueError, TeghStoreError) as exc:
            raise interpose.InterposeError(
                f"the credential this wrap moved out of your config "
                f"({leaf}.{field_name}) is no longer in "
                f"{secrets_path} ({exc}). Nothing was restored — "
                "put the value back in that file, or edit the backup at "
                f"{backup_path} to hold the literal, then re-run `tegh unwrap`."
            ) from exc

    return UnwrapPlan(
        project=project,
        backup=backup,
        backup_path=backup_path,
        secrets_path=secrets_path,
        audit_path=store.audit_path(project),
        sites=interpose.plan_restore(backup, resolve=resolve_secret),
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


def render_plan(plan: UnwrapPlan) -> str:
    return "\n".join(
        [
            f"tegh unwrap will change, for {plan.project}:",
            "",
            *_lines(plan.actions, done=False),
            "",
            "It leaves untouched:",
            "",
            "  tegh.lock and the admitted rows, so re-wrapping does not re-run "
            "the ceremony",
            f"  the audit tape at {plan.audit_path}",
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


def confirm(question: str, *, prompt: Optional[Prompt] = None) -> bool:
    """Ask on the terminal. A non-interactive stdin is a NO, never an implied yes.

    The same contract as the prompt `tegh approve` reaches in the base
    (`safe_agents.broker.approval.release_cli`): where nobody can type, silence
    means no human agreed, and `--yes` is how one says so in advance. Every
    negative answer prints one line saying nothing was changed.

    `prompt` stands in for the terminal when a caller supplies one.
    """
    sys.stdout.flush()
    if prompt is None:
        if not sys.stdin.isatty():
            print(
                "REFUSED: stdin is not a terminal, so nobody can answer this "
                "prompt. Nothing was changed. Pass --yes to state the approval "
                "up front.",
                file=sys.stderr,
            )
            return False
        prompt = input
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


def apply_plan(store: TeghStore, plan: UnwrapPlan) -> int:
    """Carry the plan out in the order that cannot lose a credential."""
    held = (
        f"The credential is still in tegh's store at {plan.secrets_path}, and "
        if plan.credentials
        else "Nothing was removed from tegh's store, and "
    )
    try:
        # Writes, then reads every site back and compares. Returning is the
        # verification; see `interpose.apply_restore`.
        interpose.apply_restore(plan.sites)
    except (interpose.InterposeError, OSError) as exc:
        print(
            f"FAILED: the harness config was not restored: {exc}\n{held}the wrap "
            f"backup is still at {plan.backup_path}. Run `tegh unwrap` again "
            "once that is fixed; it picks up from what is on disk.",
            file=sys.stderr,
        )
        return EXIT_NOT_UNWRAPPED

    if plan.credentials:
        fields_by_leaf: dict[str, list[str]] = {}
        for credential in plan.credentials:
            fields_by_leaf.setdefault(credential.leaf, []).append(credential.field_name)
        try:
            store.remove_secret_fields(plan.project, fields_by_leaf)
            remaining = store.read_secrets(plan.project)
            left = [
                _credential_name(credential)
                for credential in plan.credentials
                if credential.field_name
                in _json_object(remaining.get(credential.leaf, "{}"))
            ]
            if left:
                raise TeghStoreError(f"still present after the write: {', '.join(left)}")
        except (TeghStoreError, ValueError, OSError) as exc:
            names = ", ".join(_credential_name(c) for c in plan.credentials)
            print(
                "FAILED: the harness config is restored, but a credential could "
                f"not be removed from tegh's store: {exc}\n"
                f"{plan.secrets_path} still holds a copy of {names}. Run `tegh "
                "unwrap` again to retry the removal (the wrap backup at "
                f"{plan.backup_path} is kept for that), or delete those entries "
                "from that file by hand.",
                file=sys.stderr,
            )
            return EXIT_NOT_UNWRAPPED

    try:
        plan.backup_path.unlink()
    except OSError as exc:
        print(
            "FAILED: the harness config is restored, but the wrap backup at "
            f"{plan.backup_path} could not be removed: {exc}\nDelete that file "
            "by hand. A second `tegh unwrap` would try to restore from it.",
            file=sys.stderr,
        )
        return EXIT_NOT_UNWRAPPED

    print(render_report(plan))
    return EXIT_OK
