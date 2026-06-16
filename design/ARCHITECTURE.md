# Implementation architecture — store, petri seam, fork mechanics

> **Status: superseded by petri PR #110 + DESIGN.md §3.1.** Kept as the reasoning that led
> there. The `Effect`/`AuditorTurn` data model in §(a), the §(a′) petri-changes table, and the
> §(d) spike plan were the *route* to what landed — petri's `Tape`/`Step`/`Node` (see
> [resampling.md](resampling.md) and `petri-meridian/src/inspect_petri/target/_history.py` @
> `4f4aa50`). Read DESIGN.md §3.1/§3.5/§3.7 for the current position; read this for why.

*The level below [TREE-IMPL.md](TREE-IMPL.md): the concrete data model, the inspect/.eval seam,
and the rollback/resample/edit code paths. Produced by an architecture brainstorm grounded in
source (refs checked 2026-06-13: petri-meridian @ 556c68d / inspect_petri 3.0.x,
collaborative-auditor, sonde-alpha-may-2026, inspect_ai main). Where this contradicts TREE-IMPL
§4 or DESIGN sketches, this document is the more-informed position — contradictions are listed
at the end and need ratifying into DESIGN.md.*

---

## (a) Recommended architecture — one page

> **Landed differently.** The `Effect`/`AuditorTurn` types below were a placeholder for "a
> durable, serializable record of what the audit did." PR #110 landed that as **petri's own
> `Step`** (`target/_history.py`): `Step` IS the durable serializable type (frozen dataclass
> with `dump()`/`load()` handling `ModelOutput` directly), `Tape` is the per-level log+replay
> queue, `Node` is `Tape` + tree pointers. There is no separate `Effect` log and no
> `AuditorTurn.effects` — what an auditor turn did to the target is exactly the `Step`s the
> audit-level tape recorded for it, and the invariant below is the tape's own correctness
> property (checked by petri's e2e tests: record→resample with zero model calls in the
> prefix). The "cold replay adapter" this section anticipated doesn't exist as a separate
> thing: `Tape(pending=seed)` IS the cold-replay path. See DESIGN.md §3.1 for the current data
> model. The two-trees-per-audit shape, `ChatMessage` as canonical payload, and the `Run`
> sketch all survive.

**Canonical store: two sonde-style node trees per audit (target tree = the specimen; auditor
tree = the Run's thread), bound by a durable, replayable ~~effect log~~ tape.** Petri's
~~`Trajectory`~~ `Tape`/`Channel`/`Controller` machinery is ~~the runtime replay engine, not
the store~~ **both** — PR #110 made `Step` serializable, so the in-memory record and the
persisted record are the same type.

```python
class Node(BaseModel):                      # one type, both trees (sonde tree.py:20 + actor)
    id: str                                 # == ChatMessage.id → petri anchor for free
    parent_id: str | None
    message: ChatMessage                    # inspect_ai's ChatMessage IS the payload
    actor: ActorRef                         # human:<user> | run:<id> | resolver:<kind>
    meta: NodeMeta                          # usage, request, grades, M-short-id, flags

class Effect(BaseModel):                    # one target-mutating auditor command — durable step
    kind: Literal["set_system","send_message","tool_result","prefill",
                  "add_tool","remove_tool","rollback","resume","end"]
    payload: dict                           # serialized Command (Stage value, anchor, ToolInfo…)
    produced: list[str]                     # target node ids created (resume → assistant node)
    recorded_output: ModelOutput | None     # for resume: the generation, replayable from store

class AuditorTurn(BaseModel):               # meta attached to each auditor assistant Node
    effects: list[Effect]                   # ordered; the fold of these IS the invariant
    target_leaf: str | None                 # derived cache: fold(effects on root→here path)

class Run(BaseModel):                       # DESIGN §3.2, amended
    id: str; kind: str; status: RunStatus
    tree_id: str                            # auditor tree (NOT a flat messages list — see flags)
    active_leaf: str                        # the pen position
    target_tree_id: str; config: dict; attached: ActorRef | None
```

**The invariant** (`target_state == replay(target-mutating effects of auditor path root→here)`)
becomes checkable data: the target leaf at any auditor node is `fold(effects along auditor
path)`, and the fold is executed two ways — live, by petri's replay (recorded steps re-fed
through `TargetContext.wait_for_resume`'s dispatch loop, `_context.py:348-382`), and cold, by
rebuilding a replay queue from stored `Effect`s. A consistency validator asserts both agree.

**Petri seam: embed the loop in-process, keep `inspect eval` as an import/export format.**
Petri's wiring below the task level is clean and small — `Channel` +
`init_controller(Controller(ch))` + `History` + two anyio tasks (`_auditor/auditor.py:65-117`) —
and every inspect runtime primitive it needs works in plain asyncio: `transcript()` lazily
self-initializes (`inspect_ai/log/_transcript.py:863-868`), `span()` works against it,
`get_model(role=)` resolves via a settable contextvar (`model/_model.py:2207-2221`),
`execute_tools` is a public export (`model/__init__.py:163`). Interactive *and fanout* Runs both
execute on the workbench runtime, because inspect provides **no supported path to
pause/steer/inject into a running sample** (confirmed: approval/ACP covers tool-approval only).
`.eval` import is a converter: `EvalSample.messages` = auditor lens, `EvalSample.timelines`
carries petri's full target tree (`inspect_ai/log/_log.py:447`; built at
`_auditor/auditor.py:197-211`); petri's own judge already proves tree reconstruction from it
(`_judge/branches.py:60-80`).

