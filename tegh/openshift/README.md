# tegh/openshift — the posture leg of the OpenShift arm

These files are an overlay onto the OpenShift arm in the public reference implementation,
`safe_agents/arms/openshift/` in `wjatx/ptc-gal-reference`, at the release tag of the version
`pyproject.toml` pins. They
are not a Python package and are not an arm of their own. The leg reports posture through `tegh`,
which the reference implementation deliberately does not ship, so the leg lives here with tegh.

| File | What it is |
|---|---|
| `59-job-posture.yaml` | The Job: runs under the agent ServiceAccount with the agent pod label, so the report is generated inside the same egress policy and SCC as the workload it describes. |
| `cluster-posture.sh` | The leg script the Job runs from the drill ConfigMap. It sources the arm's `negative-proof.sh` from its own directory. |
| `expected-output.log` | A transcript, not a diff target. See below. |

## Running the leg

1. Copy `59-job-posture.yaml` and `cluster-posture.sh` into the reference implementation's
   `safe_agents/arms/openshift/` directory, at the pinned ref.
2. Add `cluster-posture.sh` to the `safe-agents-drill-scripts` entry under `configMapGenerator` in
   that directory's `kustomization.yaml`. The file list is explicit, and kustomize refuses a listed
   file that is missing, which is why the reference implementation leaves it out rather than
   commenting it out.
3. Run `cluster-arc-run.sh` as usual. Its step 15 already checks for `$HERE/59-job-posture.yaml` and
   runs the leg when the file is present.

The leg also needs `tegh` on `PATH` inside the image the Job runs (`safe-agents-missileer:arc`), or
set `POSTURE_CLI` to where it is. The reference implementation's images do not install tegh, and
without it `cluster-posture.sh` prints `posture leg SKIPPED` and exits 0. A skipped leg is not a
passed one, so check the output for that line before reading the step as evidence.

## About `expected-output.log`

It was captured from a copy of the arm kept in the repository tegh was split out of, before that
copy was removed in favour of the reference implementation. It is a historical record. Do not diff a run of the reference
implementation's arm against it: the two drivers have diverged, and a mismatch would say nothing
about either. Three edits were made to it after capture: the home directory in the two
"Uploading directory" lines was replaced with `/Users/you`, the cluster API hostname on the
`server=` line of the preflight step was replaced with `api.cluster.example.com`, and the issue
numbers of a private tracker were removed from ten printed lines, leaving the rest of each line
as printed.

Read it with care: at step 15 it records `(skipping the posture leg: not present in this tree)`,
and the `PHASE 6.1 PREDICATE: PASS` block at the end was still printed. The driver that produced it
printed that block unconditionally. The reference implementation's driver now prints it only when
the leg ran. This transcript therefore does not show the posture leg running. The skip happened
even though the tree it ran from shipped `59-job-posture.yaml`; the likely cause is the relative-`$0` check
that the reference implementation's driver has since replaced with `$HERE`.
