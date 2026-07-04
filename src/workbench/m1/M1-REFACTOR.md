# M1 refactor plan — consolidated from 4 review agents (2026-07-04)

Post-M1-HYBRID. Frontend ~410 LOC, backend ~195 LOC, tests ~280 LOC,
plus structural (arch Q1-Q8). Batched by dependency + risk.

## Batch A — file reorg (arch Q2+Q5+Q7, backend #4/#5) ~3h

One mechanical PR that groups by domain and kills the proposal
duplication:

- **New `m1/proposals.py`**: `Gate`, `Proposal` protocol, `Prompt`
  (from `kernel.py`), `RunProposal` (from `run.py`), `CiteProposal`/
  `Quote`/`Finding`/`cite()` (all of `cite.py`), + `async
  review_seeds(gate, seeds, desc, config) -> dict` shared core.
  `BaseProposal` dataclass with `id`/`verdict`/`pending`/`_bundle()`.
- **`m1/run.py` → `m1/handles.py`**: `SampleRow`, `_PollingHandle`,
  `ScanHandle`, `_finite_or_none`/`_first_numeric`. (`RunProposal`
  moves to `proposals.py`.)
- **`workbench/gate.py` → `workbench/step.py`** (`StepGated` only).
- `Orchestrator.gate` (not `kernel.gate`); `server.py` reaches
  `session.orchestrator.gate.resolve(...)`.
- `tools.py` `review_seeds`/`review_finding` call the shared cores +
  `_turn` + `json.dumps`; `wb.review_seeds`/`wb.cite` call the same
  cores directly. `cite.py` deleted.

Net ~-60 LOC, one construction path per proposal.

## Batch B — kernel boundary (arch Q1) ~2h

- `OrchestratorKernel.turn()` public CM (bump counter, `outputs.
  setdefault`, set/reset `_current_turn`). `run_turn()` uses it.
- `kernel._emit` → `kernel.emit` (public).
- `Orchestrator.record_turn(tid)` encapsulates the `_turn_msg` write.
- `tools._turn(orch)` → `with orch.kernel.turn() as tid:
  orch.record_turn(tid); yield tid`.

Kills 8 `SLF001`. Independent of A.

## Batch C — wire contract (arch Q8, backend #3) ~3h

- **New `m1/wire.py`**: `WB_MIME`/`STREAM_MIME` constants +
  `DisplayEvent` (breaks `kernel↔hooks` cycle) + one `TypedDict` per
  `kind` (13: `PromptPayload`, `RunProposalPayload`, `EvalRunPayload`,
  `SampleRowPayload`, `ScanPayload`, `CitePayload`, `FindingPayload`,
  `TranscriptPayload`, `ExcerptPayload`, `TracebackPayload`,
  `CellDonePayload`, `BgDonePayload`, `RewindMarkerPayload`) +
  `WbPayload` union + `wb_bundle(text, payload) -> dict`.
- Every `_repr_mimebundle_` returns `wb_bundle(...)` (9 sites).
- `tools._fold_eval` and `AttachedRun._repr_mimebundle_` both build
  `EvalRunPayload` — mypy catches drift.
- `frontend-wb/src/components/orch/types.ts` hand-mirrors the
  TypedDicts (fill in per-kind fields). Add a smoke asserting every
  emitted `kind` is in `get_args(WbPayload)`.

Best done after A so TypedDicts land in `proposals.py`/`handles.py`.

## Batch D — backend reuse (backend #1/#2/#6-#10) ~3h

- `attach.py` ACP client → `acp.Connection.send_request` (pattern:
  `inspect_ai/agent/_acp/tui/client.py:278`). ~-50 LOC.
- `_audit_task.py` → `return inspect_petri.audit(seed_instructions=…,
  **cfg)`. **Verify first**: `import_eval` (M0 replay) doesn't need
  `workbench_auditor`'s `TURN_END_SOURCE` anchors in batch logs.
  ~-35 LOC.
- `_PollingHandle._done()` hook → `AttachedRun` overrides `_done()`
  instead of `_watch()`. ~-15 LOC.
- `wb_display.py` subclass `LogDisplay` (override every `logging.
  info`-emitting method). ~-15 LOC, marginal.
- `_finite`/`_finite_or_none` → one recursive `_finite` in
  `handles.py`. `_session_dir` → inline in `Orchestrator.__init__`.