**Persistence: session dir is source of truth, three layers.** (1) `runs/<run>/tree.json` —
both trees + effects, written per mutation under a per-conversation lock (sonde `store.py`
fsspec atomic write); (2) `runs/<run>/events.jsonl` — append-only wall-clock provenance log
(sonde's verbatim principle); (3) `transcripts/<audit_id>.json` — a *path projection* emitted at
completion/branch-promotion, which is what summaries/grades key on. **The pen** is a store-layer
lease on `(tree_id, tip_node_id)` checked on every write, with the Run layer doing the
*scheduling* that makes lease acquisition graceful (pause-first interlock, per-Run feedback
queue, turn-boundary injection — collaborative-auditor's `playback.py:25-66` + `feedback.py:21-48`
pattern, kept).

---

## (a′) Petri/inspect changes that simplify this

> **Obsolete — PR #110 landed a coherent refactor instead of this piecemeal table.** The
> first two rows ("serializable `Step`" + "replay-queue value substitution") were the leverage
> and are exactly what landed: `Step.dump()/load()`, `Tape(pending=…)`, `Tape.replayable` /
> `Tape.compose`, `History.branch` over `Node`. The rest either landed alongside
> (`audit_tape()` contextvar; `_run_target` as `auditor.py:_run_trajectory`), became
> unnecessary (no `from_steps` adapter — `Tape(pending=seed)` is it), or remain as the two
> small inspect_ai re-export asks (`init_model_roles`, `Transcript.subscribe`). Kept below as
> the wishlist that shaped the PR.

We can modify petri (and propose to inspect). The mitigations in (c) were sized assuming a fixed
dependency; with patch access, several collapse to small upstream changes and the workbench
stays thin. In rough cost/benefit order:

| change | where | turns into | unblocks |
|---|---|---|---|
| **`Trajectory.from_steps(steps)` public constructor** + serializable `Step` (drop the leading `_`, give it a `model_dump`/`model_validate`) | `petri target/_history.py` | a documented function instead of vendoring ~60 lines | cold replay (spike 4), resume-after-restart, .eval-import branching |
| **Replay-queue value substitution**: `history.branch(anchor, current, overrides={step_idx: value})` | `petri target/_history.py` | one optional dict lookup in the replay path | human target-edits via *live* replay (eliminates the "store-backed only" carve-out in Q3) |
| **`AnchorMap` snapshot/restore** on `Controller` | `petri target/_controller.py` | `controller.anchor_map.state()` / `.restore(state)` | M-short-ids stable after process restart; we stop persisting it in `run.config` by hand |
| **Expose the pausable turn boundary**: split `auditor_agent`'s loop body into a public `auditor_turn(state, tools, …) -> state` | `petri _auditor/agent.py` | the loop we'd write anyway, but reusable upstream | our Run loop is `while …: await turn(); await step_gate` — no copy of compaction/continue-prompt/eager-resume logic |
| **`ResponseOutput` kept on the resume tool result** (structured, not just the rendered string) | `petri tools/_resume.py` | one extra field on the ToolResult | the dual-rendering trap (hard problem #2) — `render(effect)` is the *only* path because the structured value is what petri itself returns |
| **Public `init_model_roles`** | `inspect_ai model/__init__.py` | a re-export | running the loop outside an inspect task without a private import |
| **Public `Transcript.subscribe`** | `inspect_ai log/_transcript.py` | drop the `_` | sub-tool-granularity liveness without monkeypatching |
| **`_run_target` exported** (the rollback-respawn loop, ~30 lines) | `petri _auditor/auditor.py` | `from inspect_petri import run_target` | spike 1 imports instead of vendors |

The first two are the leverage: with serializable steps + override-on-replay, **the durable
`Effect` log and petri's `_Step` become the same type**, the cold/live distinction disappears,
and "hardest problem #1" is solved in petri rather than worked around in the workbench. Nothing
here changes petri's existing behaviour for existing callers — every change is additive (a
public name, an optional argument, an extra field). The inspect_ai changes are re-exports of
already-working private symbols.

**Sequencing:** land the petri changes on a `workbench-support` branch of petri-meridian first
(we control it), pin the workbench to that, upstream to meridianlabs in parallel. The inspect
re-exports go as a small PR; until merged, the workbench imports the private names with a
`# TODO upstream` comment — they're stable in practice.

---

## (b) Per-question analysis

### Q1. Representing conversations/trajectories

**The three candidate stores, as actually built:**

1. **Event-sourced DAG with dual patch streams (collaborative-auditor).** `EventNode` carries
   `auditor_patches` + `target_patches` (RFC6902, computed by `track_state_changes` diffing
   serialized state, `models.py:148-193`); `Branch` = tip pointer + deep-copied materialized
   state (`models.py:83-94, 258-279`); reconstruction replays patches root→event
   (`models.py:231-255`). Verdict: the *dual streams synchronized by handler discipline* is the
   documented bug farm (their own refactor plan), and JSON-patch-on-serialized-state is a
   write-amplifying, opaque encoding of what is really "a node was appended". Reject as
   canonical — but **keep two of its ideas**: the shared immutable event pool with
   branch-as-tip-pointer, and turn-level replay with argument overrides (`branching.py:33-102`).

2. **Parent-pointer node tree (sonde).** Flat `nodes: dict[str, Node]`, `parent_id`,
   `active_leaf`; path materialization is a parent walk (`tree.py:106-116`); edit = sibling node
   + copied active-path suffix (`tree.py:161-185`); per-mutation atomic JSON save (`store.py`);
   separate append-only `conv.events` provenance list. Verdict: the right substrate — branches
   are structural (shared prefix via parent pointers, zero copying), branch indicators are a
   children-count query, persistence is trivial. Its gap: single-actor, single tree, no notion
   of an agent whose actions *cause* target nodes.

3. **Petri `Trajectory` (recorded steps + replay).** A trajectory records every `next_command`
   return and `send_response`/`generate` result as `_Step`s (`target/_history.py:29-41,
   83-135`); a child trajectory replays the ancestor step queue truncated at the branch anchor
   (`_history.py:137-161, 190-209`), re-executing the *dispatch code* against recorded values so
   messages/tools/staging are rebuilt by construction. Verdict: this is the correct *mechanism*
   for the invariant, and the wrong *store*: steps hold live objects by reference (`Stage`
   docstring: "passed by reference (in-process channel) and never serialized", `_types.py:81`),
   `_Step` is a private dataclass, and the whole tree dies with the process.

**Decision: one Node type, two trees, cross-linked by effects (synthesis of 2 + 3).** Not "one
tree with auditor messages as nodes and target state derived per node": auditor actions that
mutate the target don't map 1:1 to target nodes (`rollback` produces none and moves the cursor;
`prefill` produces none but changes the next generation; `add_tool` order matters; one `resume`
can produce an assistant node whose formatted rendering also lands inside an auditor tool-result
message). The linkage must be an **ordered effect list per auditor turn**, not node references.
And not petri's Trajectory as the auditor tree (TREE-IMPL §4.1 proposed this — overruled, see
flags).

