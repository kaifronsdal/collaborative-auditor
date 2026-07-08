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

## Adversarial-review caveats (2026-07-08)

Each refactor was stress-tested against reconnect / resume /
concurrent-candidates / orch-vs-M0 / pause-interrupt. Findings:

**A1 b-wide is flawed as specified.** `splice()` walks the
ancestor chain only; if child-L2's L1-root is a *sibling* of
parent-L2 under an L2 wrapper (the doc's stated nesting), the
parent's L1 content is never on the ancestor path → `splice()`
throws `"anchor not found"` at `core.ts:413`. Correct nesting
requires grafting under the *parent-L1-Trajectory span
containing the fork anchor* — an extra ~30-50 LOC search, not
"+10 `defaultKey`". Also: flat `by_role[(bid,"target")]` as span
content is wrong when L1 rollback exists (chronologically-flat
across lanes); needs a second span-walk. Real net ~−10 to +5.
**b-narrow's −40 is accurate; do that instead.**

**A2 — two deletion claims are wrong:**
- `reconcileQueued` **cannot** be deleted. Post-R4, `pre_turn`
  copies-not-clears; the frontend's only consumption signal is
  the id appearing in `ModelEvent.input`. Without mid-session
  `{t:"state"}` (A3), deleting `reconcileQueued` leaves ghost
  bubbles forever. Either keep it (~22 LOC) or add
  `{t:"queued_consumed", branch, ids}` from `post_generate()` —
  which is really an A3 typed-delta. **Hidden dep: A2 → A3-
  typed-deltas.**
- `_truncateByRole` → **selector, not deleted.** Otherwise
  clicking branch/resample shows nothing until `branch_created`
  lands (was: instant column truncation at anchor).
- `useIsPending` is **useless for `UNLOCKED` commands** — ack in
  ~1ms doesn't cover an 80ms double-click. But those are
  idempotent server-side (`gate.resolve` returns False on 2nd),
  so their guards can be *deleted entirely*.
- **Failure mode:** WS drops after `send()`, before `ack` →
  `pending[]` retains the entry → button disabled forever.
  `connect()` must reset `pending: []`.
- Real net ~−130.

**A3-typed-deltas — 4 undercount/hazards:**
- `_candidates` **spawns before broadcasting** (`server.py:486`
  vs `:491`). Under typed-deltas, replay events arrive before
  `span_role_delta` → `resolveRole` fails → events permanently
  unbucketed → candidate cards stay empty. **Must reorder to
  broadcast-before-spawn** (as `_register_and_spawn` already
  does), or `case "branch_created"` re-scans unbucketed events.
- **6 sites, not 5** — `dismiss_candidates` missing.
- `{t:"batch_resolved"}` must carry `ended: [child_ids]` (set by
  `_stop_running_branches`), else sidebar shows cancelled
  candidates as still running.
- `{t:"orch"}` needs `span_role_delta` too (registers
  `("orch","orch")`).
- Add `if (msg.v <= state.version) return {}` guard — pre-
  existing bug that A3 amplifies (each stale frame carries more
  mutations).

**A4 — two mis-partitions:**
- `generating` fold **loses the TTFB shimmer** — the gap between
  `pre_turn()` and first pending `ModelEvent` (0.2-3s on real
  models) is exactly what `showShimmer = isGenerating &&
  !lastIsPending` covers. Deriving `isGenerating` from
  `lastIsPending` reduces `showShimmer` to `false`. **Keep
  `generating` on `{t:"status"}`** (R3's wire-through), or emit a
  synthetic pending event before `model.generate()`.
- `notifications` **mis-partitioned** — `kernel.notifications` is
  drained every turn, so `{t:"orch"}` wholesale-replace wipes any
  `{t:"notify"}`-appended chip. Keep on the append-only
  `{t:"notify"}` path; drop from `{t:"orch"}`.
- `pendingGates` fold needs lifting to `selectors.ts` (+15-20
  LOC) for `useKeyboardShortcuts` to reuse.
- Real net backend ~−10.

## Ordering (revised)

1. **A3-batch** — sound, no caveats, ~50 LOC. Kills ghost-gap.
   Provides `reduceOne` extraction.
2. **A1-b-narrow** — sound, orthogonal, −40 real. Defer b-wide
   until the interleaved L1×L2 tree is designed properly.
3. **A3-typed-deltas** — with: reorder `_candidates` to
   broadcast-before-spawn; add `{t:"queued_consumed"}` from
   `post_generate()`; carry `ended:[…]` on `batch_resolved`;
   `span_role_delta` on `orch`; version guard in `apply()`.
4. **A2** — now `reconcileQueued` is deletable (via
   `queued_consumed`). `_truncateByRole` → selector over
   `pending`. `connect()` resets `pending: []`. Delete (don't
   replace) the `UNLOCKED`-command guards.
5. **A4-partial** — `{t:"orch"}` for `bg_jobs`/`bg_cells`/
   `model`/`span_id` (fixes `fbc8ab6` bug). Keep `generating` on
   `{t:"status"}`; keep `notifications` on `{t:"notify"}` only;
   lift `pendingGates` fold to `selectors.ts`.
6. **A1-b-wide** — only after designing the L1×L2 graft
   correctly (child-L2's L1-root under the parent-L1-span
   containing the fork anchor, not under the L2 wrapper).

R5 already landed → A2's fast-ack prereq is satisfied (the doc's
"A4 first" was wrong). Remaining slow-under-lock paths
(`_h_end`, `_h_import_running`) should move off the lock as part
of step 3.

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

## Estimated net after all six steps (revised)

Backend ~−60, frontend ~−130, wire +9 message types (`ack`,
`batch`, `branch_created`, `current`, `batch_resolved`, `orch`,
`queued_consumed`, plus keep existing). One `useIsPending` hook;
one `Orchestrator.dirty()`; b-narrow target timeline `by_role`-
sourced; `case "state"` connect-only. b-wide (both timelines
structurally identical) deferred.

## A1-b-wide design (event-sourced)

Session-wide target `Timeline`, structurally the same as the auditor
timeline so both columns take the identical `convertServerTimeline →
computeFlatSwimlaneRows → splice()` path and `_by_anchor` becomes
deletable. Purely event-sourced — zero `Tape` reads (so petri #111's
`Tape.prefix()` deletion is a non-issue for the builder).

### What `splice()` needs (the invariant)

`core.ts:395` `splice(root, target)` builds `chain =
ancestorChain(root, target)` by walking **`.branches` only**, then
for each `chain[i]` cuts its `content` at the `AnchorEvent` whose
`anchor_id === chain[i+1].branchedFrom` (throws at `:413` if absent;
`branchedFrom == null` → discard prefix so far). So the tree must
satisfy: **every non-root span's `branchedFrom` is a target anchor
that appears as an `AnchorEvent` in its `.branches`-parent's
`content`.** Nothing else matters — no L2 wrapper spans.

### Tree structure

One flat tree of L1-trajectory spans, session-wide. No L2 wrapper.

    target-root (synthetic wrapper, content=[], branchedFrom=null)
    ├── B0/L1-root                        branchedFrom=null   ← root branch
    │   ├── B0/L1-a  (rollback @t3)       branchedFrom=t3
    │   │   └── B2/L1-root (L2 fork @t5′) branchedFrom=t5′    ← grafted
    │   └── B1/L1-root (L2 fork @t2)      branchedFrom=t2     ← grafted
    │       └── B1/L1-a (rollback @t9)    branchedFrom=t9
    └── B3/L1-root                        branchedFrom=null   ← 2nd `start`

Each node is one `Trajectory` from some `branch.history`. An L2
fork's L1-root is a **`.branches` child of whichever L1 span,
anywhere in the session, generated the fork's last shared target
turn live** — not a child of an L2 wrapper.

### Graft algorithm (three passes over `by_role`)

    # 1. Enumerate every L1 trajectory span_id, session-wide.
    l1_of: dict[str, str] = {}                    # l1_span_id → branch_id
    for bid, b in session.branches.items():
        for t in _walk(b.history.root):
            l1_of[t.span_id] = bid

    # 2. Bucket target events by owning L1 span (b-narrow's owner()).
    def owner(sid):
        while sid is not None and sid not in l1_of:
            sid = session.span_parent.get(sid)
        return sid
    buckets: dict[str, list[str]] = {sid: [] for sid in l1_of}
    for bid in session.branches:
        for u in session.by_role.get((bid, "target"), []):
            if (d := session.events.get(u)) and (o := owner(d["span_id"])):
                buckets[o].append(u)

    # 3. Split each bucket at first ModelEvent: pre-live vs live.
    #    anchor_owner[X] = the one L1 span whose *live* content has
    #    ModelEvent with output.message.id == X (unique — replays/
    #    serves emit AnchorEvent only, never a fresh ModelEvent).
    pre, live, anchor_owner = {}, {}, {}
    for sid, uuids in buckets.items():
        i = next((i for i, u in enumerate(uuids)
                  if session.events[u]["event"] == "model"), len(uuids))
        pre[sid], live[sid] = uuids[:i], uuids[i:]
        for u in live[sid]:
            d = session.events[u]
            if d["event"] == "model" and (mid := _msg_id(d)):
                anchor_owner[mid] = sid

    # 4. For each L1 span with live content, derive branchedFrom +
    #    graft parent. branchedFrom = last AnchorEvent.anchor_id in
    #    pre[sid] (covers BOTH L1-replayed and L2-served prefix —
    #    same event shape). Graft at anchor_owner[branchedFrom].
    def to_span(sid):
        bf = next((session.events[u]["anchor_id"]
                   for u in reversed(pre[sid])
                   if session.events[u]["event"] == "anchor"), None)
        return {"id": sid, "branched_from": bf,
                "content": [{"type": "event", "event": u}
                            for u in live[sid]],
                "branches": []}, anchor_owner.get(bf)

    spans = {sid: to_span(sid) for sid in l1_of if live[sid]}
    root = {"id": "target-root", "content": [],
            "branched_from": None, "branches": []}
    for sid, (span, parent_sid) in spans.items():
        (spans[parent_sid][0] if parent_sid in spans
         else root)["branches"].append(span)

`_by_anchor` / `_index_anchor` deleted — `anchor_owner` is the same
map, computed inline (b-narrow's "second job", minus the
incremental maintenance).

### `branchedFrom` value

**Not** `Branch.branched_at`. That (`run.py:449`) is the last
anchored step in the L2 `_tape_prefix()` and may be a `GEN_SOURCE`
(auditor) or `Stage` anchor — neither appears in the target
column, so `splice()` would throw. The correct value is the last
**target** anchor before the span went live, derived from the
event bucket without touching the tape (step 4 above). Analogous
to `auditor_branched_from()` filtering to `GEN_SOURCE`.

### Edge cases

**Fork on a shared-prefix anchor** (parent has L1 lanes A, B via
rollback @t3; L2 fork at t2, shared): `anchor_owner[t2] = A` — only
A has the *ModelEvent* for t2 (B has replayed AnchorEvent, no
ModelEvent). Child grafts under A. Ownership by origin.

**L1 rollback inside the L2-served prefix** (parent rolled back;
child's L2 fork is past it → child's L2 replay re-executes the
rollback → child gets lanes A′, B′ where A′ never goes live).
Naive "graft `child.history.root`" fails: A′ has no content, B′'s
`branchedFrom` isn't in it. The algorithm sidesteps this by
**grafting each L1 span independently** at its own
`anchor_owner[branchedFrom]`: A′ (no live content) → dropped; B′
grafts directly under parent-B. Child's L1-internal edges are
discarded and re-derived from event ownership; result matches
`history.children` in the common case, correct in the hard case.

**Fork before any target turn**: `pre[sid]` has no `AnchorEvent` →
`branchedFrom = None` → grafts under wrapper; `splice()` discards
ancestors. Correct (no shared target prefix).

### `defaultKey` / `SwimlaneColumn`

Ship each branch's L1 span-id set on the wire
(`branches_meta[bid].l1_spans = [t.span_id for t in
_walk(b.history.root)]`):

    const l1Set = new Set(branches[branch]?.l1_spans ?? []);
    const defaultKey = useMemo(() => {
      if (rows.length === 0) return null;
      const mine = isAuditor
        ? rows.find(r => rowSpan(r).id === branch)
        : [...rows].reverse().find(r => l1Set.has(rowSpan(r).id));
      return mine?.key ?? rows[rows.length - 1].key;
    }, [rows, isAuditor, branch, l1Set]);

`selectLane` for target mirrors auditor: if the picked span's id is
not in `l1Set`, resolve its owning branch (reverse `l1_spans` map)
and `switchBranch(owner)` — otherwise clicking a foreign lane shows
its content but leaves `current`/queued/status pointing at the
wrong branch.

### Petri #111 interaction

`_group_events_by_trajectory` is the per-History version of step 2;
b-wide's is the same walk over `session.span_parent`, `l1_of`
widened to all branches. `_drop_replay_prefix` (cut at `BranchEvent`)
covers L1-replay boundary but **not** L2-serve boundary (child's L1
root has `prefix_len == 0` → no `BranchEvent`); first-`ModelEvent`
(step 3) covers both uniformly. `session.span_parent` nests each
branch's L1 tree under its own `target_span_id`, but those are
top-level siblings — runtime span tree does **not** encode L2
nesting; graft edges must be computed. `anchor_owner` makes that
~8 LOC, not the 30-50 the adversarial review estimated.

### Revised cost

| | LOC |
|---|---|
| `timeline.py` `build_target_timeline` → session-wide | −20 (80→~60) |
| `session.py` delete `_by_anchor` + `_index_anchor` | −18 |
| `session.py` `_on_event`/`view()` collapse | −8 |
| `session.py` `view()`+`branch_created`: `l1_spans` | +4 |
| **backend** | **−42** |
| `SwimlaneColumn.tsx` `defaultKey`+`selectLane` cross-branch | +14 |
| `session.ts` `l1_spans` on meta; single target timeline slot | +6 |
| **frontend** | **+20** |
| **net** | **~−22** |

`anchor_owner` (one pass over target `ModelEvent`s) replaces the
per-child `parent.history` search; event-sourced `branchedFrom`
replaces `_tape_prefix()` reads. **The "defer b-wide"
recommendation no longer holds** — b-wide is now cost-neutral with
b-narrow *and* deletes `_by_anchor`.

### Rejected: per-branch both columns

Make auditor per-branch too. Same code path, no graft. Loses: the
auditor swimlane as branch-nav (its primary purpose), `computeForks`
sibling arrows, the gantt tree. Not worth it.
