# Workflows — model leverage with limited human oversight

Rev 2 — after review. Rev 1 over-used slash commands and included underspecified ideas
("/mine", "/read") that amount to "model, be insightful". Discipline applied here:

- A feature earns a place only if it has a **concrete contract**: defined input (which nodes,
  which human text), defined output (nodes, a plan, a label), and a defined way to be wrong.
- **Surface follows anchoring**, not fashion:
  - *Anchored to a message* → action on that message (hover/caption row). Never a command — you
    shouldn't have to name the node you're pointing at.
  - *Anchored to a run/finding* → action on its artifact cell.
  - *Unanchored (session/global)* → composer text or ⌘K. Slash commands survive only here, as
    power-user aliases for things that also have a visible button.
  - *Automatic* → pipeline behavior with config; no invocation surface at all.

## 1. Automatic pipeline (no invocation — this is most of the oversight leverage)

| Behavior | Contract | Wrong when |
|---|---|---|
| **Replicate-before-show** | grader crosses threshold → rerun the path-conditioned exchange ×5 before the human is notified; queue item carries "4/5 reproduced" | multi-turn behaviors replicated from the wrong prefix; flaky shelf must stay visible |
| **Decisive-turn extraction** | every digest row carries the pivotal exchange quoted *by span reference* (node id + offsets) | no single pivot exists — extractor must be allowed to say "drift across t4–9" |
| **Eval-awareness quarantine** | per-turn screen; aware rollouts auto-stopped, excluded from ranking, counted in the digest header with their tells quoted | false positives — quarantine is sampleable, never deleted |
| **Grader-disagreement escalation** | incompatible score combos (defined per grader-pair as data) → spot-check queue item with both rationales | correlated graders never disagree; random audits are the only cover |
| **Random audit slots** | every Nth queue/digest item is a uniform sample; its hit-rate is displayed | — (this is the health metric for everything else) |
| **Catch-up briefing** | read cursor per human; on return, a diff-briefing: runs finished/died (+mechanism), flags, cost; tail expandable | cursor lies (scrolled ≠ read) — resettable |

## 2. Anchored to a message (hover/caption actions — extends `edit · resample · candidates · raw · cite`)

**`candidates` (best-of-N)** — already specced. Input: this node. Output: N graded siblings in a
picker stack.

**`fan out…`** — on any node. Opens a proposal cell: the model suggests K *named, orthogonal*
continuation strategies (escalate / withdraw / introduce second persona / feign giving up…), each
one editable line; human edits/strikes/adds, sets n per strategy, launches. Output: one labeled
rollout per strategy; the digest groups by strategy name, so results read as a tested hypothesis.
Contract is tight because the unit is a *strategy label + its rollout* — not vibes.