**Where Trajectory fits:** the live engine inside a running Run. Every fork on a live Run goes
through `History.branch(anchor)` (`_history.py:220-229`) so prefix model calls replay free. The
store-projection of the step list (our `Effect` log, with `ModelOutput` serialized) makes the
same replay possible *cold* — after restart, or for a branch whose live runtime is gone — by
reconstructing a replay queue from stored values. This is the one genuinely new mechanism we
must build (the "durable steps" adapter; petri needs either vendoring ~50 lines of `_history.py`
or an upstream public `_Step` constructor).

**Persistence reconciliation with TOOLS.md.** TOOLS.md §1 says "transcript = one JSON per audit,
audit id = `seed#replicate`" — but a transcript is one *path* through the tree. Resolution: the
tree file is the live truth; a **transcript is a projection of one root→leaf path**, emitted
when a Run completes or a human promotes a branch. Branch-of-a-transcript id:
`<seed>#<replicate>~<branch>` where `<branch>` is the server-assigned branch slug (children
creation-ordered, like petri's `branch N` naming from creation index,
`_target_timeline.py:50-55`). Grades/summaries key on these path ids in their JSONL files;
*node-level* grades additionally live in `node.meta.grades` (sonde `server.py:836-839` pattern)
so they survive re-projection and render in any lens. SQLite stays a derived index, never the
truth.

