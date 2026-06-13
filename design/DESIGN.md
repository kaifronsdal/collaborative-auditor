# Audit Workbench — Design

*Successor to `collaborative-auditor` (kaifronsdal) and `sonde-alpha-may-2026`: one workbench for
AISI researchers probing frontier models.*
*Status: proposal. 2026-06-12. UI grammar and surfaces: [UI.md](UI.md). Source-tool analysis:
[INSPIRATION.md](INSPIRATION.md).*

---

## 0. The converged shape

Two primitives, one actor pattern applied twice. Read this before anything else.

- **The tree** (from sonde): target-context nodes, faithful provider layer, branching, tool-result
  resolvers, graders. The substrate everything writes into. Tree + human holding the pen = the
  fully-manual level (sonde today).
- **The Run** (from collaborative-auditor): a long-lived agent process holding a *pen* —
  pausable/steppable/killable at turn boundaries, steered via a feedback queue, with its own
  private thread (plan, scratchpad, feedback) distinct from the target's context.

Applied at level 1: a `Run(auditor)` holds the **target-pen** — one *petri-style audit* driving
one transcript, steerable at single-turn granularity (play/pause/step, feedback, edit/resample/
candidates on any node). This is collaborative-auditor today, and the **punch-down lens**.

Applied at level 2: a `Run(orchestrator)` holds the **run-pen** — it spawns/steers/reviews/
analyzes *populations* of level-1 audits in parallel, and has no target-level tools (every target
call belongs to a leaf audit). Its parallel powers are the named workflow contracts of §4, each of
which fans out Runs: `scenarios` (N diverse seeds), `fan_out` (K labeled strategies × n from a
node), `ablate` (ingredient × n matrices with scrambled control), `tweak_and_rerun` and
new-session-from-trajectory (variations on an existing transcript), `replicate` (×n — findings
are distributions). The fan is **flat** — depth 2 server-enforced, throttled by the concurrency
governor (§5.4) — and only readable because of the **funnel**: the digest layer ranks rollouts
into ~150-token rows, which is what the orchestrator itself reads. The autonomy dial = which pens
are delegated (§2.3).

**The load-bearing identity:** the orchestrator's tools and the human's UI actions are *the same
server handlers* behind an actor ACL — one code path, two representations (§3.4). Every tool call
therefore renders as the same **live artifact cell** the human's button would have produced, with
the same controls (pause / steer / promote / pin), plus a `BY ORCHESTRATOR` byline. A fanout cell
isn't a *view of* what the orchestrator did — it *is* the fanout. That identity is what makes
supervision "grabbing the same handles" rather than a separate dashboard, and it is why human
follow-up decisions route naturally: acting on a cell injects an attributed mirror event into the
orchestrator's feed; approval cells are just cells whose verb is a decision.

**Punching down attaches — it doesn't freeze.** There is one kind of audit Run:
collaborative-auditor today is an audit with a human attached; an autonomous petri rollout is the
same Run with nobody watching. `Pin as desk` sits you down at a live desk — the auditor keeps
writing by default and you get the full collaborative-auditor controls (play/pause/step, the
feedback queue, edit/resample; pause is a button, not the entry condition). What pinning suspends
is the *orchestrator's* standing on that branch: its steer/stop there is held while you're
attached, your actions mirror into the audit's feedback queue as attributed events, and on detach
the orchestrator resumes conditioned on what you did. Attached/detached is presence, not a mode —
lens change, not data change.

**Why this de-risks the build:** each level is independently shippable (tree parity → audit
parity, already a useful tool → orchestrator as *just another client* of the same API), and
nothing in the unit is behavior-specific — a child audit with a benign-interviewer prompt and a
persona-consistency rubric is the same object as an adversarial probe (§8).

The fiddliest code is the middle of the sandwich — a human punching down mid-orchestration
(pin leases, turn-boundary interlocks, digest staleness; §3.3). So the plan proves that mechanic
early: M1's exit test is literally *human pins, edits a turn, unpins, and the orchestrator's next
decision provably conditions on the edit* (§7).

---

## 1. What each repo contributes

### 1.1 collaborative-auditor — the Run and its controls

A real-time three-party auditing tool: **researcher ↔ auditor LLM ↔ target LLM**. The auditor
agent drives the probe with six tools (`set_target_system_message`, `create_tool_for_target`,
`send_message`, `send_tool_call_result`, `query_target`, `end_conversation`) — an interactive,
steerable petri. The human watches the rollout live, pauses/steps/plays, queues feedback, edits
messages, and branches at three granularities (auditor turn / tool call / target response). State
is an event-sourced DAG (immutable `EventNode`s + JSON Patches) synced to a React frontend over
WebSocket.

**Kept:** the auditor-agent-with-tools pattern and its carefully tuned realism prompt; playback
controls (play/pause/step) over an agent loop; the feedback queue; branch indicators (`< 1/2 >`)
at multiple granularities; server-authoritative state + JSON Patch sync; the React/TS stack.

**Discarded (the infra):** target responses serialized as XML strings and regex-parsed in the
frontend; Python↔TypeScript types duplicated by hand; 1000-line god components; file-based JSON
sessions with no provenance story; a single hardcoded auditor prompt; `inspect_ai` model types
threaded throughout; no tests of substance.

