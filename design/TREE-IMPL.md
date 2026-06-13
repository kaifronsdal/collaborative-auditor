# Tree mechanics: what exists, what's hard, what petri 3.0 carries

*Implementation recon for the hardest part of the build: auditor rollbacks nesting target
rollbacks, plus the resampling/editing machinery — a better version of what collaborative-auditor
already does. Companion to [DESIGN.md](DESIGN.md) §3 and §7 (M0). File refs checked 2026-06-13.*

---

## 1. Collaborative-auditor as-built

A git-like event-sourced DAG (`src/collaborative_auditor/models.py:66-129`):

- `EventNode` — append-only events (`SYSTEM_INIT`, `AUDITOR_TURN_START`, `TOOL_CALL_ADDED`,
  `TOOL_CALL_EXECUTED`, `RESEARCHER_MESSAGE`) carrying **two patch streams**: `auditor_patches`
  and `target_patches` (JSON Patch ops on the respective message lists).
- `Branch` — a tip pointer plus **materialized** state: `auditor_messages` and `target_state`,
  deep-copied at fork time.
- `Session` — the shared event pool + the branch list.

Branching reconstructs state at the fork event by walking root→event and replaying patches
(`models.py:258-279`), then deep-copies. Resample-target forks at the parent of the target
response and re-runs the model on the same target state (`handlers/branching.py:134-217`,
`handlers/playback.py:116-157`); edits fork at the parent and append the edited message
(`handlers/editing.py:116-143`). Nothing cascades: branches are independent, originals live on.

**The pain points** (confirmed by `agent_notes/architecture_refactor_plan.md`):

1. **Dual state, loosely coupled.** `auditor_messages` and `target_state` are synchronized only
   by convention — every handler that touches one must remember to patch the other. The refactor
   plan's own warning: tool-call edits must "ensure consistent `target_state`". This is the bug
   farm.
2. **No real auditor↔target nesting.** Rolling back the auditor conversation gets you a deep
   copy of the target state *as of that event* — correct only because every target mutation is
   itself an event in the same stream. There is no first-class statement of the invariant.
3. **Reconstruction cost.** `build_view_state()` is O(branches × messages); patches replay on
   every branch-point detection; no replay boundary markers.
4. **Granularity confusion.** Branch indicators exist at both turn and tool-call level, but tool
   call edits are really turn-level replays.

## 2. The nesting problem, stated precisely

The auditor's conversation *contains* target interactions: some auditor tool calls
(`send_message`, `query_target`, rollbacks) mutate the target context. So:

> **Invariant:** at any node of the auditor tree,
> `target_state == replay(target-mutating effects of the auditor path from root to here)`.

Branch/edit/resample the auditor anywhere, and the target state on the new branch must equal the
replay of the *edited* path — while sibling branches keep theirs. Collaborative-auditor enforces
this implicitly via dual patch streams + deep copies; the successor should enforce it
structurally: **the target state is derived, never independently mutated.**

## 3. What petri 3.0 (petri-meridian 3.0.11) provides

- **`Trajectory` + `replayable()`** (`src/inspect_petri/target/_history.py:43-187`) — recorded
  conversation spans forming a tree; a child trajectory replays ancestor steps to the branch
  point, then records live. `BranchEvent` marks the replay→live boundary; `AnchorEvent` tags
  results with rollback anchors; deterministic trailing steps are detected and included in
  replay. This is exactly the branch-by-replay engine §1's pain points ask for.
- **`Channel` + `Controller`** (`target/_channel.py:17-92`, `target/_controller.py:53-149`) —
  typed staging primitives (`Slot` constants: `SYSTEM`, `USER`, `TOOL_RESULT`, `PREFILL`),
  commands carrying anchor ids, and an `AnchorMap` issuing short ids (M1, M2…). Target mutation
  becomes *staging through a controller*, not in-place list surgery.
- **Native rollback tools** (`tools/_conversation.py:6-150`) — `rollback_conversation(message_id)`
  and `restart_conversation(keep_tools=True)` already implement "target experiences amnesia;
  auditor retains memory" atop `controller().rollback(anchor_id)` — atomic, anchor-addressed.
- **Tree-aware timeline rendering** (`_auditor/_target_timeline.py:22-102`) — builds an inspect
  `Timeline` from the trajectory tree, drops replay prefixes, handles children natively.

petri-dish layers this onto live agent scaffolds (ACP targets, `REWRITE` slot, tool
interception) — relevant later for agentic-harness targets, not for the core tree.

**What petri does *not* have:** the dual-trajectory problem. Petri assumes one linear auditor
making tool calls; the target rollback is a tool call, not a separate agent's tree. Auditor-side
branching/editing — the whole desk — is ours to build.

## 4. Build assessment

Plausible and worth it. The shape:

1. **Auditor turns as `replayable()` steps.** The auditor tree becomes a petri `Trajectory`
   tree: fork = child trajectory; edit = replay with argument overrides; resample = replay up to
   the turn, then live. This replaces collaborative-auditor's hand-rolled event DAG + patch
   replay.
2. **Target state derived through `Controller`.** Auditor tools stage via the controller; a
   replayed auditor turn re-issues the same stage commands, so the §2 invariant holds by
   construction instead of by handler discipline. Rollback anchors come free.
3. **The `Run` (DESIGN §3.2) binds the pair.** One abstraction owning (auditor trajectory,
   target trajectory), enforcing the invariant, exposing pen/pause-at-turn-boundary. Pin = a
   lease on one (auditor, target) pair; this is new code, on petri rails.
4. **Reuse, don't rebuild:** anchor/short-id assignment, replay queue management, rollback
   atomicity, timeline rendering substrate.

Still from scratch: the Run/pen/lease layer, turn-level grouping of multi-tool auditor turns
under replay, the consistency validator, and the view-state projection (branch points, `‹ 1/2 ›`
indicators, candidates picker) over dual trajectories rather than one timeline.

**M0 consequence:** the riskiest slice to spike first is a `Run` wrapping petri trajectories
with one edit-with-replay round trip — auditor edit at turn k → target state provably equal to
the replayed path — rendered as a two-branch tree in any UI at all.
