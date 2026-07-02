# M1 refactor notes — upstream reuse + deferred work

From four code-quality/reuse subagents (2026-07-01). Applied in `b1d0868..HEAD`;
what's left is upstream PRs and larger refactors deferred to M1.3.

## Upstream PRs (each unblocks a deletion here)

| Target | Change | Unblocks |
|---|---|---|
| `inspect_petri._task.audit` | `auditor: Agent \| None = None` param → `audit_solver` | `wb.run_audits` stays one line; `batch_auditor` with `drain_control` hook |
| `inspect_petri._auditor.agent.auditor_agent` | `before_turn: Callable[[AgentState, int], Awaitable[bool]]` fired at loop top | delete `CONTROL`/`SampleControl`/`drain_control` (~50 lines in `run.py`) |
| `inspect_petri.__init__` | re-export `render_target_timeline`, `RenderedTarget` — **landed @ `7d27f77`** (PR #4) | `wb.transcript`/`wb.read_transcript` = 3-line wrappers |
| `inspect_petri.util` (new) | `flat_score_values` (from `petri_ukaisi.analysis`) + `audits_df(log_dir)` — **landed @ `f60b274`** (PR #3) | `AuditRunHandle.audits` per-dimension columns |
| `inspect_ai` (upstream candidate) | guard removal + `init_active_samples` no-op — already on `model-event-output-streaming` @ `4636d9a6` | concurrent `eval_async` |
| `inspect_ai.log._samples.ActiveSample` | add `store: Store` field (pass `state.store` at `_eval/task/run.py:1195`) | `import_running` v2 — snapshot a running sample's tape without stopping it |

## Applied from the reviews

- **Dead code:** `_WbNamespace`, `DisplayEvent.wire()`, `_ns_baseline` deleted;
  stale kernel docstrings updated; `Workbench.__repr__` fixed.
- **Naming:** `_gate` → public `gate`; `RunHandle.done` → `n_done`;
  `kind = "eval_run"|"audit_run"`.
- **inspect reuse:** `_wire_bundle` → `jsonable_python` (recurses, so nested
  numpy/bytes coerce too); log discovery → `active_samples()[].log_location`;
  `python_tool` gains `code_viewer` for transcript rendering.
- **`StepGated` mixin** (`gate.py`) — `Orchestrator` and `Branch` inherit;
  `await_step` / `rearm` name the pattern.
- **`_PollingHandle` base + `RunHandle.launch` classmethod** own the
  `display → task → watcher` wiring instead of a free `_launch` reaching
  into privates.
- **`Session.start_orchestrator` / `notify` / `emit` + `Orchestrator.send`**
  — server.py `orch_*`/`step`/`play`/`pause` cases collapsed onto these.
- **`CONTROL` cleanup** — `_watch` pops the run's ids on finish.
- **Deny → return** — `run_audits` on deny returns a settled
  `AuditRunHandle(error="denied: …")` instead of raising, so the model
  reads one line not a traceback.
- **`orch_*` dispatch** now `broadcast_status()` (was silently not).

## Real-model e2e findings (v2→v4 on `m1-e2e-0702`)

- v2/v3: 5/8 cells wasted on unqualified `model=` + `wb.steer([handle.id])`.
  Fixed via prompt (@ `488357e`): model must be fully-qualified;
  `handle.running_ids` for steer targets; `.audits` only after `.wait()`.
- v4: 7 cells, 1 log_dir, `.audits` DataFrame after `.wait()`, steer used
  correct sample id. Remaining: (a) e2e's `_find_operator_message` reads
  `EvalSample.messages` (target conv) but steer goes to the *auditor* —
  false negative; check auditor `ModelEvent.input` instead. (b) `.audits`
  `sample_id` col is inspect's uuid, not the seed `id` — agent passed it to
  `read_transcript`; accept either or note in prompt. (c) no `score_*`
  columns with `audit_judge(None)` at `max_turns=3` — expected.

## Deferred to M1.3

- **`CONTROL` → `AgentChannel`** (inspect-reuse #1, −45 lines) — requires
  `acp_transport` set, which needs `acp_server=True` (binds a socket). The
  petri `before_turn` PR is the cleaner path for `run_audits`; `AgentChannel`
  is the path for `run_eval` on channel-based agents.
- **`_LIVE` → `OrchestratorKernel._instance`** — the guarded resources
  (`InteractiveShell.instance()`, `sys.stdout`) are seized by the kernel,
  not the orchestrator. Plus `close()` context-manager.
- **God-class split** — `Gate` class (pending + gate + resolve),
  `install_workbench_hooks(shell, emit)` free function.
- **`ProposalBase`** — when M1.3 adds `CiteProposal`/`ScanProposal`.
- **`wb.scan`** — real API is `inspect_scout.aio.scan_async` +
  `scan_status_async` polling (not the design doc's fictional
  `iter_progress`). `ScanHandle` follows `RunHandle`'s shape.
- **`wb.excerpt`/`transcript`** — wrap `inspect_scout.span_messages` +
  `inspect_petri.render_target_timeline` + `messages_as_str`.
- **`Verdict` TypedDict**, `OrchView` TypedDict.