## Batch E — frontend dedup ~4h

- **`CodeCell`/`BashCell` → `<CellShell>`** + `useCollapsedFirst()`
  hook. ~-55 LOC. `OrchTurn.tsx` only.
- **`SeedPeek`/`QuotePeek` → `<CheckListPeek<T>>`** + `Variant.rows`
  (built once in `variant()`, `ReviewModal` reads it). ~-40 LOC.
  `GateCard.tsx` only.
- `resultText`/`contentText` → `../tool-renderers/util.ts`; delete
  `ToolPair.tsx`'s copy. ~-22 LOC.
- `basename` → `@tsmono/util` (2 sites). `autosizeTextarea` →
  `@tsmono/util` (2 sites). ~-12 LOC.
- `<ComposerTextarea>` (autosize + Enter/Shift-Enter) shared between
  `OrchColumn`/`DeskView`. ~-15 LOC. Don't over-parametrise the full
  composer.
- `.rl-dot-*` → all 7 states in `styles.css`. ~-4 LOC.

## Batch F — frontend ts-mono reuse ~2h

- `Modal.tsx` → `@tsmono/react` `Modal` (rename `open→show`/
  `onClose→onHide` at 3 callsites; `ComponentIconContext` provider
  once at app root). ~-110 LOC. Visual pass required.
- `CopyBtn` → wrap `@tsmono/react` `CopyButton` (upstream thunk +
  `stopPropagation` first, or keep as 25-LOC wrapper).

## Batch G — CSS utilities + consolidation ~2h

- `orch.css`: `.hstack` (`display:flex; align-items:center`) + `.g4`…
  `.g14` gap variants (17 sites) + `.truncate` (13 sites). ~-85 LOC.
- After E's `<CellShell>` merge, ~25 more selectors drop.
- **Also**: fold the 3 `/* -- {agent} additions -- */` EOF marker
  blocks into their sections + delete 8 dead rules + merge 6 dup
  selectors (analysis done by prior agent; deferred here after 3×
  gateway timeout on the ~900-line rewrite — do it as targeted Edit
  calls, not a whole-file Write).

## Batch H — test fixtures + coverage ~4h

- **New `m1/_fixtures.py`**: `orch_by_turn(turns)` (5 copies),
  `@asynccontextmanager mock_orch_session(turns, **kw)` (6 sites),
  `wait_gate(k)` (6 inline), `_wb_events`/`_tool_call`/`_wait_for`/
  `_settle`/`spawn_demo_eval`/`demo_eval_cmd`. ~-280 LOC across 8
  files.
- `HYBRID_CORE_TURNS(n)` shared between `_screenshot_m1.TURNS` and
  `_smoke_m1_hybrid._run_e2e`.
- **HIGH-risk coverage** (add smokes): `bash(background=True)`,
  `read.py` (`wb.transcript`/`excerpt`), `wb_display` erroring
  sample, `attach.py` ACP error paths, `server.py` M1 handlers
  (`resolve_gate`/`orch_detach`/`cancel_bg`), `wb.cite`,
  `plots.by_model`/`model_label`.
- **pytest-anyio migration** (thin wrap): `tests/conftest.py` +
  `tests/test_m1_*.py` calling into existing `_smoke_*._amain`; keep
  `__main__` entries. `pytest -m "not slow"` for CI.

## Batch I — Session extraction (arch Q3) ~2h, optional

- `build_auditor_timeline` → `workbench/timeline.py`. ~-75 LOC.
- `Session.save/load` → `workbench/persist.py` (mirror
  `m1/persist.py`). ~-100 LOC.

## Not doing

- `renderContent` → `@tsmono/inspect-components` `MessageContent`
  (too heavy, visual regressions).
- `bash`/`read_file` → inspect's (sandbox-bound, wrong exec model).
- `RunRow`/`ScanRow` merge (insufficient overlap).
- Display pipeline collapse (arch Q4 — each hop earns its place).
- Full `<Composer>` extraction (state machines genuinely differ).

## Order

A → B (independent) → C → D → E → G → F → H → I. A+B can run in
parallel (disjoint files). C depends on A. E/G/F are frontend-only,
independent of backend batches. H last (fixtures land in the
post-refactor file layout).

Total: ~24h across 9 batches; ~850 LOC net removal + contract
enforcement + 8 SLF001 gone + 7 coverage gaps closed.
