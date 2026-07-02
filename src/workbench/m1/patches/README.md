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

| patch | commit | what | why the workbench needs it | upstream |
|---|---|---|---|---|
| `inspect-eval-async-guard.patch` | `fa94cd82` | Lift the `_eval_async_running` reentrancy guard in `eval_async`. | `wb.run_audits` / `wb.run_eval` call `eval_async` from inside the orchestrator kernel while a session `eval_async` may already be live; `gather(run_audits×2)` (M1-RUN-AUDITS §guard) needs overlapping calls. | not yet PR'd |
| `inspect-init-active-samples-noop.patch` | `4636d9a6` | `init_active_samples()` → no-op. | With the guard lifted, a second concurrent `eval_async` would otherwise `.clear()` the first's `ActiveSample` list and the first eval finishes `status='error'`. Also keeps `active_samples()` usable for reaching into a concurrent eval's samples. | not yet PR'd |
| `inspect-active-sample-store.patch` | `536002a8` | `ActiveSample` carries the sample's `Store`. | `{t:"import_running"}` snapshots a live petri `AuditTape` (which lives in the sample `Store`) from `active_samples()` without waiting for the `.eval` flush (M1-RUN-AUDITS §Desk). | not yet PR'd |

The fork branch also carries the older `model-event-output-streaming`
commits (`bf845b99`, `8ab09182`, `bca65a6c`) that predate M1; those are
not workbench-specific and are tracked separately.

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