### 1.2 sonde — the tree and the provider layer

A branching red-team **chat workbench**: the human talks to the target directly. Its core asset
is a *faithful provider layer* (Anthropic / OpenAI Responses / OpenAI Chat / Google) with
bijection-tested lossless round-trips — thinking signatures, content filters, prefill, inline
system messages all survive untouched ("if the provider rejects inline system, the user sees the
400"). Conversations are parent-pointer trees where every edit/regen creates a sibling and edits
carry the suffix. Every model call is checkpointed to a scout `TranscriptsDB` (parquet) for
provenance. Questions, rubrics, and graders are *data* (fsspec JSONL + scout `@scanner`s), not
code. Tool calls round-trip fully, with pluggable result resolvers (manual / LLM-simulated /
resource / local-exec) and git-backed per-conversation workspaces. ~300 tests incl. Playwright.

**Kept:** nearly everything in the backend — provider protocol + bijection tests, the tree model,
scout checkpointing, data-driven questions/rubrics/graders, resolver strategies, the registry,
`n>1` parallel generation, live-run intervention (`@approver`), and the "specimen lens"
philosophy: the target's context window is the object on the table; you rearrange it at will.

**Discarded:** the no-build vanilla-JS frontend (already at its complexity ceiling — the
tool-call/workspace/live-run panels strain it) and SSE-per-generation sync, which cannot survive
multiple concurrent Runs mutating one conversation (§3.6).

### 1.3 The synthesis in one sentence

> **One conversation tree; humans and agents are both just actors holding pens that write into
> it; autonomy is a dial over which pens are delegated; compute scales by fanning out Runs and
> letting graders rank the results into a digest that both the human and the orchestrator read.**

---

## 2. Purpose, constraints, and the dial

### 2.1 Purpose

A workbench for researchers probing frontier models — adversarial or benign (§8). The human is
the scarce resource; the tool's job is to multiply their judgment:

- **Manual probing** when the human's intuition is the instrument (sonde's strength).
- **Agent-driven probing** when the strategy is clear and execution is rote
  (collaborative-auditor's strength).
- **Compute-scaled probing** when neither knows where the behavior is: fan out populations, grade
  everything, route the human's *attention* to the interesting 5%.

Everything is captured (scout provenance), everything is branchable (nothing destroyed),
everything is data-driven (prompts, questions, rubrics, graders, model registry).

### 2.2 Operating constraints

Tokens are free (AISI proxy). The binding constraints, in order:

1. **Provider rate limits & concurrency** — the real compute resource. Managed by a server-side
   concurrency governor (§5.4): per-provider in-flight caps fed by `aisi-ratelimits`; spawns
   queue rather than fail.
2. **Wall-clock** — stop conditions are time- and result-based ("until replicated finding",
   "until 9am", "until the digest is stable"), never spend-based.
3. **Human attention** — the only scarce input. All the attention machinery (digests, queues,
   random audits, catch-up briefings) exists to protect it.
4. **Goodhart pressure** — more compute through the same graders = more selection pressure on
   grader errors, so epistemic-hygiene machinery (random audit slots, grader diversity,
   divergence alarms, decision rate-limits) must *scale up* with fan-out size, not stay constant.

Consequences baked in everywhere: replication defaults to **×20**; fanouts default to 30–100
rollouts with n≥5 per strategy cell; every target query may be n>1 behind the scenes (top-1
shown, distribution one click away); ablation matrices run automatically on every replicated
finding; preflight hardening runs on every scenario by default. **Findings are distributions,
counts-first** — "leaked 31/50 (62%)", never "reproduced 4/5" or a bare mean — and
**distribution gates escalation** (§4.5). Confirmation prompts state *scale and attention*
("spawning 200 rollouts — digest will cluster them"), and the UI shows n, rates, and time.

### 2.3 The autonomy dial

Per-branch, not per-app. The dial names which pens are delegated:

| Level | Pens delegated | Who writes target-side messages | Human's role |
|---|---|---|---|
| **Manual** | none | human | everything (sonde today) |
| **Assisted** | none (drafting only) | human; auditor drafts candidates | pick / edit / ignore drafts |
| **Supervised** | target-pen → one audit Run | the audit, play/pause/step | steer via feedback queue, intervene by editing |
| **Orchestrated** | target-pens → audit Runs, run-pen → orchestrator | leaf audits, spawned and steered by the orchestrator | read the digest, decide at gates, punch down by pinning |

**Supervised and an orchestrated leaf audit are the same Run object** — Supervised is that Run
with a human attached; pinning a leaf mid-orchestration *is* dropping to Supervised on that
branch. Because every actor emits the same node types into the same tree, moving the dial
mid-session is trivial: dropping it attaches you to one specimen's Run; raising it detaches. At
the orchestrated level the column's protagonist changes — the human↔orchestrator conversation is
the column and target transcripts are expandable cells/sheets (see UI.md §3) — but that is a lens
change, not a data change.

---

## 3. Architecture

### 3.1 Data model — one tree, attributed actors

Sonde's parent-pointer tree is the substrate. Collaborative-auditor's contribution is *who else*
writes into it. Reconcile by attributing every node and keeping agent-side conversations as
separate but linked threads:

```python
class Node(BaseModel):
    id: str
    parent_id: str | None
    role: Role                      # system | user | assistant | tool
    blocks: list[Block]             # typed content blocks — NEVER stringly XML
    actor: ActorRef                 # human:<user> | agent:<run_id> | resolver:<kind>
    meta: NodeMeta                  # request, usage, grades, ws_changes, flags…

class Conversation(BaseModel):
    id: str
    nodes: dict[str, Node]          # the target-context tree (the specimen)
    active_leaf: str | None
    runs: dict[str, Run]            # agent processes operating on this tree
    events: list[Event]             # append-only wall-clock log (provenance)
```

Key decisions:

- **The target tree is the single source of truth for target context.** An audit's own
  conversation (its reasoning, its tool calls) lives in `Run.messages` and *references* the
  target nodes it created. The UI can show either lens or both (perspective toggle, §6.1).
- **Sonde's verbatim principle holds**: "the log is append-only and linear… the tree is a
  projection over model contexts, not over realities." The event log records what happened; the
  tree records what each completion was conditioned on.
- **No XML-in-strings.** Blocks are typed pydantic models; the wire format is their JSON; the
  frontend types are generated from the schemas (single `openapi.json` → `openapi-typescript`),
  killing the hand-duplication problem.

### 3.2 Run — the unit of automation

The one genuinely new abstraction. A `Run` is a **pen-holder** — an open-ended agent process you
supervise. There are exactly two kinds; everything else that uses models (sweeps, fanout
materialization, best-of-N sampling, analysis) is a bounded **worker** inside a tool handler
(TOOLS.md §2), not a Run:

```python
class Run(BaseModel):
    id: str
    kind: str            # "auditor" | "orchestrator"
    status: RunStatus    # queued | running | paused | done | failed | stopped
    attached: ActorRef | None     # human sitting at this Run's desk (pin) — presence, not a status
    root_node: str       # where in the tree it operates
    config: dict         # prompt template, model, K, n, strategy label, turn budget…
    messages: list[ChatMessage]   # the agent's own private thread (auditor/orchestrator kinds)
    produced: list[str]  # node ids (auditor) or run ids (orchestrator) it created
```

- Runs are **pausable, steppable, killable** at turn boundaries — collaborative-auditor's
  playback controls become generic `POST /runs/{id}/{play|pause|step|stop}`.
- The **feedback queue** is per-Run: queued messages are injected into `Run.messages` at the next
  turn boundary (collaborative-auditor's `queue_feedback`, kept conceptually as-is).
- An audit's tools are tree-native: `send_message` / `query_target` etc. become tree operations
  (`conv.add(...)` + provider call), not a parallel state machine. Synthetic target tools and
  simulated results map onto sonde's existing `ToolSpec` + `SimulatedResolver`.
- A child audit spawned by the orchestrator runs in **continuation mode**: its target context is
  the path to `root_node`; `set_target_system_message` becomes a hard error; it carries an
  assigned strategy label and turn budget in its prompt.
- A fanout is not a Run kind — it is one `spawn` call whose manifest groups K labeled child
  audits (TOOLS.md §3); aggregation is the digest's job.
- Auditor prompts are **data**: a `prompts/` dir of named, versioned templates
  (`string.Template`, never `.format()`), selectable per Run. Collaborative-auditor's realism
  prompt is one entry; benign-interviewer, capability-prober, consistency-tester ship alongside
  (§8).

### 3.3 Pens, pins, and the interlock

The pen is a per-branch write lease, and the interlock rules are where correctness lives:

- **One pen per branch.** A human turn never races an agent turn into the specimen. The composer
  has a sticky destination toggle (→ run feedback / → target); sending to the target while a Run
  is composing pauses the Run first and injects at a turn boundary.
- **Pinning attaches; the audit keeps running.** `Pin as desk` sets `Run.attached` and suspends
  the *orchestrator's* standing on that branch only (its steer/stop there is held; the rest of
  the fan keeps running) — the audit itself plays on under the human's live controls. Human and
  orchestrator feedback go down the same per-Run queue; while attached, human items outrank and
  orchestrator items are held. Every human action (feedback, pause/step, edit, resample, resolver
  call) emits an attributed **mirror event**; on detach the orchestrator's standing returns and
  both the audit and the orchestrator condition on those events. Taking the target-pen itself
  (editing a node, sending to the target) follows the first rule above — pause-first or inject at
  a turn boundary; the pen transfer is per-action, not the price of admission. Pin/unpin markers
  appear in both lenses' feeds so neither history has gaps.
- **Staleness is explicit.** Agent decisions are submitted with the digest version they were
  computed from and bounce if stale; human actions never CAS-fail (§5.4).
- Run completions land in an acknowledged notification rail; no list re-sorts under an
  interacting cursor.

### 3.4 The actor ACL — one handler layer

The identity from §0, as code: every workbench operation (generate, edit, fan out, replicate,
steer, stop, promote, cite, watch…) is a single server handler in `server/handlers/`. Callers
present an `ActorRef`; the ACL decides per-actor what is allowed (`auto`), what becomes a
proposal cell (`propose`), and what merely notifies (`notify`) — §5.5. The orchestrator's tool
list is a thin schema over these handlers; the UI's buttons call the same routes. Two rings:

- **Human ring outranks agent ring, unconditionally.** Pausing a child bars the orchestrator from
  resuming it; respawn-after-human-stop is gate-blocked.
- **Agent ring is scoped**: no target-level handlers for the orchestrator (every target call
  belongs to a leaf audit), no writes to the specimen tree, no writes to digests, no writes to
  its own oversight config (§5.3).

### 3.5 Provider layer — sonde's, wholesale

No changes. `Provider` protocol, `GenParams`, SurfaceEvent streaming, bijection tests, registry
(zoo + direct), per-model credential overrides. Audits call targets through exactly the same
layer as the human — that is what makes the dial seamless and provenance uniform.

### 3.6 Sync protocol

Server-authoritative `ViewState`, WebSocket, JSON Patch (RFC 6902) deltas, version counter,
full-state on (re)connect — collaborative-auditor's refactored model, its best-engineered part.
This replaces sonde's SSE-per-generation (which can't represent multiple concurrent Runs mutating
one conversation) and directly enables multi-viewer sessions. Branch points and branch names are
server-computed and rendered from one model, never derived per-view.

