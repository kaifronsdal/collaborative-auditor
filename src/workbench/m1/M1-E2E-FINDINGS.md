# M1 e2e findings — `m1-e2e-0702` v5b (2026-07-02)

Structural pass (exit 0, 244s). Bugs surfaced, not UI:

## `RunHandle.running_ids` never populates
`handle.rows` listed `['1#1', '1#2']` while samples were actively
running (17-msg conversation), but `running_ids` stayed `[]` for the
full 10s poll. `wb.steer` therefore no-op'd. Suspect: the
`active_samples()` filter matches on `log_location` (a `.eval` file
path) but `RunHandle.log_dir` is the *directory* — check
`RunHandle._running`. Fix: match on `s.log_location.startswith(
self.log_dir)` or on `task_id`.

## `wb.run_audits` missing `model_roles["judge"]`
`audit_judge()` needs `model_roles={"judge": …}`; the task built in
`wb.run_audits` doesn't pass it → every sample errors at grading with
`Model role 'judge' is required`. Add `model_roles={"judge":
auditor_model or model}` to the `Task(...)` (or to `eval_async`
kwargs).

## `wb.steer` no visible receipt
Returned `<no output>` even though the prompt promises one. Check
whether the receipt only prints when `id ∈ running_ids` — if so it
silently swallows misses. Should always print `steered N samples (M
not running)`.

## `_n_assistant_turns` over-counts
Counts every non-pending `ModelEvent`, including retry attempts under
backoff. v5 showed "8 turns" during 600s of pure retry. Cosmetic for
the poll trace but misleading. Filter on `ModelEvent.retries == 0` or
count `ChatMessageAssistant` in `state.messages` instead.

## `pyproject.toml` — fixed @ this commit
Orphaned `exclude-newer-package` with no global `exclude-newer` →
`uv sync` re-resolve fails against editable inspect_ai fork version.
Removed.

## Not exercised (need targeted tests)
- `waiting` broadcast on gate open (n<threshold; add a
  `n_per_seed=10` variant or lower `GATE_THRESHOLD` under a test flag)
- RunProposal seeds render (frontend-only; screenshot harness covers)
