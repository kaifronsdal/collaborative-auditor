# M1 e2e findings — `m1-e2e-0702` v5b (2026-07-02)

Structural pass (exit 0, 244s). Bugs surfaced, not UI:

## `RunHandle.running_ids` never populates — **fixed**
`handle.rows` listed `['1#1', '1#2']` while samples were actively
running (17-msg conversation), but `running_ids` stayed `[]` for the
full 10s poll. `wb.steer` therefore no-op'd. Root cause:
`RunHandle._poll` filtered `active_samples()` by
`log_location.startswith(self.log_dir + os.sep)`; inspect may
relativise `ActiveSample.log_location` against `os.getcwd()`
(`_eval/task/run.py:profile.log_location`), so a caller-passed
`log_dir` won't reliably prefix it. Now matches on
`s.task == self.task_name` (unique per handle for `run_audits` —
`audit-{uuid6}`) with the path check as an OR fallback.
`_smoke_m1_run.py` cell 2 now asserts `h2.running_ids` non-empty
mid-run; `_e2e_m1_real.py` polls `rows.running` on the audit_run card.

## `wb.run_audits` missing `model_roles["judge"]` — **fixed**
`audit_judge()` needs `model_roles={"judge": …}`; the task built in
`wb.run_audits` didn't pass it → every sample errored at grading with
`Model role 'judge' is required`. Added `"judge": auditor_model or
model` to the `model_roles` dict.

## `wb.steer` no visible receipt — **fixed**
Returned `<no output>` even though the prompt promises one. Now always
`display(Markdown("→ steered N/M running samples (K not running)"))`
regardless of match; still writes to `CONTROL` for every id (a sample
may not have started yet). `_smoke_m1_run.py` cell 2 asserts the
receipt appears in the model-facing render; `_e2e_m1_real.py` greps
python-tool results for `→ steered`.

## `_n_assistant_turns` over-counts — **fixed**
Counted every non-pending `ModelEvent`. Tenacity's `@retry` wraps the
*inner* `generate()` (which itself calls `_record_model_interaction`),
so each failed attempt under backoff emits its own completed
`ModelEvent` with `error` set and a fresh uuid — v5 showed "8 turns"
during 600s of pure retry. Now filters `and not e.get("error")`.

## `pyproject.toml` — fixed @ this commit
Orphaned `exclude-newer-package` with no global `exclude-newer` →
`uv sync` re-resolve fails against editable inspect_ai fork version.
Removed.

## Gate + `waiting` broadcast — **exercised** (`--gate`)
`_e2e_m1_real.py --gate` asks for `GATE_THRESHOLD + 2` seeds so a
`RunProposal` gate opens; the poll loop auto-resolves it with
`{"surviving": ["s0", "s1"]}` (trims to 2 seeds). `Gate.on_change` is
wrapped to record `orch.status` at each fire; asserts `"waiting"`
appears (the property overlay from `6a22676`) and that the poll loop
independently observes `orch.status == "waiting"` while
`gate.pending` is non-empty.

## Not exercised (need targeted tests)
- RunProposal seeds render (frontend-only; screenshot harness covers)