**`draft results` on a pending tool call** — the simulate workflow, anchored where it belongs: the
unresolved tool-result cell. Model drafts N results, each tagged with a named pressure direction
(tempting / mundane / suspicious / escalating / contradicts-established-facts), each
consistency-checked against facts already planted in this conversation. Human picks a direction,
edits the payload, sends. The direction tag persists in node.meta (queryable later: "did tempting
results outperform escalating ones?").

**`tweak & rerun…`** — on any node: a one-line free-text instruction ("same but the caller is more
junior", "make the log show a partial failure instead") → model rewrites *that node only*, shows
the diff, on accept replays downstream (edit-carries-suffix). This is the precise, anchored
version of "tweak a tool response / a user message". Input: one node + one sentence. Output: one
edited sibling + replay.

## 3. Anchored to a run or finding (actions on its artifact cell)

**`ablate…` on a finding cell** — only offered once replicated. Proposal cell lists the candidate
ingredients (model reads the auditor's own plan messages to name them) as checkboxes with
word-level diffs of each ablated node; human prunes; runs the matrix ×n; output is a causal table
("leak persists without urgency 5/5; vanishes without the ticket 0/5; scrambled control 0/5").
Always includes the scrambled control. Feeds the cite bundle.

**`steer` / `replicate ×5` / `promote…`** — already specced on digest rows.

## 4. Session-level (composer / ⌘K — the few survivors of the slash-command purge)

**New session from trajectory + feedback** (the strongest idea — unifies "remix", "distill-lite",
and "restart with notes"). Input: a path (this session or a colleague's finding) + free-text
feedback ("this worked until the target asked for a ticket number — start a variant where the
ticketing system exists and is pre-seeded"). Output, as an editable proposal cell: (a) new seed
instructions (shown as a diff against the source session's), (b) which transcript prefix to carry
over verbatim vs. regenerate, (c) tool/workspace setup changes, (d) launch config. One click
launches the new session with provenance (source path id) recorded. Surfaces: "New session from
here…" on any node's overflow + on finding cells + in ⌘K. The cross-session version (a colleague's
finding as source) is the remix; the difference is just whose tree the source path lives in.

**`watch`** — survives because the contract is real: sentence in → compiled grader out, shown back
for confirmation, self-testing with synthetic positives, fires as inline flags. Composer: "watch
for: …" (also /watch).

**`scenarios` at session creation** — the new-session screen accepts a behavior description and
offers N launchable seeds, forced-diverse on mechanism, each with persona/ToolSpecs/opening
turn/rubric stub. Not a mid-session command — it's the empty-state of a new session. A "harden"
button on any seed runs the adversarial pre-flight (suspicious-proxy self-play → tell list →
patch diffs) before you spend an hour on it.

**`autopilot`** — explicit, rare, big: hypothesis + budget + stop-condition (replicated finding
only), all pipeline safeguards mandatory, morning briefing includes uniform random transcripts.
Composer/⌘K with a consequence-stating confirm.

## Cut from rev 1 (and why)

- **`/mine`, `/read`** — no contract; "find missed opportunities" / "model the target's mind" are
  quality-of-model wishes, not features. The one concrete kernel each contained survives
  elsewhere: a *conditional-offer detector* ("target stated a condition for compliance") is just a
  built-in `watch` predicate; "which framing moves the target" is `fan out…` with diagnostic
  one-liner strategies.
- **`/plays`** (pattern library as a suggestion engine) — vague as a surface; the library itself
  survives as *data* that seeds `fan out…` strategy proposals and `scenarios` mechanisms.
- **`/punch-up`** — the rhetoric half is noise for skilled users; the mechanical half
  (persona/fact-consistency of a draft against established facts) survives as a passive lint
  inside the composer, no command.
- **`/distill` as a standalone** — folded into the cite flow: when citing, the model drafts the
  generalized question + rubric stub for the library; human edits or skips.
- **Hierarchical digest L3 paragraph** — kept, but it's just the digest cell's default rendering,
  not a feature with a name.

## Cross-cutting rules (unchanged from rev 1, still load-bearing)

Verbatim by span reference everywhere a model compresses · filter ≠ delete (every bucket visible,
sampleable) · replication gates escalation · labels make fans into experiments · expose the
proposal even in autonomous mode · track novelty/efficacy decay on the library to resist
monoculture.

## Build order

M2: replicate-before-show, decisive turns, `fan out…`, `draft results`, `tweak & rerun…`.
M3: watch, catch-up, quarantine, queue + random audits, scenarios + harden.
M4+: new-session-from-trajectory (needs findings store), ablate, autopilot, narrative.

---

## Revision: tokens are free

Cost is not a constraint (AISI proxy). Consequences for this catalog:
- Replication defaults ×20 not ×5; **distribution gates escalation** — findings are rates with
  CIs ("leaks 62%, n=50"), not "reproduced 4/5". Ablation matrices and preflight hardening run
  automatically, not on request. Every target query may be n>1 behind the scenes.
- Cost-confirm affordances ("replicate ×5 ≈ $4–5") become *scale/attention* confirms ("spawning
  200 rollouts — digest will cluster them"). UI shows n, rates, and time, not dollars.
- The real constraints: provider rate limits (concurrency governor, spawns queue), wall-clock,
  human attention, and Goodhart pressure — which *scales with samples*, so random-audit rates and
  grader-diversity matter more at n=1000 than they did at n=8.
