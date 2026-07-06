# M1 cleanup plan — consolidated from 6 review agents (2026-07-06)

Post-M1-REFACTOR (batches A-I) + e2e-v4 green. ~−600 LOC net across
6 batches; also fixes `npm run build` and adds real ruff config.

## Batch J — tooling foundation ~1h

Do first — everything downstream depends on ruff config + tsc.

- **`frontend-wb/tsconfig.json`**: fix duplicate `@types/react`
  identity (verified: `tsc` exits 0). Add to `compilerOptions`:
  `"baseUrl": ".", "paths": {"react": ["./node_modules/@types/react"],
  "react/*": [...], "react-dom": [...], "react-dom/*": [...]}`.
  `exclude` won't work (files reached via import); `skipLibCheck`
  irrelevant (`.tsx` not `.d.ts`). Then `"build": "tsc --noEmit &&
  vite build"` (drop `-b`).
- **`pyproject.toml [tool.ruff.lint]`**: currently absent → 177
  `# noqa` suppress rules that aren't enabled. Add `select =
  ["E","F","W","I","UP","B","SIM","RUF","PL","A","BLE","SLF","ARG",
  "ANN2","S"]`; `ignore = ["PLC0415","PLC2701","PLR2004","PLR0913",
  "S101"]` (lazy imports + private-dep-imports + magic-values +
  arg-count + assert are structural to this project). Add
  `[tool.ruff.lint.per-file-ignores] "src/workbench/_smoke_*.py" =
  ["SLF001","PLR0915","BLE001"]`, `"tests/**" = ["SLF001"]`. Then
  `ruff check --select RUF100 --fix` to strip newly-dead noqa.
- **`pyrightconfig.json`**: add `"extraPaths": ["src"]`; drop
  `"reportMissingImports": "warning"` mask.
- **`pyproject.toml` deps**: drop `openai`, `jsonpatch`,
  `python-dotenv` (0 imports). Keep `aisitools` (inspect entry-point
  hook, needed at runtime); keep `anthropic` as explicit pin.
- **`frontend-wb/package.json`**: drop `@tsmono/tsconfig` dep
  (vite.config.ts:20 documents why it can't help).
- **`scripts/sync-worker.sh`**: `rsync -az` → `rsync -azc` × 3.
- **`frontend-wb/pnpm-workspace.yaml`**: update stale "only
  ./transcript/transform" comment (now imports chat/timeline too).

## Batch K — dead code purge ~2h

Independent of J. Pure deletion; no behaviour change.

- **`audit_run` kind end-to-end** (pre-HYBRID `AuditRunHandle`
  leftover): `m1/persist.py:57` `_RUN_KINDS` → `== "eval_run"`;
  `types.ts:112` drop `"audit_run"` from union; `cards/index.ts:46`
  drop entry; `ProgressCard.tsx:146` `const audit = ...` always
  false → delete + cascaded dead branches at :376/:390/:401-404 +
  `RunPayload` alias :29; `orch.css:622-646` `.audit-row*` (7 rules)
  + `.audit-row` halves of combined selectors :662,669,690.
- **`stop_now` dead branch**: `auditor.py:61,113-115` — only
  implementer returns `False` unconditionally (was `wb.stop`).
  Change `pre_turn` return type to `list[ChatMessage]`; drop the
  `if stop_now: break`. Update `Branch.pre_turn` (run.py:453).
  Rewrite docstrings :9,:65,:86 (cite non-existent `_NoHooks`).
- **`Workbench.__init__(session=)`**: drop param + all 5 callsites
  (leftover from `steer`/`snapshot_running` needing session).
- **`wire.ts:185`** `stop_sample.hard?: boolean` — never sent/read.
- **Dead frontend exports**: `lib/selectors.ts` `usePending` +
  `useEventTree`; `store/session.ts` `useRewriteDraft`;
  `GateCard.tsx:34` `type Quote`; `Output.tsx:28` `meta` prop (+
  callsite).
- **Dead CSS** (`styles.css`): `.branch-divider`/`.bd-*` :780-791,
  `.cfg .c-*` :201-209,397, `.cfg-select`/`.cfg-turns` :212-225,
  `.dest-toggle` :814-819, `.raw-modal-{head,overlay}` :877-895,
  `.side-new-icon`/`.sidebar-dot`/`.sidebar-expand-btn` :107-127,
  duplicate `.wordmark-row .wordmark` :140-142. Add missing
  `.error-banner` rule (used but unstyled).
- **Dead CSS** (`orch.css`): `.g14` :20, `.orch-col .turn` :121,
  `.out-wrap:has(.out-head .out-live)` :338, `.fx-out
  .arrow/.path/.stat.run` :588-594; stale TODO OrchColumn:56-58.
- **`inspect_repr.py:255`** `_HANDLE_TYPES` in `__all__` (0 imports).
- **`CiteProposal.grades_ref`** end-to-end (proposals/wire/wb/
  types.ts) — never rendered, never set by `review_finding`.
  **Verify no pending UI plan first.**
- **Stale `.pyc`**: `__pycache__/{gate,run,cite}.*.pyc`,
  `_smoke_m1_run.*.pyc`.
- **Stale docstrings** citing deleted symbols: `session.py:480`,
  `kernel.py:489-490`, `orchestrator.py:3-13,128,148,514`,
  `persist.py:24,62`, `tools.py:80` ("8 tools" → 7),
  `auditor.py:9,86`. Also `M1-RUN-AUDITS.md` refs in
  `orchestrator.py:148,514` (eval_async leakage no longer applies
  in-process).

Est ~−140 LOC.

## Batch L — backend structure ~4h

Depends on J (ruff config lands first).

- **L1** `server._dispatch_locked` (350-LOC match) → per-case
  `async def _h_<name>(session, data)` at module level; match
  becomes one-liner-per-arm. ~+20 net but each handler <30 LOC.
- **L2** `tools.make_tools` (385-LOC closure) → extract bash
  pipeline (`_stream`/`_evals`/`_fold_eval`/`_card`/`_pump`/`_tail`
  /`bash`, ~230 LOC) to `m1/bash_tool.py` as `make_bash_tool(orch)
  -> Tool`; `_evals` becomes explicit param. `make_tools` → ~60-LOC
  assembler.
- **L3** `Session._by_role` → public `by_role` (5× SLF001).
- **L4** `AttachedRun.discover(log_dir)` (attach + one poll, no
  display/watcher) + `async wait_for_sample(id, timeout)`;
  `server.py` `import_running`/`stop_sample` shrink to 3-5 lines.
  ~−25, drops 3 SLF001.
- **L5** `_first_numeric`/`_finite` → public `first_numeric`/
  `finite` (4× PLC2701).
- **L6** `wire._truncate`/`_MODEL_TEXT_CAP` → public; `kernel.py`/
  `tools.py` import instead of redefining. ~−6.
- **L7** `attach.interrupt_sample` (72 LOC) → extract
  `_acp_request` context helper. ~−15.
- **L8** `Session._cancel_all(tasks)` helper; `close` +
  `_stop_running_branches` call it. ~−12.
- **L9** `Orchestrator.messages_for_save()` → `persist.py` calls
  it instead of reaching into `_rewind_to`/`_turn_msg`. ~−4, 2 SLF.
- **L10** `kernel._settle` (65 LOC) → extract `_emit_traceback`.

Est ~−80 LOC, ~−15 noqa.

## Batch M — frontend structure ~4h

Depends on K (audit_run purge touches ProgressCard first).

- **M1** `RewritePanel` dup (`ModelEventRow.tsx` + `ToolPair.tsx`)
  → `useSelectionPill(hostRef)` hook + `<RewritePanel .../>` in
  `components/RewritePanel.tsx`. ~−100.
- **M2** `LinearColumn`/`SwimlaneColumn` scroll machinery →
  `useColumnScroll(turns, {linked, onSync}, ref)` hook. ~−80.
- **M3** `contentText`/`shortId` dup → import from
  `tool-renderers/util.ts`. ~−10.
- **M4** `cards/index.ts` → collapse `CARDS` (with dead `variant`)
  to flat `cards: Record<string, AnyCard>`; drop payload-type
  re-exports here + from each card file. ~−30.
- **M5** `switchBranch(id)` store action (3 sites). ~−10.
- **M6** `.stat.err` inline `style={{color:...}}` × 3 → drop
  (selector already sets it); `.fx-dot.err` class instead of
  inline. ~−5.
- **M7** `ModelPicker` clear-buttons → `updateConfig({field:
  undefined})`; extract `<NumRow field label {...}/>`; extract
  shared `row(m)` for Models/Recent lists. ~−45.
- **M8** `types.ts` `ScanPayload.elapsed?: string` ↔ `wire.py`
  drift — add to Python (or drop from TS if never emitted).
- **M9** `FindingCard.tsx:35` `.gate-resolved` has no rule → use
  `.truncate` directly.

Est ~−280 LOC.

## Batch N — cross-repo reuse ~2h

Depends on M (touches same frontend files). Skip anything that
pulls new heavy transitive deps.

- **N1** `_flatten_content(v.content)` → `v.text`
  (`ChatMessage.text` property). Drop-in; delete helper. ~−8.
- **N2** `wire._truncate` → `inspect_ai._util.text.truncate(...,
  overflow="…", pad=False)`. ~−3. (Composes with L6.)
- **N3** `<pre>{JSON.stringify(v,null,2)}</pre>` × 3 →
  `<JSONPanel data={v}/>` from `@tsmono/react`. ~−3 + Prism
  highlighting for free.
- **N4** `CollapsibleContent` → `@tsmono/react` `ExpandablePanel`.
  Adapter needed (`id`+`lines` vs `maxHeight`); we already provide
  `componentStateHooks` at `lib/inspectState.ts:44`. ~−52. **Only
  if visual parity holds** — screenshot before/after.
- **Skip**: `Markdown`→`MarkdownDiv` (pulls markdown-it +
  mathjax3; `marked` is lighter and works).

Est ~−65 LOC.

## Batch O — test hygiene ~3h

Depends on L (L4 changes what `import_running`/`stop_sample` tests
cover).

- **O1** Hardcoded `/tmp/` paths → `tempfile.mkdtemp` + `finally
  rmtree`: `_smoke_m1_hybrid.py:259,263`; fixed session_dir names
  :128,470 → `f"smoke-hybrid-{os.getpid()}"`.
- **O2** `_smoke_m1_hybrid.py:288` bare `sleep(0.5)` → drop
  (`AttachedRun` handles empty-dir grace).
- **O3** `_smoke_m1_coverage.py:162,184` `create_subprocess_shell`
  + SIGKILL → `_exec` (like `_run_interrupt`) so kill reaches
  inspect directly.
- **O4** Leaked `mkdtemp`: `_smoke_m1_{orchestrator,features,scan}`
  → `rmtree` at end of `_amain`.
- **O5** `_smoke_m1_hybrid.py:458` `_wait_tool` (uses deprecated
  `get_event_loop()`) → rewrite via `_fixtures.wait_for`.
- **O6** `_fixtures.py`: add `wait_running(h, timeout=10)` (poll
  `h._poll()` until `running_ids`); replace inline loops in
  `_smoke_m1_hybrid.py:378` + `_smoke_m1_coverage.py:170`.
- **O7** `_fixtures.py`: add `tool_result_text(ev)`; replace dups
  in `_smoke_m1_orchestrator.py:210` + `_e2e_m1_real.py:319`.
- **O8** `_screenshot_m1.py:193-204` → `mock_orch_session` (with
  cwd restore).
- **O9** `tests/test_m1_{hybrid,coverage,orchestrator,scan,
  streaming}.py` → collapse to one `tests/test_m1_smokes.py`
  parametrized over modules (with per-param `slow` marks).
- **O10** `conftest.py`: drop unused `kernel`/`orch_session`
  fixtures (0 consumers). `_fixtures.__all__`: drop `tool_call`.
- **O11** Coverage: extend `_check_server_handlers` with
  `stop_sample`/`import_running` cases; merge subprocess bootstrap
  with `_check_acp_errors` (halves `slow` runtime).
- **O12** `_smoke_m1_hybrid.py:236` — exercise
  `read_file(offset=, limit=)`.

Est ~−60 LOC net, +1 coverage path.

## Not doing

- `Markdown`→`MarkdownDiv` (heavy transitive deps).
- `resultText`→`toolOutputText` (not output-equivalent).
- Prose block-actions padding-right (accept — claude.ai does same).
- Delete `CONCURRENT-EVAL-DESIGN.md` / `M1-RUN-AUDITS.md`
  (intentionally-retained rationale docs; cited from HYBRID.md).
- STATUS/TURNS headers hide when all rows done (minor UX; defer).

## Order

J + K in parallel (disjoint). L after J. M after K. N after M.
O after L. Screenshot recapture after M+N.

Total: ~16h across 6 batches; ~−600 LOC + `npm run build` fixed +
real ruff config + ~200 dead-noqa stripped.
