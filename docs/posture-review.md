# Posture claim reviews

`tegh posture` prints claims about what holds for a project and what does not. Each claim carries
a citation, and `tegh/tests/test_posture_citations.py` checks that the citation resolves: the file
exists, the symbol is defined, the issue is still open. No test checks that the claim is true. The
claims are about absence and about where a boundary sits, and those are read off the code by a
person, not derived from it.

This file is the record of that reading. It is an attestation: a named step, done at a fixed
moment, leaving a dated entry that `git blame` attributes.

## How a review is done

**When.** Before every release. `scripts/check_posture_review.py` runs in the release workflow
and refuses a tag whose version has no entry here, or whose entry names a different platform
version from the one `pyproject.toml` pins. A review may be recorded at any other time with
`Release: none`.

**What is read.** Every `PostureLine` in `tegh/posture.py`, `tegh/cluster.py` and
`tegh/harnesses/`, with its module and function docstrings, and the sentences in `README.md` that
restate them. Each claim, with its detail, is read beside the code or document its source names,
at the platform version the project pins.

**What is asked of each claim.**

- Does the code do what the claim and its detail say, today?
- Does the cited place still say what the claim relies on? A line range that resolves can have
  moved onto unrelated text. Cite a symbol (`path.py::name`) where one exists.
- Is anything stated as a cause or a fact that the code did not establish?
- Is a gap described as open that has closed, or a property described as holding that has not
  been attempted?

A claim that fails is fixed before the release or recorded here as open with where it is tracked.

**Who.** Someone who did not write the claims under review, where that is possible. A reader who
wrote a claim reads their intention into it.

**What an entry holds.** A second-level heading with the date, then these lines, which the release
check reads:

    Release: <version being released, or none>
    Platform: safe-agents <pinned version>
    Commit: <the commit the claims were read at>

followed by what was in scope, what was not read and why, and the findings.

**What the gate does not establish.** It establishes that an entry exists for the release. It does
not establish that the reading was careful. An entry that finds nothing across every claim is more
likely a shallow reading than a clean report, and should say what it did to look.

## 2026-10-08

Release: none
Platform: safe-agents 0.75.0
Commit: e4bf462

The first review, read after 0.3.0 was released. The next release needs its own entry.

**Scope.** Every posture line and docstring in `tegh/posture.py`, `tegh/cluster.py`,
`tegh/harnesses/__init__.py` and `tegh/harnesses/claude_code.py`; the posture table and the
sentences about `tegh posture` in `README.md`. About 75 claims. Platform sources were read at the
`v0.75.0` tag.

**Not read.** The harness reference was read only in the sections the durability line cites, so
adapter comments about vendor behaviour outside those sections were marked unestablished and not
ruled on. No claim was exercised end to end: nothing was run on a cluster, and no wrapped session
was started. The findings below come from reading code beside prose.

**Method.** Three readers took one part each and returned a verdict per claim with the lines they
read. Every finding recorded below was then checked against the source a second time by a
different reader before anything was changed.

### Claims that said more than the code did

| Claim | What the code did | Outcome |
|---|---|---|
| "No credential, key, store or audit tape is mounted into this pod" (`cluster.py`) | Checked for `/var/lib/tegh`, `/var/lib/tegh-grants` and `/var/lib/tegh-audit`. The platform's manifests mount `/var/lib/broker`, `/var/lib/broker-grants` and `/var/lib/broker-audit`, so a pod holding the store and the audit tape was reported as holding nothing. | Fixed. The paths are the ones the manifests mount, with `/run/issuer-private` added. A test fails for each mount that goes unreported. |
| "this project's harness config names the gateway as its only MCP server" | Read the local-scope block alone. Claude Code loads the project and user blocks beside it, so a server added at either scope after a wrap left the line at yes. | Fixed. All three blocks are read. A gateway beside other servers, and a config that cannot be read, are each reported as their own state, below posture 1. |
| "MCP calls are gated on this one machine" (the posture line itself) | Only calls to tools the gateway serves are gated. A plugin's server or a claude.ai connector is not behind it. | Fixed in the wording, here and in `README.md`. |
| The built-ins line's sentence on which reads taint the turn | One part of it is wider than the code. | Open, and tracked privately until it is fixed. |

### Citations that had moved

Seven line ranges resolved and pointed at unrelated text: five into `tegh/store.py` and
`tegh/cli.py`, and two into the platform's decision engine and broker server. The citation test
passed throughout, as its own documentation says it would. Two of the seven were on the polarity
lines, which the citation test had never been given.

Fixed. The platform citations name symbols now, and the citation test resolves a symbol in any
cited file, in the installed platform package for a platform path. The polarity lines, every
interposition state and the posture line are in the set it checks. Ranges into docstring
paragraphs, which have no symbol, were corrected and will move again.

### Prose that had gone stale

- The module docstring of `posture.py` listed four gaps as unconditional. There are five, and the
  first is read per project.
- The durability line predated the reference's later observations, described one permission mode
  as if it were the only one, and did not mention the hook entry a wrap now writes.
- `claude_code.py` said the rewrite half of a wrap was not built, and dated an observation two
  days earlier than the reference does.
- `README.md` said the cluster leg's report reads posture 1. The leg's project is not wrapped, so
  the report derives a posture below 1.
- Comments beside the mount table cited manifest lines for more than those lines hold.

All fixed.

### Lines that asserted a cause the code had not established

- An unsigned lock was reported as "written with `--allow-unsigned`". The report reads only that
  the signature file is absent, which has three possible causes.
- The verify-keys gap said the signature rules are skipped. They run when the calling environment
  names a verify-keys file.
- The admission review printed an upstream description under "this text steers the model". On a
  wrapped project the gateway serves its own short description and none of the server's text.

All reworded.

### Left as found

- `cluster.py` describes the agent pod's egress as constrained "to the gateway". That is this
  arm's network policy and not the ladder's definition of posture 2; the policy was not read to
  confirm it.
- The cluster note takes one yes-or-no for interposition, so in a pod a gateway beside other
  servers is described as no call being routed.
- Three adapter comments about vendor behaviour (the environment a spawned server receives,
  ownership of a managed configuration file, plugin cache contents) have no line in the reference
  to check them against.
