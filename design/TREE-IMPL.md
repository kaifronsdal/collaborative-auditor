# Tree mechanics: what exists, what's hard, what petri 3.0 carries

> **Status: superseded by petri PR #110 + [resampling.md](resampling.md).** This was the
> early recon; the answer it was reaching for landed as `Tape`/`Step`/`Node` in
> `petri-meridian/src/inspect_petri/target/_history.py` @ `4f4aa50`. §1 (collaborative-auditor
> as-built) and §2 (the invariant) remain accurate and useful background. §3 describes
> pre-PR-#110 petri; §4 is collapsed to a pointer.

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

## 3. What petri provided pre-PR-#110 (petri-meridian 3.0.11)

*Historical — `Trajectory`/`_Step` below became `Node`/`Tape`/`Step` (public, serializable)
in PR #110. See [resampling.md](resampling.md) §Implementation for what's there now.*

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

**This became `Tape`/`Step`/`Node` — see [resampling.md](resampling.md) and DESIGN.md §3.1.**
PR #110 made petri's own steps public, frozen, and serializable (`Step.dump()/load()`), so the
"durable `Effect` log + cold-replay adapter" this section anticipated collapsed into petri's
one record/replay mechanism: `Tape(pending=seed)` is the cold-replay path, `Tape.replayable` /
`Tape.compose` are the public API, `Node` is `Tape` + tree pointers, and the §2 invariant is
the tape's correctness property (proven by the PR's e2e record→resample test). What remains
workbench-side: the `Run`/pen/lease layer (DESIGN §3.2/§3.3) and the view-state projection
(branch points, `‹ 1/2 ›` indicators, candidates picker) over the two `Node` trees.
