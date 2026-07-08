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

## A1-b-wide design

Session-wide target `Timeline`, structurally the same as the auditor
timeline so both columns take the identical `convertServerTimeline →
computeFlatSwimlaneRows → splice()` path and `_by_anchor` becomes
deletable. Designed against b-narrow as the base (already landed).

### What `splice()` needs

`splice(root, leaf)` (`core.ts:395`) computes `ancestorChain(root,
leaf)` by walking `.branches` only, then for each node cuts its
`.content` at the *next* node's `branchedFrom` (`findIndex` on
`event === "anchor" && anchor_id === branchedFrom`; inclusive slice).
`branchedFrom == null` discards everything above. So the tree must
satisfy, for every parent→child edge:

  child.branchedFrom is null, OR appears exactly once as an
  AnchorEvent in parent.content.

That is the only invariant the builder has to guarantee.

### Tree structure

One session-wide tree. The wrapper root is the same synthetic empty
span the auditor tree uses. Under it, one span per **live-recorded
L1 trajectory** — i.e. the (L2 branch, L1 trajectory) pair at which a
target step actually hit the provider (neither L1- nor L2-served).
Every other L1 trajectory in every branch's `history` is a replay
copy of one of those and is *not* emitted.

    target-root  (wrapper, content=[], bf=null)
    └─ A.α       ← L2-root branch A's L1 root      (bf=null)
       ├─ A.β    ← A did an L1 rollback at r        (bf=r)
       │  └─ B*  ← L2 child B forked on A.β         (bf=t_B)
       │     └─ B.γ  ← B did its own L1 rollback    (bf=r')
       └─ C*     ← L2 child C forked on A.α, pre-r  (bf=t_C)

`B*`/`C*` are each branch's **boundary trajectory** — the L1 node
that was current when that branch's L2 replay ended. Its
entirely-L2-served L1 ancestors/siblings (B.α, B.β-mirror, …) are
dropped; its post-live L1 descendants (B.γ) are emitted unchanged.

### Graft algorithm

Walk `session.audit_history` in L2 pre-order (parents before
children). Maintain `anchor_owner: dict[anchor_id → span]` populated
as spans are emitted.

    def target_branched_from(b: Branch) -> str | None:
        # Last *target-side* anchor in the L2 shared prefix — the mirror
        # of auditor_branched_from(). GEN_SOURCE steps anchor on auditor
        # message ids, which never appear in target-column content, so
        # splice() couldn't cut on them.
        return next(
            (s.anchor_id for s in reversed(_tape_prefix(b.audit_tape))
             if s.anchor_id and s.source != GEN_SOURCE),
            None,
        )

    def l2_served(b: Branch) -> set[str]:
        return {s.anchor_id for s in _tape_prefix(b.audit_tape) if s.anchor_id}

    def boundary_traj(b: Branch, served: set[str]) -> Trajectory:
        # The L1 trajectory current at L2-go-live: descend from the L1
        # root through children whose *rollback anchor* was L2-served
        # (⇒ the rollback tool-call itself was replayed ⇒ the child was
        # created during L2 replay). `children` is creation-ordered, so
        # the last match is the current lineage.
        t = b.history.root
        while (cs := [c for c in t.children if c.branched_from in served]):
            t = cs[-1]
        return t

**Graft point** for branch `b`: `anchor_owner[target_branched_from(b)]`
(or the wrapper root when `None`). Append `to_span(b, boundary_traj(b))`
to that span's `branches`; recurse into `b`'s L2 children.

**Uniqueness.** Each target anchor is a `ChatMessage.id` /
`Stage.anchor_id` minted exactly once — at the (L2, L1) pair where
the call ran live. Every L2 descendant that L2-serves it drops it
from content (∈ `served`); every L1 descendant that L1-serves it
never emits an `AnchorEvent` for it (`Tape.replayable` serve path
emits nothing). So `anchor_owner` has exactly one entry per anchor —
no search over `parent.history` is needed, and the multi-hop case
(C's `t_C` was live-recorded by grand-parent A, not immediate parent
B) falls out for free.

### Span content

`buckets_for(branch)` is b-narrow's grouping unchanged
(`timeline.py`): walk `by_role[(bid,"target")]`, resolve each
event's `span_id` up `session.span_parent` to the nearest L1
`Trajectory.span_id`.

`to_span(b, t, is_boundary)`:
- **Boundary traj** (`C*`): drop the *prefix* of `buckets[t.span_id]`
  up to and including the last `AnchorEvent` with
  `anchor_id ∈ l2_served(b)`. Prefix-cut, not set-filter — L2-served
  turns emit `ToolEvent`s live (`execute_tools` runs on the served
  output) which would otherwise double with the parent's copy that
  `splice()` prepends. `AnchorEvent` is the last emit of a turn, so
  cutting there is turn-aligned.
- **Non-boundary L1 descendants**: b-narrow's per-traj rule unchanged.
- **`C*`'s L1 children with `branched_from ∈ served`** are skipped
  (entirely-L2-served — dead lanes the parent already covers).
- Every emitted `AnchorEvent` registers `anchor_owner[aid] = span`.
- Every emitted span carries `description = b.branch_id` (the L2
  owner tag — see `defaultKey` below).
- `C*.branched_from = target_branched_from(b)`.

The `_by_anchor` cross-borrow is gone: `splice()` supplies the
parent's `ModelEvent`s directly (they precede the cut anchor in the
parent's bucket, since `ModelEvent` is emitted before `AnchorEvent`).

### L1-rollback / shared-prefix interaction

**Fork on a lane-specific anchor**: `anchor_owner[t_B] = A.β`. Graft
under A.β. `splice` from B.γ walks `B.γ → B* → A.β → A.α → root`;
each cut hits.

**Fork on a shared-prefix anchor** (`t_C` in A.α, before A's rollback
point `r`): only A.α emits its `AnchorEvent` (L1 serve emits nothing),
so `anchor_owner[t_C] = A.α`. Graft under A.α. No ambiguity.

**Rollback-between-anchor-and-fork** (rare — auditor turn that calls
`rollback` and nothing else): `boundary_traj` descends past
`target_branched_from(b)`'s traj into the post-rollback child;
`splice()` would fail. **Guard (~6 LOC):** when `boundary_traj(b) is
not history.root` and `target_branched_from(b)` is not in the
boundary traj's fresh log, graft under
`anchor_owner[boundary_traj.branched_from]` and set
`C*.branched_from = boundary_traj.branched_from` instead.

### `defaultKey` / `SwimlaneColumn`

Both timelines are now session-wide. Auditor spans have
`id === branch_id`; target spans have `id === l1_traj.span_id` and
carry the L2 owner in `description`.

    const defaultKey = useMemo(() => {
      if (rows.length === 0) return null;
      const mine = rows.filter((r) => {
        const s = rowSpan(r);
        return s.id === branch || s.description === branch;
      });
      return (mine.at(-1) ?? rows.at(-1))!.key;
    }, [rows, branch]);

The `isAuditor` special-casing in `selectLane` collapses: both
columns dispatch `switchBranch` when the picked lane's L2 owner ≠
`branch`, else `setSelectedKey` locally.

**Empty-graft window** (child spawned, no live target step yet):
`convertServerSpan` filters empty branches, so `C*`'s row is absent
and `mine` is empty. Fall through to the graft-parent's row via
`branches_meta[branch].parent` recursion (~5 LOC) — better than
b-narrow's per-anchor `_by_anchor` fill.

### Cost (from b-narrow)

| file | Δ |
|---|---|
| `timeline.py` — `build_target_timeline` → session-wide | ~+10 |
| `session.py` — `_by_anchor` + `_index_anchor` + key branching | ~−28 |
| `SwimlaneColumn.tsx` — unified `defaultKey`/`selectLane` | ~+7 |
| `session.ts` reducer — target keyed as auditor | ~+3 |
| `selectors.ts` — `useSwimlanes` reads shared target tree | ~+2 |

Net **~−6** (backend −18, frontend +12), ±10 for the guards.

**Note:** uses `_tape_prefix(tape)` — the inlined
`(list(tape.log)+list(tape.pending))[:tape.prefix_len]` since petri
#111 deletes `Tape.prefix()`. The petri rebase adds this helper.

### Simpler alternative considered (rejected)

Make *both* timelines per-branch and reconstruct the L2 prefix on
the frontend by walking `branches_meta[*].parent`. Reintroduces the
borrow problem frontend-side (~+30) and kills the L2-branch swimlane
gantt in *both* columns — a real UX loss the sidebar doesn't fully
replace. Not simpler in LOC and strictly worse in capability.
