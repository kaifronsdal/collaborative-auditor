# Race-condition + UI-responsiveness fixes

Consolidated from 5 investigation agents (2026-07-08): WS-handler
races, button-debounce audit, target-streaming trace, queued→
generate visual-gap trace, slow-model chaos playwright test.

Chaos test (`_e2e_m1_chaos.py`): 5/6 PASS, 0 exceptions/console
errors, but **s1 (double-pause) FAILs** and **s4** found child
target-column empty after fork.

## Batch R1 — frontend re-entry / debounce (3 changes, 12 hazards)

Frontend-only. Highest priority — kills the "corrupted state" root.

- **`session.ts` `_pendingChild`** (line ~1135): early-return if
  `current === PENDING_BRANCH || current === PENDING_ID`. Also:
  `branchAt`/`resampleAt`/`editAuditorCall`/`editTargetMessage`
  capture the *real* branch id at click time and send it as
  `branch:` (not omitted → server `session.current`). Covers
  button-audit #1-4,9; WS-race #3,4,11,12.
- **`GateCard.tsx` + `OrchColumn` head-gate**: local `sent: Set<
  string>` — `resolve()` adds the id then sends; buttons `disabled
  = sent.has(id)`. `Approve all` iterates then disables. Covers
  button-audit #12-15.
- **Per-button in-flight flags** (StartView's `isStarting`
  pattern): `ScannerPicker` Run, `DeskView` composer primary
  (250ms `justSent` debounce so double-click Send doesn't morph→
  Play), `CandidateCell` dismiss. Covers button-audit #5,6,11,16.
- **`transport("pause")` optimistic flip** — remove it (or
  debounce the primary button 200ms). Chaos s1 shows the flip
  makes double-click send `pause`→`play`. `Branch.pause()` is
  now synchronous-ish (cancels scope), so backend `{t:"status"}`
  arrives fast enough; let it drive the button.
- **`inject()` guard**: return early if `current` is a sentinel
  (WS-race #12 — message silently lost).

## Batch R2 — target streaming + timeline correctness

Backend `session.py` + `timeline.py`. Fixes "target column takes
a while to update" + chaos s4.

- **`_target_timeline`** (session.py:~312): after
  `build_history_timeline`, append any in-flight target
  `ModelEvent` uuid from `by_role[(bid,"target")]` onto the tip
  span's `content`. See target-streaming agent's exact patch.
  Fixes streaming AND the one-turn-late completion.
- **`{t:"timeline"}` keyed to viewed branch** (session.py:~296,
  gap #10): auditor timeline is session-wide but broadcast under
  the emitting branch's key → viewed branch's timeline goes
  stale when candidates run in background. Broadcast auditor
  timeline under `self.current` (or store session-scoped).
- **Chaos s4 — child target column empty after fork**: verify
  the above `_target_timeline` fix also covers this (replayed
  prefix events should be in `by_role` for the child). If not,
  ensure `_register_and_spawn`'s post-replay `broadcast` ships a
  `{t:"timeline", role:"target"}` for the child.

## Batch R3 — pause/generate lifecycle

Backend `auditor.py`/`run.py`/`session.py` + frontend shimmer.

- **Stuck pending bubble after pause-interrupt** (WS-race #15,
  `c2004c4` regression): on `scope.cancel_called` in
  `auditor.py`, before `continue`, mark the pending `ModelEvent`
  rewound — `session.mark_rewound(branch.auditor_span_id,
  <pending_uuid>)`. Need the uuid: capture it via a pre-generate
  hook, or scan `session.events` for the last `pending=True`
  event on the auditor span. Simplest: `Branch` records
  `_pending_gen_uuid` in `_on_event` when a `pending=True`
  ModelEvent lands on its auditor span; `pause()` marks it
  rewound after cancel.
- **`branch.generating` on wire** (gap #3,4): include
  `"generating": branch.generating` in `broadcast_status()` +
  `view()`; add to `wire.ts` `Down.status` + `BranchMeta`.
  `SwimlaneColumn`/`Column` gate `showShimmer` on `generating
  === role` instead of `status === "running"`. Kills the false
  auditor-shimmer during target-generate.
- **Pause-with-queued visual feedback** (WS-race #6): when
  `Branch.pause()` auto-plays because queued is non-empty, emit
  a `{t:"notify", text:"interrupted — sending queued input"}` so
  the user sees why status bounced back to running.

## Batch R4 — queued lifecycle

Backend `run.py` + frontend `session.ts`.

- **Gap #6 (the "disappears for a while")**: `pre_turn()` reads
  `msgs = list(self.queued["auditor"])` but does NOT clear it
  there. Clear in `post_generate()` instead (after the pending
  `ModelEvent` has shipped, so `reconcileQueued` on the frontend
  is the actual removal path). Any interleaved `{t:"state"}`
  still ships the queued msgs → ghost persists until the lead
  bubble renders.
- **`reconcileQueued` on `{t:"update"}` too** (gap #2) — one-
  line addition to `session.ts` case `"update"`.
- **`sendFeedback` auto-steps** (gap #9): `DeskView.sendFeedback`
  → if `status !== "running"`, also `transport("step")`.
- **Drop `queued["target"]`** (gap #7): remove from
  `Branch.__init__`; `_h_inject` rejects `role=="target"` with a
  clear error. (Or wire it into `run_turn_tools` — but there's
  no UI path that injects to target, so drop it.)

## Batch R5 — dispatch / lock

Backend `server.py`.

- **`_register_and_spawn` releases lock before
  `_replayed.wait()`**: move the 5s wait + `autoplay` +
  `broadcast_status` into a spawned task, so the dispatch lock
  is released immediately after `_spawn(session, branch)`. Pause
  during a slow fork then works instantly.
- **`switch` pauses the outgoing branch** (WS-race #9): before
  repointing `current`, `session.branches[old_current].pause()`
  so nothing is left autoplaying-and-unreachable.

## Regression harness

- `_e2e_m1_chaos.py` s1 should flip to PASS after R1's
  transport-debounce fix.
- Add s7: pause mid-generate → assert no `pending=True`
  ModelEvent lingers in `byRole` (R3 stuck-bubble).
- Add s8: assert child target column has ≥1 row within 2s of
  fork (R2/s4).
- `_smoke.py` target-streaming assertion: after a target
  generate starts, the shipped `{t:"timeline", role:"target"}`
  references the pending uuid (R2).

## Order

R1 (frontend) + R2 (session.py) in parallel — disjoint. Then
R3 (auditor/run + shimmer). Then R4 (queued). Then R5 (server
dispatch). Re-run `_e2e_m1_chaos.py` + `_e2e_m1_ui_fixes.py`
after each.

Total: ~15 root-cause fixes across 5 batches.