### Q2. The petri / .eval seam

**What petri actually exposes below the task level.** `audit()` (`_task/audit.py:19-97`) is a
thin Task wrapper over `audit_solver` (`_auditor/auditor.py:34-117`), which does five things we
can do ourselves: build `Channel(seed, seed_tools, metadata)`, `init_controller(Controller(ch))`
(`_controller.py:188-191`), default agents, then a task group running `_run_auditor` (auditor
agent + `end_conversation`) and `_run_target` (the rollback-respawn loop: run trajectory → catch
`RollbackSignal` → `history.branch` → new `TargetContext` → repeat, `auditor.py:127-158`). The
auditor agent itself is one `for` loop: compact → `generate` → `execute_tools` → check
`end_conversation` (`_auditor/agent.py:192-245`). **There is a clean seam: keep `target/_*` and
the tool set verbatim; replace `auditor_agent`'s loop with our pausable Run loop** (the loop is
where pause/step/feedback/pen live; the prompts and `auditor_tools()` come along as data).

**Runtime prerequisites outside an inspect task — all verified:**
- `transcript()` self-initializes a `Transcript` per contextvar (`log/_transcript.py:863-868`);
  petri's `AnchorEvent`/`BranchEvent` are upstream inspect event types (`event/_anchor.py`,
  `event/_branch.py`), and `Transcript` has a (private) subscription API for per-event callbacks
  (`_transcript.py:843-855`) — our in-process liveness tap.
- `get_model(role="target")` resolves via the `_model_roles` contextvar; `init_model_roles`
  exists but is **private** (`model/_model.py:2207`; not in `model/__init__.py`) — usable, flag
  for upstreaming.
- Model API outside eval keeps almost everything: tenacity retries (`model/_retry.py`),
  per-Model-instance `max_connections` + adaptive concurrency (`_generate_config.py:90-93` —
  *not* an eval-level semaphore), `CachePolicy` (epoch contextvar must be pinned), usage on
  `ModelOutput`. Lost: ModelEvents into an eval log (we have our own store), sandboxes (not
  needed for simulated-tool audits; revisit for petri-dish/ACP targets), eval-set retry
  bookkeeping.

**Options:**
- **A — embed in-process.** Full control: pause at turn boundaries, pen interlocks, human
  staging through the same `Controller`, live `History.branch` forks, per-event push. Cost: we
  own scheduling/retry-on-crash, and `.eval` becomes an import format.
- **B — everything is an inspect eval, side-channel steering.** Reality check kills it: realtime
  data is an *internal* SQLite buffer for `inspect view`
  (`log/_recorders/buffer/database.py:79-221`, undocumented); `.eval` zips are written
  incrementally but sample-at-a-time (`log/_recorders/eval.py:683-918`); and there is **no
  injection path** — human approval / `input_screen` / ACP are in-process tool-approval
  surfaces, not message-injection or pause APIs. Building a sandbox-service-style side channel
  into a running sample is a bigger, more fragile system than the workbench runtime itself.
- **C — hybrid.** Desk in-process; bulk via `inspect eval`. The trap: DESIGN §3.3's *punch-down*
  ("pin a row of a running fanout") is impossible against an inspect sample (see B). So fanout
  rows must also be workbench Runs.

**Recommendation: A for execution, C for interop.** All Runs (desk and fanout) execute on the
workbench runtime — a fanout is N Runs under a concurrency governor, each individually
pausable/pinnable, which is exactly what M2's per-row `pause/steer/stop/pin` requires. The
inspect-task path stays alive for two purposes: (1) **import** — petri evals run elsewhere/at
scale land as `.eval`; converter reads `read_eval_log_sample` (`log/_file.py:409-437`), takes
`EvalSample.messages` as the auditor lens (linear), rebuilds the target tree from
`EvalSample.timelines` (authoritative tree shape; `branched_from` anchors + replay-prefix dedup
exactly as `_judge/branches.py:60-80` does), falls back to AnchorEvent/BranchEvent + span
reconstruction from `EvalSample.events` when timelines are absent (failed samples —
`auditor.py:100-110` logs and continues); (2) **export** — a workbench Run can be projected to
the same transcript schema, and optionally re-emitted as an `.eval` for inspect-ecosystem
tooling. If we ever want overnight 5k-rollout scale with inspect's eval-set retry machinery,
hooks give per-event progress in-process of the eval (`Hooks.on_sample_event`,
`hooks/_hooks.py:147-160, 432`) — read-only monitoring, which is all that tier needs.

