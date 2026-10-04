"""Per-harness discovery adapters — one module per coding harness tegh wraps.

`tegh/discovery.py` holds the harness-agnostic half: the result
vocabulary, the `mcpServers` entry parser, and the shadowing resolver. This
package holds the other half — where a *particular* harness keeps its config,
what its scope ladder is, and which of its keys govern approval and enablement.

**The Claude Code adapter is descriptor #1, not the shape of the interface.**
v1 wraps Claude Code only, and more adapters follow (OpenClaw, OpenCode,
Hermes, Cursor). The seam exists now, with one adapter and a full conformance
suite behind it, precisely because it is far cheaper to draw here than to
extract later from a core that has already grown a Claude Code accent.

Each adapter is expected to expose a `discover(...) -> DiscoveryResult` entry
point. There is deliberately NO shared ABC yet: one implementation is not
enough evidence to know which parts of the signature are common and which are
Claude Code's. The abstraction is owed to adapter #2, which will show it.

    from tegh.harnesses.claude_code import discover

Adapters also supply their **wrap-durability line** for `tegh posture` — can the
wrapped agent undo its own wrap? That answer is a property of the harness, not
of tegh, and it differs sharply between them, so it lives beside the adapter
rather than in a table posture owns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tegh.lock import Harness
    from tegh.posture import PostureLine


def durability_line(harness: "Harness") -> "PostureLine":
    """This harness's wrap-durability claim, or an honest refusal to make one.

    An unknown harness returns `unknown` rather than a default: inheriting
    another harness's durability claim is precisely the overclaim the ladder
    exists to prevent, and silence is the correct answer for a harness nobody
    has researched.
    """
    from tegh.posture import PostureLine  # noqa: PLC0415 — cycle

    if harness == "claude-code":
        from tegh.harnesses.claude_code import DURABILITY  # noqa: PLC0415

        return DURABILITY
    return PostureLine(
        claim=f"Wrap durability for {harness!r} is unknown",
        holds="unknown",
        source="docs/references/harnesses/ carries no researched row for this harness",
        detail="no durability claim is made rather than inheriting another harness's",
    )


def gateway_config_site(harness: "Harness", project, home=None):
    """Where this harness's gateway pointer lives, or None if tegh cannot say.

    Dispatched here rather than through a module-level registry for the same
    reason `durability_line` is: an adapter imports `posture` for `PostureLine`,
    so a registry built at import time closes a cycle. None for an unknown
    harness — and callers must read that as "not proven interposed", never as
    "not interposed", because the two differ and only one of them is safe to
    assume.
    """
    if harness == "claude-code":
        from tegh.harnesses.claude_code import config_sites  # noqa: PLC0415

        _, gateway_site = config_sites(project, home=home)
        return gateway_site
    return None
