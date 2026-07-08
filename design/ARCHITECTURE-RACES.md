# Structural fixes for the race/responsiveness bug classes

Companion to `RACE-FIXES.md`. That doc patches each symptom;
this one identifies the four architectural changes that make the
classes impossible by construction. Consolidated from 4
read-only review agents (2026-07-08).

## The four changes

### A1 — session-wide target timeline, `by_role`-sourced (b-wide)

**Class eliminated:** target-column lag / one-turn-late / R2's
`_by_anchor` band-aid.

**Root cause:** `_target_timeline` reads `tape.log` for content,
which `Tape.replayable` appends *after* emitting the event that
triggers `_on_event`'s rebuild. `build_auditor_timeline` reads
`by_role` (populated on emit) and never had this bug.

**Change:** port `_target_timeline` to the `build_auditor_timeline`
pattern in `timeline.py` — walk `History` for tree *shape* only
(set-once metadata, never lags), populate `content` from
`by_role[(bid,"target")]` grouped by L1-trajectory span via
`session.span_parent`. Make it **session-wide** (nest each
branch's L1 tree under the L2 `audit_history` tree; child L2
branch's L1-root gets `branched_from = last target anchor in
audit_tape.prefix()`); frontend `splice()` reconstructs shared
prefix from the parent — same as auditor already does.

**Deletes:** `_by_anchor` + `_index_anchor`, R2's tail-append,
`init_transcript()` contextvar footgun in `view()`, petri
`build_history_timeline` import + `_anchor_lookup` full-transcript
scan. `_on_event`'s timeline block collapses (both roles key to
`self.current`).

**Cost:** `session.py`/`timeline.py` −55 LOC; `SwimlaneColumn.tsx`
`defaultKey` +10 LOC. Wire semantics change (target timeline
session-wide, keyed like auditor).

**Fallback (b-narrow):** keep `_by_anchor` as an honest
cross-L2-branch anchor resolver (its second job, distinct from the
lag band-aid); still source content from `by_role`. −40 server,
0 frontend/wire change. Kills the lag class; leaves the fold into
b-wide as a follow-up.

### A2 — `pendingCmds` overlay + `{t:"ack", req_id}`

**Class eliminated:** every double-click / re-entry corruption
(R1's 9 per-button guards + `PENDING_BRANCH`/`prevCurrent`/
`reconcileStatus`/`reconcileQueued`/`STATUS_RANK`/rollback ~200 LOC).

**Root cause:** actions optimistically *mutate* server-owned
state (`current`/`status`/`queued`/`branches`); a second click or
a concurrent `{t:"state"}` clobbers the mutation before the
server's echo reconciles it.

**Change:** actions never mutate server state. `send(msg)`
appends `{...msg, req_id: uuid()}` to `store.pending: Up[]`;
render state = `f(serverState, pending)` in selectors
(`queuedView = server.queued ∪ pending inject-cmds`; `currentView
= pending-fork ? {kind:"pending"} : server.current`). Server:
`_dispatch` emits `{t:"ack", req_id}` in `finally` (~4 lines);
reducer `case "ack"` filters `pending`. One exported hook:
`useIsPending(pred: (c: Up) => boolean)` — every button
`disabled={useIsPending(c => …)}` at whatever granularity it
needs (per-`display_id` for approve, `FORK_KINDS.has(c.t)` for
branch/resample family).

**Deletes:** `PENDING_BRANCH`, `PENDING_ID`, `prevCurrent`,
`reconcileStatus`, `STATUS_RANK`, `reconcileQueued`,
`_truncateByRole`, `case "error"` rollback, `case "queued"` dedup,
all component-local `useState` guards (`isStarting`, `sent`,
`sentIds`, `justSent`, `_pendingChild` sentinel check).

**Cost:** `session.ts` net −180 LOC; `server.py` +4; `wire.ts` +2;
6 component files swap `useState` guards for `useIsPending`.

**Prerequisite:** A4 (or R5) — `_dispatch_lock` must release
before slow waits so acks return <50ms; otherwise unrelated
buttons stall behind a slow fork.

**Why not the alternatives:** `req_id` server dedup solves
*retransmission*, not double-click (two clicks = two fresh ids).
`inFlight: Map<t, N>` has an unfixable granularity problem
(`approve` needs per-id; fork-family needs cross-`t`).

### A3 — `{t:"batch", ops:[…]}` + typed mid-session deltas

**Class eliminated:** ghost-gap (event+timeline in separate
frames → React paints between them) + `{t:"state"}` clobber of
optimistic entries.

**Root cause (two independent halves):**
- **Frame atomicity:** `_on_event` enqueues `{t:"pool"}`,
  `{t:"event"}`, `{t:"timeline"}` as 3 frames → 3 `set()`.
  `reconcileQueued` fires on `event`; the row renders on
  `timeline`. One-frame gap where the ghost is gone and the row
  isn't there.
- **Mid-session `{t:"state"}` clobber:** 5 broadcast sites
  (`_register_and_spawn`, `_candidates`, `pick_candidate`,
  `switch`, `start_orchestrator`) ship a full `view()` — including
  `queued`/`events`/`pool` *unchanged* — and `case "state"` does
  wholesale replace. That unchanged re-ship is what stomps
  optimistic writes.

**Change (two parts):**
- **Batch:** `_on_event` accumulates `ops: list[dict]`, enqueues
  one `{t:"batch", v, ops}`; `session.ts` adds `case "batch":
  ops.reduce(reduceOne, state)` where `reduceOne` is the existing
  per-case body extracted. ~30 backend + ~20 frontend.
- **Typed deltas:** replace the 5 mid-session `broadcast(
  {t:"state", …view()})` with `{t:"branch_created", id, meta,
  span_role_delta, current}` / `{t:"current", branch}` /
  `{t:"batch_resolved", batch, picked}` / `{t:"orch", state}`.
  Keep `push_full_state` connect-only. `case "state"` then only
  runs on connect (no optimistic state to reconcile).

**Deletes:** the 4 ad-hoc `case "state"` guards
(`pendingNewAudit`, `reconcileStatus`, `PENDING_ID` re-key,
`prevCurrent` rollback) — same machinery A2 deletes, from the
other direction.

**Cost:** ~50 LOC batch; ~5 new `Down` variants + reducer cases
for typed deltas.

**Composes with A2:** both delete the reconciliation machinery.
A2 alone still needs `{t:"state"}` to not clobber `pending[]`
(trivially — `pending` is client-owned); A3 alone still needs
per-button guards. Together: `case "state"` runs once (connect),
buttons read `useIsPending`, no reconciliation anywhere.

### A4 — partition derived state; `{t:"orch"}` for process-only

**Class eliminated:** stale `bg_jobs`/`pending_gates`/`generating`
+ every "add another `_broadcast_status_soon()` call" fix.

**Root cause:** UI-visible fields split arbitrarily between
`{t:"state"}` (`bg_jobs`, `orchestrator`), `{t:"status"}`
(`generating` — R3 just added it), and frontend event-stream
folds (`pending_gates` in `OrchColumn`, `lastIsPending` in
`Column`). Each new field either needs a broadcast call at every
mutation site (forgettable — see below) or a frontend fold.

**Live bug found:** `bash_tool.py:319` and `orchestrator.
cancel_bg` call `_broadcast_status_soon()` "so JobsPanel picks up
the proc" — but `broadcast_status()` sends `{t:"status"}`, which
doesn't carry `bg_jobs`. The fix at `fbc8ab6` doesn't work.

**Change (partition by data origin):**
- **Transcript-derivable → frontend fold, drop from wire:**
  `generating` (last event on role X is `pending=True`
  ModelEvent — `Column.tsx:79` already does this; R3's
  wire-through is correct but unnecessary), `pending_gates`
  (fold gate cards' `pending` flag — `OrchColumn:91` already does
  this; `useKeyboardShortcuts:30` is the last stale reader),
  `context_chars` (from `last.model.input`), the `"waiting"`
  overlay on `orch.status` (`= running && pendingGates.length >
  0`). Delete from `view()`/`broadcast_status()`; drop
  `Gate.on_change → _broadcast_status_soon`.
- **Process-only → `{t:"orch", orch: orchestrator.view()}`:**
  `bg_jobs` (pids, `run_log_dirs` — not in transcript, meaningless
  on resume), `bg_cells`, `notifications`, `model`, `span_id`.
  One `Orchestrator.dirty()` that enqueues it; call at the ~4
  mutation sites (`_bash_procs` add/pop, `kernel.bg` add/discard,
  `run_log_dirs.append`). A few hundred bytes per push.

**Cost:** −50 LOC backend (`generating`/`pending_gates`/
`context_chars` deleted from `view`/`broadcast_status`/`Branch`);
+1 `Down` variant + reducer case; `bash_tool`/`cancel_bg`
`_broadcast_status_soon()` → `orch.dirty()`.

**Rejected:** reactive `@property.setter` — every field that
actually causes staleness is a *container* mutated in place
(`_bash_procs: dict`, `gate.pending: set`, `state.messages:
list`); setters don't fire on `.append()`.

## Ordering

**A4** (or its subset R5) first — A2's `ack` needs fast dispatch.
Then **A3-batch** (small, kills ghost-gap immediately). Then
**A2** (deletes the reconciliation machinery). Then **A1**
(orthogonal — rendering source). **A3-typed-deltas** and
**A4-frontend-fold** can land any time after A3-batch.

## What of R1-R5 becomes obsolete

| tactical | subsumed by |
|---|---|
| R1 all 9 guards + `_pendingChild` sentinel | A2 (`useIsPending`) |
| R2 `_by_anchor` tail-append | A1 (`by_role`-sourced content) |
| R3 `generating` on wire | A4 (frontend fold — R3's version works, this simplifies) |
| R3 stuck-bubble retract, `Step(None)` pop | **kept** — pause-interrupt lifecycle, not a race |
| R4 `pre_turn` no-clear | A3-typed-deltas (no mid-session `{t:"state"}` to clobber) — but keep as defence-in-depth |
| R4 `reconcileQueued` on `update` | A2 (no `reconcileQueued` at all) |
| R5 release lock before replay-wait | **kept** — prerequisite for A2 |
| R5 `switch` pauses outgoing | **kept** — semantic fix, not a race |

R1-R5 land now (chaos test passes); A1-A4 are the follow-up
refactors that let us *delete* R1/R2/R3-generating/R4-reconcile.

## Estimated net after all four

Backend −150, frontend −170, wire +8 message types (`ack`,
`batch`, `branch_created`, `current`, `batch_resolved`, `orch`,
plus keep existing). One `useIsPending` hook; one
`Orchestrator.dirty()`; both timelines identical; `case "state"`
connect-only.
