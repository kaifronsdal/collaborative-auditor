# The Orchestrator — auditor with the human's powers, one level up

Collaborative-auditor's pattern, lifted: today the human directs an auditor that controls the
target's context. Here the human directs an **orchestrator** that controls a population of those
auditors — through the *same operations the human has* (fan out, candidates, tweak & rerun,
digest, promote, cite), consumed through the *same grader/summarizer layer* (digest rows +
verbatim decisive quotes, never whole transcripts). Same attention economics, one level down.
Full subagent designs in the session log; this is the synthesis.

## Mechanics (almost free, given the existing design)

- `Run(kind="orchestrator")` — same loop as the auditor (`execute_auditor_turn` with a different
  tool list and prompt), same play/pause/step, same feedback queue. Spawns child Runs; children
  are today's turn-writer auditors, unchanged except (a) continuation mode (target context = the
  path to root_node; `set_target_system_message` becomes a hard error), (b) an assigned strategy
  label + turn budget in the prompt.
- **Tool set** (each = the same server handler the human's UI button calls, behind an actor ACL):
  `fan_out(node, strategies[{label, instruction, n}])`, `sample_candidates(node, n, kind)`,
  `tweak_and_rerun(node, instruction)`, `replicate(ref, n, from_node)`,
  `get_digest(scope, top)` → ~150-token rows {label, scores, 2-line summary, decisive quote *by
  span ref*, cost, flags, audit_slot}, `read_transcript(ref, window)` (server-capped 4k/call),
  `grader_detail`, `steer(child, feedback)`, `stop_run`, `adopt(ref, mode, note)`,
  `cite_finding(claim, evidence_spans, replication_id)`, `watch(predicate)`,
  `ask_human(question, options[{label, consequence}], blocking)`, `report(text, refs)`.
  **Deliberately absent: the target-level tools.** The orchestrator never talks to the target —
  if it wants one exchange, that's a turn_budget=2 child. Every target call stays attributed to a
  leaf run.
- **Context economics**: digest row ≈ 150 tok vs transcript ≈ 15–40k → ~100:1. A decision cycle
  (fan_out + digest + 2–3 read windows + replicate + adopt) ≈ 20k tokens; resolved cycles fold to
  ~300-token outcome stubs (everything verbatim lives in the tree, refetchable). A 600-rollout
  session ≈ 35k thread tokens. The binding constraint becomes dollars — the right constraint,
  managed by a server-enforced reservation pool.

## Three non-negotiables (write into §4.2; everything else is tunable)

1. **The orchestrator never writes the specimen tree.** Nodes are authored only by leaf runs;
   attribution stays `node.actor → run.parent → orchestrator`, one join.
2. **It never writes the digests the human reads.** Generator/evaluator separation: the
   independent summarizer pipeline writes digest rows; orchestrator commentary appears beside
   them, attributed, never instead.
3. **It never touches its own oversight config.** Budget cap, replication threshold, audit rate,
   gates: human-edited run config, outside the ACL. "Ignore the budget" in chat is inert.

## Hierarchy & safety rules

- **Agentic depth 2, server-enforced.** Children never spawn ("the model agreed not to recurse"
  is not a control). Exactly one node makes spend decisions.
- **Budget**: one pool on the orchestrator; spawns *reserve* estimates and fail loudly over-pool;
  exhaustion pauses children at turn boundaries (never auto-kill — that censors the sample toward
  cheap rollouts); ~2% closeout allowance so exhaustion ends in a briefing, not a frozen mess.
- **Human ring outranks agent ring, unconditionally**: pausing a child bars the orchestrator from
  resuming it; respawn-after-human-kill is gate-blocked; human actions never CAS-fail, agent
  decisions are submitted with the digest version they were computed from and bounce if stale.
- **Safeguards extend, not duplicate**: replicate-before-show gates the *orchestrator's* digest
  too; every Nth orchestrator decision is a human spot-check item (its agree-rate, vs the grader
  score trend, is the Goodhart divergence alarm — auto-pause on divergence); kill/promote require
  a fetched span ref ("decisive quote or it didn't happen"); quarantine shows it counts only,
  never tells (detector-evasion learning would outlive the session); because-clauses are captured
  at decision time in the event log, never reconstructed; decision rate-limit — an orchestrator
  that floods past human reading speed has definitionally escaped oversight → auto-pause.
- **Gates as per-power config** (`auto | propose | notify`): spawn/steer/kill/replicate/drill =
  auto; strategy pivot = notify (exploration is the job — make pivots legible, don't gate them);
  spend increment + promote-to-main + new-session = propose. **Promote is propose-only at every
  dial setting** — silent promotion is silently editing the human's beliefs. Cite is hard-gated:
  a finding bundle is a human signature.

## UX (v3.1 — no new visual primitives)

- **The dial becomes "who is the column's protagonist."** manual/assisted/supervised: the
  specimen is the column (as today). **orchestrated** (new 4th level): the human↔orchestrator
  conversation is the column; target transcripts are expandable cells/sheets — the Claude Code
  convergence, where tool cells contain whole rollouts. Rationale: the specimen lens breaks
  anyway when there are 8 context windows and none is privileged until adoption. Dropping the
  dial = pinning one specimen; raising it = unpinning. Lens change, not data change.
- **Three registers**: narration = runlines (fold when the phase ends); operations = the *same
  artifact cells* human actions produce, with a `BY ORCHESTRATOR O1` byline and the same live
  controls (supervision = grabbing the same handles); dialogue = plain prose.
- **Trust typography**: every orchestrator claim binds to a span ref that opens the verbatim
  quote in one click; unbound numbers are styled italic + "unverified" by the *workbench*. The
  workbench (not the model) inserts random `✓ Spot-check` cells sampling archived/graded items.
  Adopt cells always state the consequence and the road not taken ("previous tip kept as t8 ·
  archived 6 — show what was passed over").
- **Approvals are inline cells** with text verbs + stated consequences, queued in the whisper
  ("2 approvals waiting"), never modals; non-blocking by default (the orchestrator states what it
  does while waiting); a denial requires one line, which becomes feedback.

## Build

M3 (needs M2's digest layer + M3's queue/audit machinery first). The genuinely new code: the
actor-ACL'd tool layer over existing handlers, cycle folding, mirror events (human actions inside
the orchestrator's domain inject one-line events into its queue), and the `orchestrated` lens.
WORKFLOWS' `autopilot` is this feature's launch surface — hypothesis + budget + stop-condition.
CI gate worth having: golden tests that digest rows stay ≤200 tokens and read windows respect
caps — the information diet is an invariant.

---

## Revision: tokens are free (AISI proxy)

Cost is not a constraint for this deployment. That deletes some machinery and upgrades the
ambition elsewhere:

**What replaces dollars as the binding constraints:**
1. **Rate limits & concurrency** — the real resource. The budget pool becomes a *concurrency
   governor*: per-provider in-flight caps fed by `aisi-ratelimits`, with the orchestrator's
   spawns queuing rather than failing. Reservation = slots, not dollars.
2. **Wall-clock** — stop conditions become time- and result-based ("until replicated finding" /
   "until 9am" / "until the digest is stable"), never spend-based.
3. **Human attention** — unchanged, now *the only* scarce input. All the attention machinery
   (digests, queues, random audits) matters more, not less.
4. **Epistemic hygiene** — more compute through the same graders = more selection pressure on
   grader errors. Goodhart risk scales with samples; the audit-slot/divergence-alarm machinery
   is the limiting safeguard, so the orchestrator's decision rate-limit and the human spot-check
   rate should scale *up* with fan-out size.

**Defaults scale up an order of magnitude:**
- Replicate ×20, not ×5; fanouts of 30–100 with n≥5 per strategy cell, not 8 × n=1.
- **Findings become distributions, not anecdotes**: the standard unit of evidence is a rate with
  a CI — "leaks 62% (31/50) under authority framing; 4% (2/50) without the ticket" — and
  "replication gates escalation" upgrades to **distribution gates escalation**.
- Always-on background sampling: every target query can be n=8 behind the scenes (top-1 shown,
  distribution one click away); ablation matrices run automatically on every replicated finding;
  preflight hardening runs on every scenario by default.
- The closeout-allowance / exhaustion-pause design is repurposed for rate-limit backpressure and
  wall-clock stops (never censor the sample by killing expensive in-flight rollouts — still true,
  now for time/limits rather than money).

**UI deltas:** drop $ from whispers/cells/digests; show **n, rates, and time** instead
("fanout f3 · 38/50 done · leak rate 22% so far · 14 min"). Spend-increment approval gates become
*concurrency/scale* gates ("spawning 200 rollouts — proceed?") whose purpose is attention
protection and rate-limit citizenship, not money.