**Liveness story:** in-process Runs push through our own server-authoritative ViewState (WS +
JSON Patch, collaborative-auditor's refactored protocol — `common.py:71-93` computes
`jsonpatch.make_patch` over last-sent state). The per-event source is a callback at the Run loop
(after each effect / tool event), optionally augmented by `Transcript._subscribe` for sub-tool
granularity (streaming target tokens come later via sonde-style provider streaming in the target
agent).

~~**Provider-layer tension (design contradiction, see flags):** DESIGN §3.5 says "sonde's
provider layer, wholesale", but embedding petri means target generations go through inspect's
`Model.generate`. The clean reconciliation: reimplement the 65-line `target_agent` over sonde's
`Provider.stream()`…~~ **Resolved the other way (DESIGN §3.5, 2026-06-16):** one provider
stack — inspect's. Both auditor and target go through `get_model(role=…)` and wrap
`model.generate` via `tape.replayable(fn, source=…)` — symmetric. Sonde's
`Provider.stream`/`SurfaceEvent`/bijection-tests are **not adopted**; inspect's provider
conversions are faithful enough for the specimen, and streaming rides inspect's existing
event-mutation path ([STREAMING.md](STREAMING.md)). The corollary survives on its own merits:
**inspect's `ChatMessage` is the canonical node payload** — petri anchors are `ChatMessage.id`s,
the channel speaks them, `.eval` import is copy-not-convert.

### Q3. Rollback/resample/edit mechanics

**How petri's nested rollback actually works (walked, not assumed).** Auditor calls
`rollback_conversation(M2)` → `controller().rollback` resolves the short id via `AnchorMap`
(`_controller.py:145-149`) → `Rollback` command → target dispatch validates the anchor against
the whole history tree and raises `RollbackSignal` *after* acking (`_context.py:449-457`) →
`_run_target` catches it, `history.branch(anchor, current)` creates a child trajectory whose
replay queue is the ancestor chain truncated at the anchor (with trailing deterministic
`send_response` acks included — `_find_cutoff`, `_history.py:190-209`) → a **fresh
`TargetContext` re-runs the target agent code**, with `next_command`/`send_response`/`generate`
all returning recorded values until the queue drains; queue-drain emits `BranchEvent` and goes
live (`_history.py:103-133`). Critically: during replay the auditor isn't sending — the channel
is rendezvous (buffer 0, `_channel.py:36`), so the auditor's next live command simply blocks
until the target's replay catches up and reads it live. Command *side effects* re-execute
(staging store, `_add_tool` mutations); command *responses* and *model calls* replay. The
invariant holds by re-running the same code on the same inputs.

**State NOT captured by the command stream:**
- **`AnchorMap`** lives on the auditor-side `Controller` (`_controller.py:28-50`), shared across
  all branches, monotonic. In-process this is a feature (short ids stable across siblings;
  cross-branch rollback supported via full-tree anchor search, `_history.py:242-248`). It is
  *not* persisted anywhere — resume-after-restart must restore the counter + map or M-ids shift
  under the auditor. Store it in `run.config`.
- **Tools on restart**: `rollback("")` empties the replay queue, so `AddTool` commands are *not*
  replayed; `restart_conversation` compensates by snapshotting and re-issuing
  (`_context.py:423-427`, `tools/_conversation.py:136-148`). Any direct `rollback("")` caller
  (our handlers!) must do the same.
- **System prompt / staging policy / message list**: all rebuilt by construction. Nothing to
  persist.
- **Span counters**: per-trajectory, designed to stay in sync under replay (`_history.py:80-81`).
- **Anything not wrapped in `replayable`** re-executes for real — custom auditor tools with side
  effects (workspace writes, petri-dish scaffold injections) are outside the guarantee. Our
  rule: every target-touching call in a target agent must go through `context.replayable` (as
  `generate` does, `target/_agent.py:20`).

**Auditor edit at turn k, step by step:**
1. **Pause** the Run at a turn boundary (cancel-scope task pattern; collaborative-auditor
   `playback.py:25-66`). Mid-turn edits force a pause-first; if the turn is in flight,
   hard-cancel both tasks and *re-derive* (step 6) — never try to unwind a half-executed turn.
