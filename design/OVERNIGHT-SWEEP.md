# Overnight sweep — 2026-07-09 (post STRESS-V2)

6 read-only find agents (A1 wire, A2 dead-code, A3 UI-patterns, A4
error-surface, A5 test-coverage, A6 perf/memory) + 1 loose-ends fixer.
Consolidated into fix-waves B (backend), C1 (frontend store), C2
(frontend components), D (cleanup), E (coverage).

## P0 — bugs we introduced in STRESS-V2

- **[B] E5A** `server.py:288-292` — `_get_or_create`+`push_full_state`
  run *before* the WS handler's `try:`. `Session.load` raise (bad
  `session.json`) → uncaught → socket closes → **auto-reconnect (P2)
  → infinite crash loop**. Fix: move both inside the `try:`; on
  exception, ship `{t:"error"}` + `ws.close(code=4000)` (client
  `onclose` checks `code>=4000` → don't auto-reconnect).
- **[B] E6** `session.py:_resync_all` — catches only WS/conn errors.
  A `view()` exception kills `drain()` → event pipe dead, UI frozen.
  Fix: `except Exception: logger.exception(...); continue` per-conn.
- **[C1] E20** `session.ts:send()` — `pending` appended but `ws.send`
  skipped when `ws` not OPEN → button greys, connect completes,
  `state` resets `pending:[]` → command silently eaten. Fix: if not
  OPEN, queue in `_outbox: Up[]`; `onopen` flushes it. Don't append
  to `pending` until actually sent.

## P1 — perf feedback loop (real at 100 turns × 10 branches)

Chain: `case "update"` → `new Map(events)` (#13) → `useSwimlanes`
deps on `s.events` (#16) → full timeline recompute × 3 → 200 rows
re-render (#23) → each row's `useTurnScores` scans events (#17) →
main thread saturates → WS receive backs up → drain fills → `_desync`
→ `push_full_state` (~100MB) → worse.

- **[C1] P13** `session.ts:698,720` — stop `new Map(state.events)`
  per frame. Mutate the Map in place; add `eventsRev: number` bumped
  on **structural** change only (`event`/`pool`/`rewound`, not
  `update`). Zustand ref-compare means `useSession(s=>s.events)`
  stops firing — that's the point.
- **[C1] P12** `session.ts:680` `case "pool"` — stop
  `resolveAll`+`buildByRole` full rebuild. `resolveAll` only needs to
  re-run on the events whose `input_refs` newly resolve (i.e., events
  with `pending===true` before this pool delta). `buildByRole` should
  never full-rebuild; `assignByRole` already handles incremental.
- **[C1] P17** — add `state.turnScores: Record<uuid, TurnScore[]>`
  index, populated in `case "event"` when `payload.t==="turn_score"`.
  `useTurnScores(uuid)` becomes `useSession(s=>s.turnScores[uuid])`.
- **[C2] P16** `selectors.ts:useSwimlanes` — key `useMemo` on
  `[timelines, eventsRev, branches]` not `s.events`. Fetch events
  via `useSession.getState().events` inside the memo body.
- **[C2] P23** — `React.memo(ModelEventRow)` + `React.memo(OrchTurn)`
  with `arePropsEqual` comparing `turn.uuid + turn.rev`. Requires
  `eventsToTurns` to attach a stable `rev` (bump when the turn's
  events change). Defer virtualization (react-window) to a follow-up.
- **[B] P10** `orchestrator.py:record_turn` → `session.save()` full
  dump per orch turn (~2GB writes/100 turns, sync). Fix:
  `session.schedule_save()` sets `_save_pending`; a bg task flushes
  every 2s (and on `close()`). `save_session(branch=b)` writes only
  that branch + `history.json`.

## P2 — correctness

- **[C1] W-A** (A1 §A) — drop `version: msg.v` from reducer arms
  `queued`/`unqueued`/`rewrite_draft`/`error`/`notify`/`rewound`/
  standalone-`update`. They're direct-broadcast sidebands that "may
  legitimately race the drain queue" — writing `version` from them
  can rewind it and let a stale guarded frame past.
- **[C2] W-B** (A1 §B + A3 §1) — `useIsPending` gaps. Extend
  `FORK_KINDS` with `import`/`import_running`. Add per-action guards:
  `candidates`/`candidates_auditor` (n-picker chips),
  `pick_candidate` (both call sites), `fork_orchestrator`,
  `edit_target_message` (3 triggers), `interrupt_and_send`, orch
  `play`/`pause`, `rewind`, `restart_kernel`, `cancel_bg`,
  `detach_cell`, `unqueue`. Shared `<PendingButton cmd={pred}>`
  helper.
- **[C2] E25** — lift `.error-banner` from `DeskView` to `App.tsx`
  so it renders on StartView too.
- **[B] E1** — `_branch_meta()` includes `error: branch.error`;
  `wire.ts` `BranchMeta.error?: string`; sidebar `BranchNode` shows
  a red dot when set.
- **[B] E17** — `switch` to unknown branch → `{t:"error"}` +
  `broadcast_current()` (repoints client to real `session.current`).
- **[B] E14** — `handles.py:_PollingHandle._watch` wraps `_poll()`
  in `try/except Exception → self._error = str(e); self._done.set()`.
- **[B] E13** — `bash_tool.py:_bg()` wraps `_pump` in `try/except
  Exception → kernel.notify(f"bg job {name} crashed: {e}")`.
- **[C1] E19** — `reduceOne` `default:` → `console.warn("unknown t",
  msg.t); return {}`.
- **[C2] K1** — `ModelPicker` Esc listener → `stopPropagation()` so
  parent modal doesn't also close. `GateCard` `1-9` shortcut →
  `.focus()` the card on mount when it's the newest gate.

## P3 — cleanup / hygiene

- **[D] Dead** (A2) — delete `sessionsList`/`SessionSummary`/
  `branchConfig`/`BranchConfig` + Sidebar/DeskView readers;
  `buildEventTree`/`EventNode` (move test to inline); `disconnect`
  action; `Settings.default_*` (or wire them — decide: wire, since
  SettingsModal already round-trips them; StartView reads
  `settings.default_*` fallback to `presets.ts`); de-export the 11
  local-only symbols; dedupe `_walk`/`_wire_page_capture`/
  `_target_tool_call`/`_auditor_out` etc into `_smoke_fixtures.py`.
- **[C2] a11y** — add `aria-label` to the ~24 icon-only buttons
  (mirror `title`); convert `<a onClick>` without `href` to
  `<button className="link-btn">`; `SettingsModal`/`RawModal`/
  `CompareSheet` autofocus first input.
- **[E] Coverage** (A5) — regenerate `frontend-wb/fixtures/*.json`
  from `_smoke.py`/`_smoke_branch_deep.py` (post-A3 they'll include
  `branch_created`/`current`/`ack`/`orch`/etc). Add
  `_smoke_m1_coverage` cases for `restart_kernel`/`cancel_bg`/
  `approve_all`; `_smoke_rewrite` case for `rewrite_target_message`.

## P4 — deferred / follow-up

- **Per-role `eventsRev`** — `SwimlaneColumn` residual 1606 renders
  (chunk-independent) is `eventsRev` × ~9 structural events/turn.
  Target column recomputes on auditor Tool/Span events. Split
  `eventsRev` into `eventsRev[branch][role]` so each column keys on
  its own. Would cut ~1606 → ~400.
- `Settings.default_*` wiring — SettingsModal round-trips them but
  StartView reads hardcoded `presets.ts`. Either wire or delete.
- A6#1 `session.events` unbounded (~100MB) — cap or rotate. Needs
  design (rewound events? Cross-branch refs?).
- A6#23 virtualization (react-window) — after `React.memo` lands.
- A3 §4 shared `<PendingButton>` with spinner idiom — after W-B.
- A4#8/#18 `stop_sample`/`cancel_bg` no-op feedback — toast.
- Frontend `notifications` unbounded (A6#4) — cap at 200, drop head.

## Execution order (disjoint scopes)

1. **B** (backend) + **C1** (frontend store) — parallel, no overlap.
   B: `server.py`, `session.py`, `persist.py`, `m1/orchestrator.py`,
      `m1/handles.py`, `m1/bash_tool.py`, `wire.ts` (BranchMeta.error).
   C1: `store/session.ts`, `lib/events.ts` only.
2. **C2** (frontend components) — after C1 lands (needs `eventsRev`,
   `turnScores`). All `components/**/*.tsx` + `lib/selectors.ts` +
   `App.tsx`.
3. **D** (cleanup) — after C2. Touches many files but delete-only.
4. **E** (coverage) — after D. Test files + fixtures only.
5. Full verify + push after each.