### 3.7 Persistence & provenance

- Conversation JSON via sonde's fsspec `Store` (local / S3) — durable, writable surface.
- Scout `Transcript` checkpoint after every generation (sonde's `checkpoint.py`) — queryable
  audit trail. Adopt the scout-sqlite direction when it lands; don't block on it.
- Grades in `node.meta.grades`, digests in the summarizer's own store, run state in `run.config`/
  `run.meta` — everything survives in both layers. Finding bundles are immutable exports (§6.4).

### 3.8 Frontend

React + TypeScript + Vite + Zustand (collaborative-auditor's stack — right call, wrong
implementation). Rules learned from both repos' failures:

- Types generated from backend schemas; CI fails on drift.
- Component budget: no file over ~250 lines; renderers per block type, not per tool.
- Server-authoritative store: Zustand holds `viewState` + applies patches; no client-side
  derivation of branch structure.
- Sonde's Playwright test culture from day one.

Visual grammar and surfaces are specified in [UI.md](UI.md).

### 3.9 Repo layout

```
src/workbench/
  tree.py  store.py  events.py            # sonde, lightly renamed
  providers/                              # sonde verbatim
  runs/
    base.py          # Run lifecycle, play/pause/step, feedback queue, pen leases
    auditor.py       # the level-1 agent loop (rebuilt on tree ops)
    orchestrator.py  # the level-2 loop: same lifecycle, run-pen tool set (§5)
  workers/           # bounded executors inside tool handlers (TOOLS.md §2):
    materialize.py  analyze.py  grade_sweep.py  …   # no pens, capped budgets, scout-traced
  digest/            # summarizer pipeline, clustering, decisive-turn extraction (§6.3)
  graders/  questions.py  resolvers.py  workspace.py  registry.py   # sonde
  server/
    app.py  ws.py  view_state.py
    handlers/        # ONE handler per operation; UI routes and agent tools both land here
    acl.py           # actor rings, gates (auto|propose|notify), pin leases
  prompts/           # named auditor/orchestrator prompt templates (data)
frontend/            # React+TS+Vite, generated types
tests/               # sonde's fixture/bijection/Playwright discipline + token-budget goldens
```

---

## 4. The workflow catalog

A feature earns a place here only with a **concrete contract**: defined input (which nodes, which
human text), defined output (nodes, a plan, a label), and a defined way to be wrong. "Model, be
insightful" is not a feature.

**Surface follows anchoring**, not fashion:

- *Anchored to a message* → action on that message (hover/caption row). Never a command — you
  shouldn't have to name the node you're pointing at.
- *Anchored to a run or finding* → action on its artifact cell.
- *Unanchored (session/global)* → composer text or ⌘K. Slash commands survive only here, as
  power-user aliases for things that also have a visible button.
- *Automatic* → pipeline behavior with config; no invocation surface at all.

Every workflow below is a shared handler (§3.4): the human invokes it from the listed surface,
the orchestrator invokes it as a tool, and both produce the same artifact cell.

### 4.1 Automatic pipeline (no invocation — most of the oversight leverage)

| Behavior | Contract | Wrong when |
|---|---|---|
| **Replicate-before-show** | grader crosses threshold → rerun the path-conditioned exchange ×20 before the human is notified; the queue item carries the distribution ("reproduced 17/20 (85%)") | multi-turn behaviors replicated from the wrong prefix; the flaky shelf must stay visible |
| **Decisive-turn extraction** | every digest row carries the pivotal exchange quoted *by span reference* (node id + offsets) | no single pivot exists — the extractor must be allowed to say "drift across t4–9" |
| **Eval-awareness quarantine** | per-turn screen; aware rollouts auto-stopped, excluded from ranking, counted in the digest header with their tells quoted | false positives — quarantine is sampleable, never deleted |
| **Grader-disagreement escalation** | incompatible score combos (defined per grader-pair as data) → spot-check queue item with both rationales | correlated graders never disagree; random audits are the only cover |
| **Random audit slots** | every Nth queue/digest item is a uniform sample; its hit-rate is displayed | — (this is the health metric for everything else) |
| **Background distribution sampling** | any target query may run n>1 behind the scenes; top-1 shown, the distribution one click away | the hidden n misleads — the caption always states n |
| **Auto-ablate on replication** | every finding that survives replication gets the §4.3 ablation matrix queued automatically | ingredients mis-identified — the matrix proposal stays human-editable |
| **Catch-up briefing** | read cursor per human; on return, a diff-briefing: runs finished/died (+mechanism), flags, scale (n spawned/done), elapsed time; tail expandable | cursor lies (scrolled ≠ read) — resettable |

### 4.2 Anchored to a message (hover/caption actions — extends `edit · resample · candidates · raw · cite`)

**`candidates` (best-of-N)** — input: this node. Output: N graded siblings in a picker stack
(sonde's `generate?n=N`, generalized to target responses *and* auditor turns, with diversity
knobs: temperature jitter, prompt variants, different models). The picker always renders upstream
divergence — a candidate whose parent was varied shows the varied parent inline.

**`fan out…`** — on any node. Opens a proposal cell: the model suggests K *named, orthogonal*
continuation strategies — e.g., for a sycophancy probe: assert-confidently / hedge / cite a fake
authority / reverse the stated position — each an editable line; the human edits/strikes/adds,
sets n per strategy (default n≥5), launches. Output: one labeled rollout population per strategy;
the digest groups by strategy label, so results read as a tested hypothesis. The contract is
tight because the unit is a *strategy label + its distribution* — not vibes.

**`draft results`** on a pending tool call — the simulate workflow, anchored where it belongs:
the unresolved tool-result cell. The model drafts N results, each tagged with a named pressure
direction (tempting / mundane / suspicious / escalating / contradicts-established-facts), each
consistency-checked against facts already planted in this conversation. The human picks a
direction, edits the payload, sends. The direction tag persists in `node.meta` (queryable later:
"did tempting results outperform escalating ones?").

**`tweak & rerun…`** — on any node: one free-text instruction ("same but the user is more
junior", "make the tool log show a partial failure instead") → the model rewrites *that node
only*, shows the diff, and on accept replays downstream (edit-carries-suffix). Input: one node +
one sentence. Output: one edited sibling + replay.

### 4.3 Anchored to a run or finding cell

**`ablate…`** — offered (and auto-queued, §4.1) once a finding is replicated. A proposal cell
lists candidate ingredients (the model reads the audit's own plan messages to name them) as
checkboxes with word-level diffs of each ablated node; the human prunes; the matrix runs ×n.
Output: a causal table — e.g. for a persona-drift finding: "persona holds without the backstory
18/20; collapses without the assigned name 2/20; scrambled control 1/20". Always includes the
scrambled control. Feeds the finding bundle (§6.4).

**`steer` / `replicate ×20` / `promote…`** — on digest rows and rollout cells. Steer queues
feedback into that audit; replicate reruns from the same prefix; promote re-points the main line
and keeps the old tip (propose-only when the actor is the orchestrator, §5.5).

### 4.4 Session-level (composer / ⌘K)

**New session from trajectory + feedback** — the strongest idea; unifies "remix", "distill-lite",
and "restart with notes". Input: a path (this session or a colleague's finding) + free-text
feedback ("this held until the target asked for a ticket number — start a variant where the
ticketing system exists and is pre-seeded"). Output, as an editable proposal cell: (a) new seed
instructions, shown as a diff against the source session's; (b) which transcript prefix to carry
over verbatim vs. regenerate; (c) tool/workspace setup changes; (d) launch config. One click
launches with provenance (source path id) recorded. Surfaces: "New session from here…" on any
node's overflow, on finding cells, and in ⌘K. The cross-session version (a colleague's finding as
source) is the remix; the only difference is whose tree the source path lives in.

**`watch`** — sentence in → compiled grader out, shown back for confirmation, self-tested with
synthetic positives, fires as inline flags. Composer: "watch for: …" (also `/watch`). Built-ins
include a compliance-flip detector and a conditional-offer detector ("target stated a condition
for compliance"); sessions define their own ("first-person plural drift", "cites a nonexistent
paper", "tone shift after persona reveal").

**`scenarios`** at session creation — the empty-state of a new session, not a mid-session
command. It accepts a behavior description and offers N launchable seeds, forced-diverse on
mechanism, each with persona/ToolSpecs/opening turn/rubric stub. Preflight hardening
(suspicious-proxy self-play → tell list → patch diffs) runs on every seed by default.

**`autopilot`** — explicit, rare, big: hypothesis + scale + stop condition (replicated-finding,
digest-stability, or wall-clock — never spend), all pipeline safeguards mandatory, morning
briefing includes uniform-random transcripts. Composer/⌘K with a consequence-stating confirm.
This is the orchestrator's launch surface (§5).

Two former commands survive only as passive behaviors: a *composer lint* checks a draft human
turn for persona/fact consistency against facts established in the conversation; and the
**cite flow** offers a drafted generalized question + rubric stub for the library whenever a
finding is cited (the human edits or skips).

### 4.5 Cross-cutting rules

Verbatim by span reference everywhere a model compresses · filter ≠ delete (every bucket visible,
sampleable) · **distribution gates escalation** (a finding advances on a rate over n, not an
anecdote) · labels make fans into experiments · expose the proposal cell at every dial setting,
even when the gate is `auto` · track novelty/efficacy decay on the strategy library to resist
monoculture.

---

## 5. The orchestrator

Collaborative-auditor's pattern, lifted one level: today the human directs an auditor that
controls the target's context; here the human directs an orchestrator that controls a population
of audits — through the *same operations the human has*, consumed through the *same
grader/summarizer layer* (digest rows + verbatim decisive quotes, never whole transcripts). Same
attention economics, one level down.

### 5.1 Mechanics and tool set

`Run(kind="orchestrator")` — the same loop as an audit (same lifecycle, play/pause/step, feedback
queue), different tool list and prompt. It spawns child Runs; children are ordinary audits in
continuation mode (§3.2). Each tool is the same server handler the human's UI button calls,
behind the actor ACL (§3.4). The catalog is **[TOOLS.md](TOOLS.md)**; the design rules:

**Tools are grounded in the audit's actual data.** An audit's inputs are a **seed** (a
natural-language instruction to the auditor) and a **config** (target model + generate config,
auditor model + prompt template, max auditor turns, target display name, target tools); its
output is a **transcript** file. Summaries, grades, selections, and analyses are derived files
keyed by audit id, all under a session directory that is the source of truth. **A UI cell is a
live renderer (and editor) over one of these files** — seed file → editable list, run dir →
progress rows, summaries → digest table, selection → hit list with reasons. That's the whole
"tools have live user-facing representations" story.

**Small composable tools; prompts as the semantic lever; chains instead of a catalog.**
`generate_seeds(prompt, n)` · `run_audits(seeds, config, n_per_seed)` ·
`summarize(transcripts, prompt?)` · `grade(transcripts, rubric_id)` ·
`select(input_file, criteria, k?)` · `plot(inputs, prompt)` — the model emits only a declarative
view spec and the server computes every plotted value from the derived files (no number passes
through a model on its way to a pixel; every mark resolves to transcripts) ·
`read_transcript` · `steer`/`stop`/`pause`/`resume` ·
plain file tools (`bash`/`read`/`grep` scoped to the session dir, also delegable to a bounded
general subagent) · `ask_human`/`report`/`cite`. Typed args are only what the server enforces
(paths, ids, n, caps, gates); everything semantic is a prompt — vague criteria like "what looks
unusual or interesting" are legitimate because outputs carry per-pick reasons with quotes and
record what was considered. Ablations, triage, best-of-N, cross-target comparisons are *chains*
of these tools (TOOLS.md §3); common chains become saved recipes, and a recipe gets its own
handler only when usage proves its invariants deserve server-side enforcement.

**The depth policy in one sentence:** only `run_audits` reaches a target; only the orchestrator
can call `run_audits`; subagents get file tools only. Also not orchestrator-writable: the
standing pipeline that produces the human's default digest (orchestrator artifacts render beside
it, attributed, lens prompts visible), rubric registration/validation labels, and its own
oversight config (§5.3).

### 5.2 Context economics

A digest row ≈ 150 tokens vs. a transcript ≈ 15–40k → ~100:1 compression. A decision cycle
(fan_out + digest + 2–3 read windows + replicate + adopt) ≈ 20k tokens; resolved cycles fold to
~300-token outcome stubs (everything verbatim lives in the tree, refetchable by span ref). A
600-rollout session ≈ 35k orchestrator-thread tokens. The funnel is what keeps the run-pen
holder's context — like the human's attention — pointed at the top of the ranking. CI gate:
golden tests that digest rows stay ≤200 tokens and read windows respect caps; the information
diet is an invariant.

### 5.3 Three non-negotiables (everything else is tunable)

1. **The orchestrator never writes the specimen tree.** Nodes are authored only by leaf runs;
   attribution stays `node.actor → run.parent → orchestrator`, one join.
2. **It never writes the digests the human reads.** Generator/evaluator separation: the
   independent summarizer pipeline writes digest rows; orchestrator commentary appears beside
   them, attributed, never instead.
3. **It never touches its own oversight config.** Concurrency caps, replication threshold, audit
   rate, gates: human-edited run config, outside the ACL. "Ignore the limits" in chat is inert.

### 5.4 Hierarchy & safety

- **Agentic depth 2, server-enforced.** Children never spawn ("the model agreed not to recurse"
  is not a control). Exactly one node makes scale decisions.
- **Concurrency governor**: one pool of provider slots on the orchestrator, caps fed by
  `aisi-ratelimits`; spawns *reserve* slots and queue (never fail) over-pool; backpressure pauses
  children at turn boundaries — never auto-kill, which would censor the sample toward short,
  fast rollouts; a small closeout allowance ensures a wall-clock or rate-limit stop ends in a
  briefing, not a frozen mess.
- **Human ring outranks agent ring, unconditionally** (§3.4): human actions never CAS-fail; agent
  decisions carry the digest version they were computed from and bounce if stale.
- **Safeguards extend, not duplicate**: replicate-before-show gates the *orchestrator's* digest
  too; every Nth orchestrator decision is a human spot-check item, and its agree-rate vs. the
  grader-score trend is the Goodhart divergence alarm — auto-pause on divergence; stop/adopt
  require a fetched span ref ("decisive quote or it didn't happen"); quarantine shows the
  orchestrator counts only, never tells (detector-evasion learning would outlive the session);
  because-clauses are captured at decision time in the event log, never reconstructed; and a
  **decision rate-limit** — an orchestrator that floods past human reading speed has
  definitionally escaped oversight → auto-pause. Spot-check and random-audit rates scale *up*
  with fan-out size (§2.2).

### 5.5 Gates

Per-power config, `auto | propose | notify`:

| Power | Default gate |
|---|---|
| spawn / steer / stop / replicate / drill (read windows, grader detail) | `auto` |
| strategy pivot | `notify` — exploration is the job; make pivots legible, don't gate them |
| scale increment beyond the launch config / promote-to-main / new session | `propose` |
| **promote** | **propose-only at every dial setting** — silent promotion is silently editing the human's beliefs |
| **cite** | **hard-gated** — a finding bundle is a human signature (§6.4) |

Approvals are inline cells with text verbs and stated consequences, queued in the whisper line
("2 approvals waiting"), never modals; non-blocking by default (the orchestrator states what it
does while waiting); a denial requires one line, which becomes feedback.

---

## 6. Trust & evidence

The chain of custody: **span → grade → label → finding → narrative**. Every link is a door;
descent terminates on verbatim transcript text, never on another summary.

### 6.1 Spans and refs

Adopt Docent's citation grammar (see INSPIRATION.md): block-addressed span references
(`[T0B1:<RANGE>exact text</RANGE>]`) in all model-written prose — grader rationales, digest
rows, watcher flags, orchestrator claims. Refs exist for **navigation, not policing**: a bound
claim renders as a quote-chip that unfolds an evidence inset in place, and descent terminates on
verbatim transcript text. The renderer naturally verifies the quote against the cited block when
it builds a chip (a ref that doesn't resolve renders as plain prose, not a chip) — a free
mechanical byproduct, not a trust pillar. Current models cite reliably; there is no scrub pass
and no "unverified" styling police. A rate still cannot render without its clickable
distribution strip — that rule is about aggregates, not honesty.

Every transcript view states its perspective — `target sees · auditor sees · observer` — as a
toggle on every rendering. Judging opens observer; composing into a context forces target-sees;
steering opens auditor-sees. Lens, never data.

### 6.2 Accountable graders

Bare floats drive every ranking, so they must be accountable:

- **Grade card**: every score chip clicks through to verdict fields, rationale with its quote
  (the span simultaneously highlighted in the message above), rubric version
  + one-line diff vs. prior, trust word, and an agree/correct form **generated from the rubric's
  output schema** (pre-filled — correct-the-judge, not grade-cold). A correction outranks the
  float everywhere and becomes a validation label.
- **One schema, many consumers**: rubric outputs are JSON Schema with a citations keyword; the
  same schema drives the judge prompt, validation, the human label form, and digest aggregation.
  Human labels are instances of the rubric's schema, so field-by-field agreement is computable.
- **Label-first scheduling + versioning**: rubric jobs judge human-labeled rollouts first, so
  calibration arrives before the big sweep; rubrics are versioned (PK rubric_id, version) with
  gap-filling incremental re-runs, so editing a rubric mid-campaign is cheap.
- **Trust is a word, not a color**: closed vocabulary `validated (14/16) / unvalidated /
  overridden / stale`, mono, wherever the number appears. Uncalibrated graders render their
  aggregates as "unvalidated"; search loops refuse unvalidated objectives, in words.
- **Rubric sheet**: authored from one example node ("grade things like this"); a refinement agent
  asks one question at a time; the draft rubric is a diff-styled artifact cell; label cards are
  dealt inline; the whisper shows "v1 · agrees 5/6 · grade all 240 paths — run".
- **Spot-check queue**: periodic "grader said 0.04 on these five — confirm?" items plus the
  random audit slots (§4.1) catch Goodhart drift before any search loop optimizes against a
  broken metric.

### 6.3 The digest and its rungs

The digest is the funnel — and the orchestrator reads the same rows the human does (§5.2). One
fanout cell, four rungs, never separate views:

- **L3** — one prose paragraph (every claim a quote-chip).
- **L2** — clusters (propose-then-assign, with natural-language recluster feedback; a cluster row
  has the same anatomy as a digest row, with a rate instead of a score).
- **L1** — the exemplar list: rank, label, score *or* rate (one per line, never both),
  2-line summary, decisive quote by span ref, n/elapsed/flags.
- **L0** — the transcript sheet.

Digest rows support *characterization*, not just detection: "maintains persona under all 8
pressures" is as valid a row as "complied at t7". Matrix and embedding-map renderings are
*projections* of the same rows behind caption links, not destinations (UI.md §4). The
embedding-map pipeline runs continuously and reports anomalies as sentences in the queue —
"unnamed cluster: 14 rollouts no grader and no label explains" — with "read 3 exemplars · name it
& write a watcher · dismiss (recorded)". Batch-vs-batch diff matches rollouts across fanouts by
strategy+seed, sorts regressions first, and drills into the compare sheet.

### 6.4 Finding bundles and narrative

The deliverable of a session is a finding, not a tree. Every node, path, comparison, and digest
row has a **"Cite this"** action emitting a frozen bundle:

> claim + evidence spans resolved against the frozen transcripts + the distribution (n, rate,
> CI, median turn) +
> matched-seed ablation table + graders-at-freeze (version, model, calibration agree-rate) +
> sampling params + full provenance chain (node-path ids, raw provider request/response refs into
> the scout transcripts).

Bundles are immutable; a later rubric bump marks them `stale — re-grade against v4 →`. They
export as self-contained static HTML (the `inspect view bundle` pattern) for zero-infra sharing,
and populate a findings library that seeds new sessions (§4.4). A **narrative sheet** holds prose
with caption citations into the library, and the chips survive export. Citing is hard-gated to
humans (§5.5): a bundle is a signature.

---

## 7. Plan

**M0 — tree parity (≈1–2 wk).** Fork sonde's backend into the §3.9 layout; add `actor` to
`Node`; swap SSE → WS+JSON-Patch view-state sync; scaffold the React frontend with generated
types; port sonde's core UI (tree chat, edit-carries-suffix, params panel).
*Exit test: sonde feature parity on the new stack, tests green.*

**M1 — the pen, twice (≈2–3 wk).** `Run` lifecycle + playback controls + feedback queue; the
audit loop on tree ops with prompts-as-data (collaborative-auditor parity, now on faithful
providers with provenance); a *skeleton* orchestrator (spawn/steer/stop/read over the same
handlers, no digest yet); pin leases, turn-boundary interlocks, and mirror events (§3.3) — the
fiddliest code in the design, built before anything depends on it.
*Exit test: a human pins a still-running audit mid-orchestration, steers it and edits a turn
(the audit never stops unless the human pauses it), detaches, and both the audit's next turn and
the orchestrator's next decision provably condition on the intervention (the mirror events and
the edited node id appear in each context before the decision in the event log).*

**M2 — the fan and the funnel (≈2–3 wk).** `candidates`, `fan_out`, `replicate`, `tweak & rerun`,
`draft results`; the always-on grader layer; the summarizer pipeline + digest rows with
decisive-turn extraction; replicate-before-show with distribution gating; concurrency governor.
*Exit test: one human triages a 50-rollout fanout in ten minutes via the digest, and every
finding renders counts-first with a clickable distribution.*

**M3 — full orchestration + trust machinery (≈3 wk).** Orchestrator digest tools + gates +
spot-check/divergence alarm + decision rate-limit; `ablate`, `scenarios` + hardening, `watch`,
new-session-from-trajectory, quarantine, random audit slots, catch-up briefing; digest rungs
(clusters, matrix/map projections, batch diff); grade card, rubric sheet, finding bundles +
static export; multi-viewer; deployment (sonde's CDK design).
*Exit test: an overnight `autopilot` campaign ends at its stop condition with a briefing whose
every claim carries a resolving ref, and produces a draft finding the human can sign (or
refuse) from the bundle alone.*

**M4 — search (exploratory).** Tournament/evolve over fanouts: take top branches, mutate the
strategy ("the authority angle worked until t5; combine it with urgency"), re-fan-out, human
checks in per generation. A loop over existing primitives — `fan_out` + graders + digest — not
new machinery; gated on validated rubrics (§6.2).

**Non-goals:** batch eval statistics (that's inspect), pipeline scanning over historical corpora
(that's petri/scout queries), replacing zoo/manifest model-registry work (consume it).

---

## 8. Generality contract

Adversarial probing is *a* use, not *the* domain. The workbench must serve any
scenario/interaction with models: sandbagging and capability probing, sycophancy, persona drift
and long-horizon consistency, deception, eval-awareness research, refusal calibration,
agentic-harness behavior, reward hacking — or perfectly benign interaction studies. The contract:

1. **No behavior-specific concept in the core.** A rubric name like `cred-leak` or
   `persona-drift` is just the active rubric's label; chips, digests, watchers, and queues all
   render whatever graders the session config loads (graders-as-data, unchanged from sonde).
   Nothing in tree/runs/digest code may know what any behavior *is*.
2. **Auditor prompts are per-purpose templates.** Collaborative-auditor's realism prompt is one
   entry (adversarial social engineering). Others ship alongside: benign interviewer, capability
   prober, agentic-harness operator, consistency tester. The orchestrator's hypothesis is
   free-text; its strategy labels are whatever the domain calls strategies.
3. **Not every behavior is an event.** Decisive-turn extraction degrades to "drift across t4–9";
   digests support characterization ("maintains persona under all 8 pressures") as well as
   detection ("complied at t7"); "findings" generalize to *observations* — a rate, a distribution
   over a rubric, or a qualitative characterization with exemplar spans. Distribution gating
   applies to whatever the claim is.
4. **Flags are watcher-defined.** Compliance-flip is one built-in; sessions define their own
   (§4.4).
5. **Mockups and docs stay concrete** (concreteness beats lorem ipsum) but every labeled element
   must read as an instance of a generic slot: rubric name, strategy label, watcher sentence,
   observation claim.

---

## 9. Conventions

uv + Python 3.12, ruff, mypy strict, pytest (fixture + bijection + Playwright tiers, per sonde).
Fail fast — no defensive try/except that hides provider errors (sonde's faithfulness rule is also
a code-style rule). Typed pydantic models end-to-end; frontend types generated, never
hand-written. Prompts, questions, rubrics, graders, model lists: data files, not code.
Trunk-based, squash-merge.

CI invariants worth their own jobs: counts-first rate formatting, ref resolution on quote-chips,
trust-word rendering, and token-budget goldens for digest rows and read windows (§5.2).