2. **Fork the auditor tree** at parent(turn-k node); create the edited assistant node
   (`actor=human:<user>`, provenance dropped per sonde's payload/provenance split,
   `tree.py:41,161-185`).
3. **Compute the target branch anchor** = last `produced` node id of the last `resume`/stage
   effect on the shared prefix (turns 1..k-1). Pure data from the effect log.
4. **Branch the live petri runtime**: spawn a replacement target task — `history.branch(anchor,
   current_traj)` + new `TargetContext` (exactly `_run_trajectory`, `auditor.py:142-158`).
   Replay rebuilds target state to the anchor with zero model calls.
5. **Re-issue the edited turn's commands** through `execute_tools` on the edited tool calls
   (collaborative-auditor's `replay_turn` with `argument_overrides`, `branching.py:33-102`,
   rebuilt on the controller): staging commands re-stage, `resume` triggers a **live** target
   generation (post-branch-point). New effects + produced target nodes are recorded on the new
   branch.
6. **Resume the auditor loop** from the new tip: input messages = materialized auditor path
   root→tip (parent walk). Old branch keeps its nodes, effects, and target subtree untouched.
7. **Validate**: `channel.state.messages` (live, `_types.py:165-199`) must equal the fold of
   effects on the new path. CI-grade assertion, every fork.

**Auditor resample at turn k:** identical fork + target branch (steps 1-4), but step 5 calls the
auditor model live on the rebuilt path (plus the standard continue prompt, `agent.py:241-245`).
**Anchor stability across siblings**: replay returns the *same recorded message objects*, so
prefix message ids — and therefore M-ids — are identical in every sibling; `AnchorMap` is shared
and append-only, so new messages per sibling get fresh, non-colliding M-ids. Petri already
supports the auditor rolling back to a *different sibling's* anchor (full-tree search fallback,
`_history.py:242-248`). Caching caveat: scope by trajectory path or siblings collide (petri
solved this: `scoped_cache`, `_context.py:113-123` — keep it).

**Target-side edit/resample (human edits a target message under a Run):**
collaborative-auditor's answer is fork-at-parent, discard auditor suffix (`editing.py:116-143`;
resample: fork at `TOOL_CALL_ADDED`, re-run target model, `branching.py:194-217`) —
directionally right, with two additions the dual-tree model forces:
- **The auditor suffix is invalidated and belongs to the old branch.** The auditor's turn-j
  `resume` tool-result *embeds the formatted target output as a string* (`tools/_resume.py:74-92`)
  — so editing target assistant node T requires forking the auditor tree at turn j and
  *re-rendering* that tool-result node from T′. Keep the formatter pure and store the structured
  `ResponseOutput` in the effect so re-rendering is deterministic.
- **Live petri can't replay a substituted value** — replay reproduces the *recorded* message;
  `Trajectory` has no override slot. Human target-edits therefore go through the **store-backed
  replay**: rebuild a fresh trajectory whose replay queue is the stored prefix steps with T′
  substituted (edited *assistant* message → swap the recorded `ModelOutput` step value; edited
  *user/system* message → swap the `Stage` payload). This is the single strongest argument for
  durable steps being canonical rather than a backup.

