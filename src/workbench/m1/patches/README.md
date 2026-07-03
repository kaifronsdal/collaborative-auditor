# Fork patches

The workbench's editable deps (`[tool.uv.sources]` in the root
`pyproject.toml`) are local checkouts of `inspect_ai` and
`inspect_petri` that carry a handful of commits not yet on the upstream
`main`. This directory captures each of those commits as a standalone
patch so a fresh checkout can be reproduced without the fork branches,
and so the delta from upstream is auditable in one place.

Each `.patch` is `git show --format='' <commit>` prefixed with a plain-
text rationale. Apply with `git apply` or `patch -p1` from the dep's
repo root.

## `inspect_ai`

Fork branch: `kaifronsdal/inspect_ai@model-event-output-streaming`
Merge-base with `UKGovernmentBEIS/inspect_ai@main`: `cc8c367e`

Only the `model-event-output-streaming` stack remains on the fork
(`4a67234b`..`bca65a6c` + merge `a3e631c0`) — 8 commits that stream
partial `ModelOutput` onto the pending `ModelEvent` for live token
display. Not workbench-specific; PR pending upstream.

The three concurrent-`eval_async` patches (`inspect-eval-async-guard`,
`inspect-init-active-samples-noop`, `inspect-active-sample-store` —
commits `fa94cd82`/`4636d9a6`/`536002a8`/`1a36c4dc`) were **dropped and
reverted on the fork** at M1-HYBRID step 6: evals now run in
subprocesses via the `bash` tool, so in-process concurrent `eval_async`
is no longer needed. See `CONCURRENT-EVAL-DESIGN.md` for the ~10
process-global hazard sites that made that approach unviable.

## `inspect_petri`

Fork branch: `meridianlabs-ai/inspect_petri@tape-replay-v2-l2-history`
Merge-base with `origin/main`: `a5af7826`

| patch | commit | what | why the workbench needs it | upstream |
|---|---|---|---|---|
| `petri-auditor-prelude.patch` | `f228ec3` | Export `auditor_prelude()` + `run_turn_tools()` from `inspect_petri._auditor`. | `workbench_auditor` layers step-gating and divergent-serve emit on top of the standard petri turn; it imports these two helpers instead of duplicating the ~45-line setup block and reaching into `_auditor.agent` privates. | not yet PR'd; sits atop the tape stack (meridianlabs-ai/inspect_petri#111) |

The rest of the fork branch (`7d80f26`..`f60b274`) is the tape/resample
stack, PR'd upstream as meridianlabs-ai/inspect_petri#110 / #111.

## Regenerating

```sh
git -C ~/GitHub/inspect_ai    show --format='' <commit> > <name>.patch
git -C ~/GitHub/petri-meridian show --format='' <commit> > <name>.patch
```

then prepend the rationale header. When a patch lands upstream, delete
it here and bump the merge-base.