**Turn grouping / fork granularity:** a petri auditor turn = one assistant message + all its
tool calls executed by `execute_tools` (`agent.py:213-232`), and petri *encourages* multi-call
turns (rollback + send_message + resume in one turn — system prompt, `agent.py:526-533`; plus
`eager_resume` injection, `agent.py:67-101`). **The atomic fork unit is the auditor turn.**
Sub-turn operations (edit one tool call's arguments) are turn-level replays with overrides —
collaborative-auditor converged on exactly this after granularity confusion (refactor plan;
`branching.py:157-191`). Branch indicators: auditor-lens `‹1/2›` derives from auditor-tree
children counts; target-lens indicators from target-tree children; both server-computed from
tree indexes (never per-view — DESIGN §3.6 holds).

**The pen:** enforce at **both** layers, different jobs. Store layer holds the lease — a write
to tip node N requires the caller's `ActorRef` to hold `lease(tree_id, N)`; violations raise
(fail fast — this is the structural guarantee that a human turn never races an agent turn). Run
layer does the *scheduling* that makes lease acquisition graceful: pause-first interlock,
per-Run feedback queue drained at turn boundaries (`feedback.py:21-48`: queue during generation,
drain on pause, close dangling tool calls with "interrupted" results first — `auditor.py:527-546`;
keep all of it), pin = `Run.attached` + lease priority. A lease is held by the Run while
playing, transferred per-action to the human (DESIGN §3.3 "pen transfer is per-action"), and
every branch tip has at most one.

---

## (c) The five hardest problems

1. **Durable replay vs petri's by-reference steps.** Petri's whole guarantee lives in in-memory
   `_Step`s that are private and unserializable; we need fork-after-restart and human-edit
   substitution, which petri's replay can't express. *Mitigation:* the `Effect`/durable-step
   log as canonical; a small adapter that rebuilds a `Trajectory` replay queue from stored
   values (vendor ~60 lines of `_history.py` or upstream a `Trajectory.from_steps()`); golden
   tests asserting live-fold == cold-fold == `channel.state.messages` on every fork shape.

2. **Dual representation of target output inside auditor messages.** The target's reply exists
   twice: as a target-tree node and as a formatted string inside the auditor's `resume` tool
   result (`_resume.py:47-92`). Target-side edits must regenerate the auditor-side rendering, or
   the two lenses silently diverge — collaborative-auditor's bug farm in new clothes.
   *Mitigation:* effects store the structured `ResponseOutput`; the auditor-visible string is
   always `render(effect)` — a pure function — re-rendered on fork; a CI invariant ensures no
   handler ever writes the string directly.

3. **Mid-turn interruption without corrupting the channel.** The channel is a rendezvous pipe;
   cancelling the auditor while the target awaits a command (or vice versa) deadlocks or orphans
   state, and `execute_tools` may be half done. *Mitigation:* turn boundaries are the only
   graceful pause points (collaborative-auditor's `_ALLOWED_DURING_GENERATION` gate,
   `server.py:99,134-139`, is the right shape); "pause now" = hard-cancel both tasks + rebuild
   the runtime from the last completed turn via cold replay — re-derivation makes hard-kill
   safe, so we never write unwind logic.

4. **ViewState cost and sync under live trees.** collaborative-auditor reserializes everything
   per push (`build_view_state` O(branches×messages), `view_state.py:67-102`). With two trees,
   multiple Runs, and per-event pushes this melts. *Mitigation:* maintain ViewState
   incrementally per mutation (the store knows exactly which node/effect changed → emit the JSON
   Patch directly, don't diff full states); persistent tree indexes (children counts, branch
   points) updated on insert; full-state only on (re)connect.

5. **.eval import fidelity.** Timelines are absent on crashed samples (`auditor.py:100-110`),
   compaction rewrites the auditor's *input* so `EvalSample.messages` isn't literally what the
   model saw (`agent.py:199-201`), eager-resume injects synthetic tool calls
   (`agent.py:67-101`), attachments need resolution. *Mitigation:* import primary path =
   timelines (authoritative tree shape, replay-prefix dedup per `_judge/branches.py`); fallback
   = event-stream reconstruction; imports are marked `lossy: true` in meta and open as
   continuation-mode specimens (read-only history, branch-from-here allowed); compaction events
   render as first-class markers, never silently flattened.

---

## (d) M0 spike plan — the riskiest vertical slice

> **Status:** spike 1 passed (petri loop runs in plain asyncio outside an inspect task).
> Spikes 2–5 are now mostly **covered by PR #110's own tests** rather than workbench spikes:
> `Step.dump()` is the store projection (spike 2's `Effect` log doesn't exist as a separate
> thing); `dd4a39d` is the e2e test asserting record→resample makes zero model calls in the
> prefix (spikes 3+4's exit criteria); `tape_from_messages` + `load_tape` are the `.eval`
> import path (spike 5). What remains workbench-side from this plan: the pausable `step_gate`
> Run loop, and the `tree.json` writer. Kept below as the original plan.

Goal (per TREE-IMPL §4): **a `Run` wrapping the petri runtime, one auditor-edit-at-turn-k round
trip, invariant provably held, two-branch tree rendered — plus the cold-replay variant nobody
has built.** No frontend, no ACL, no orchestrator. ~3-5 days.

1. **Petri loop outside inspect (½ day).** Plain asyncio script: `init_model_roles` (private —
   note for upstreaming) with auditor + target models; `Channel(seed_instructions=...)`,
   `init_controller(Controller(ch))`, `History()`; anyio task group with (a) vendored
   `_run_target` loop (`auditor.py:127-158`, ~30 lines), (b) **our** auditor turn loop:
   `get_model(role="auditor")` → `generate(input, tools=auditor_tools())` → `execute_tools` →
   await a `step_gate` (asyncio.Event) — the pause/step/play primitive. *Exit: a 6-turn audit
   completes; `transcript().events` has ToolEvents/AnchorEvents with no inspect eval anywhere.*

2. **Projection into the store (1 day).** Wrap `Controller.stage/rollback/resume/add_tool` (or
   tap `channel.request`) to emit `Effect` records; mirror `channel.state.messages` deltas into
   target-tree `Node`s (ids = `ChatMessage.id`); auditor turns into auditor-tree `Node`s with
   `AuditorTurn.effects`. Write `tree.json` per turn (atomic). *Exit: `fold(effects,
   root→leaf)` reproduces `channel.state.messages` exactly — the invariant validator exists and
   passes.*

3. **Live edit-at-turn-k (1 day).** Pause at gate; fork auditor tree at turn k with an edited
   `send_message` argument; compute prefix anchor from effects; `history.branch` + new
   `TargetContext` + respawned target task; re-issue the edited turn's commands; resume the
   auditor loop on the new path. *Exit: two branches in the store; new branch's fold == live
   target state; old branch byte-identical to its pre-fork serialization; prefix produced zero
   target-model calls (assert via a counting wrapper on `generate`).*

4. **Cold replay + target-edit substitution (1-1.5 days).** Kill the process. Reload
   `tree.json`; rebuild a `Trajectory` replay queue from stored effects (the `from_steps`
   adapter — the riskiest new code; budget for vendoring `_history.py`); resume the audit. Then
   the substitution case: edit a stored target *user* message, rebuild with the swapped `Stage`
   payload, verify the auditor-side `resume` tool result re-renders from the new
   `ResponseOutput` and the suffix lands on a new branch. *Exit: a restarted process continues
   an audit; a human target-edit forks correctly with no model calls in the prefix.*

5. **.eval import (½ day).** `read_eval_log_sample` on a real petri-meridian `.eval`; convert
   `EvalSample.messages` + `EvalSample.timelines` → the same store schema; run the invariant
   validator on the imported trees. *Exit: an imported audit opens as a continuation specimen
   and branch-from-imported-node works via cold replay.*

Render throughout with a throwaway text/HTML tree dump — the M0 React work proceeds in parallel
against the same `tree.json` schema.

---

## Flags: where this contradicted the design docs

*Ratified into DESIGN.md / TOOLS.md / TREE-IMPL.md 2026-06-13; updated 2026-06-16 against
petri PR #110 + DESIGN.md §3.1/§3.5. Kept as the reasoning.*

1. **DESIGN §3.2 `Run.messages: list[ChatMessage]` cannot survive Q3.** A flat list can't hold
   auditor-side branch/edit/resample. The Run's thread must be a tree. **Landed:** DESIGN §3.1
   — both trees built from petri's `Node`; `Run.tree_id`/`active_leaf`.
2. **DESIGN §3.1 "Run *references* the target nodes it created" is insufficient** for the
   invariant — rollback/prefill/add_tool mutate target state while producing no node. The
   linkage must be ~~the ordered effect log~~ the audit-level tape. **Landed:** the linkage IS
   the tape's `Step` log; no separate `Effect` type (DESIGN §3.1 first bullet).
3. **TREE-IMPL §4.1 "the auditor tree becomes a petri `Trajectory` tree" — overruled** (steps
   were by-reference, unserializable, private). **Landed differently:** PR #110 fixed exactly
   that — `Step` is now public, frozen, serializable (`dump()/load()`), so petri's own `Node`
   tree IS usable as the replay substrate at both levels. The "store nodes vs. live engine"
   split this flag drew no longer exists.
4. ~~**DESIGN §3.5 "sonde provider layer, wholesale" conflicts with embedding petri.**
   Resolution: reimplement `target_agent` on sonde providers.~~ **Moot — resolved the other
   way:** DESIGN §3.5 now says one provider stack (inspect's); sonde's provider layer is not
   adopted. `ChatMessage` as canonical payload survives independently.
5. **TOOLS.md §1 "transcript per audit" needs a branch-id story** — transcripts are path
   projections; `seed#replicate~branch` ids, with the tree file as live truth. **Landed:**
   DESIGN §3.7.
6. **DESIGN §3.3's punch-down rules out inspect-task fanouts** — inspect has no
   pause/steer/inject path into a running sample. Fanout rows run on the workbench runtime;
   `inspect eval` is an interop format. **Landed:** DESIGN §3.7 last paragraph.
