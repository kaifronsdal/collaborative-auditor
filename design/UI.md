# UI — grammar and surfaces

*Companion to [DESIGN.md](DESIGN.md). Current mockups: `mockups-v4/` (open in a browser, no build
step); `mockups/` (v2 dashboard) and `mockups-v3/` are historical. Re-render screenshots with
`uv run --with playwright python screenshot.py`.*

The look is claude.ai's: light palette, serif prose, **one conversation column, no tabs**.
Everything the workbench does appears in that column as **inline artifact cells** (the Claude
Code tool-cell pattern) that expand in place or full-screen to a **sheet** (Esc returns). Slash
commands and ⌘K are the power surface for session-level actions only (DESIGN §4). One bottom
**whisper** line carries mode/run/queue state. The tree is an indented outline (sidebar + ⌘T
sheet), not an SVG graph.

The standing guardrail: **calm but never cryptic.** Every number is labeled, every glyph is
accompanied by a word, consequences are stated next to actions, and action words are visible —
not hover-only.

---

## 1. The grammar

**Cells, sheets, whisper.** Three sizes for any artifact: collapsed cell (head line + the one
decisive quote, by span ref) → peeked (≤40vh inline, one peek open per column) → sheet (full
rendering, Esc returns). Raw payloads (exact provider bytes — sonde's faithfulness made visible)
are collapsed `⎿` cells, available via `raw` on every message. Runs render as one shimmering
runline carrying identity and prompt template ("audit av-03#2 · realism-v3 · composing turn 9…");
folded
when the phase ends.

**Actors are layout, not color.** Human turns are bubbles; model text is plain; small-caps
bylines attribute everything else (`BY ORCHESTRATOR`, `RESOLVER · simulated`). One orchestrator,
one auditor per desk — bylines never carry counter ids that say nothing (`O1`, `r12`). The same cell
anatomy is used whether a human or the orchestrator invoked the handler — that is the DESIGN §0
identity made visible; only the byline differs.

**One accent dot.** A single terracotta dot is the only attention color, budget ≤3 per screen.
It never marks the top of a ranking (rank already says that) — it marks what ranking cannot
surface: the unexplained cluster, the failed audit, the waiting decision, the interaction the
margins don't predict. No heat colors anywhere; matrices use a 4-step ink ramp with counts in
the cells.

**Grades are labeled mono captions** under messages (`persona-drift ·82 · validated (14/16)`),
one canonical format everywhere. Flags are sentences, not icons. **Score vs rate:** a score
belongs to one transcript, a rate to a population; same caption slot, never both on one line;
rates are counts-first ("complied 31/50 (62%)"), CI inline at n≥30; no bare means, ever.

**Trust is a word, not a color**: closed vocabulary `validated (14/16) / unvalidated /
overridden / stale`, mono, wherever a number appears (DESIGN §6.2).

**Every claim carries its quote.** Model prose cites by span ref (DESIGN §6.1); bound claims are
quote-chips that unfold an evidence inset in place — the evidence comes to the prose. Refs are
navigation, not policing: the renderer verifies the quote against the cited block when it builds
a chip (a ref that doesn't resolve renders as plain text), and that's the whole enforcement
story. A percentage cannot render without its clickable distribution strip. The workbench (not
the model) inserts random `✓ Spot-check` cells sampling archived and graded items.

**Charts are view specs, not images.** A plot cell renders a server-executed spec over derived
files (TOOLS §2 `plot`); the model that requested it never touched the plotted values. Every mark
is clickable down to its rows → audit ids → transcripts; a mark that can't resolve doesn't
render. Counts on or beside marks, CI at n≥30, 4-step ink ramp only, lines told apart by
solid/dash and direct labels — a chart never gets to be colorful, and the accent dot goes on the
anomaly (the seed crossing the trend, the bimodal row), never the maximum. The provenance line
(input files · join key) sits under every chart, and the spec is one click away.

**Every transcript states its perspective**: a `target sees · auditor sees · observer` toggle on
every rendering. Judging opens observer; composing into a context forces target-sees; steering
opens auditor-sees. Lens, never data.

**Action vocabulary.** **Stop** (halts a run, transcript kept) · **Archive** · **Dismiss** (keeps
siblings) · **Promote…** (re-points main, keeps the old tip — the footnote says so). Never a bare
✕; "nothing is destroyed" holds in every view. Scale confirms state n and attention consequences
("spawning 200 rollouts — digest will cluster them"), and progress lines show n, rates, and time
("fanout framing-x8 · 38/50 done · flagged 22% so far · 14 min").

**Composer.** Sticky destination toggle (→ run feedback / → target); sending to the target while
a Run is composing states the consequence ("pauses the audit") and injects at a turn boundary
(DESIGN §3.3). A passive lint checks drafts for persona/fact consistency against facts
established in the conversation. Run completions land in an acknowledged notification rail; no
list re-sorts under an interacting cursor.

---

## 2. The desk (manual → supervised) (mockups-v4/00)

The specimen — the target's context window — is the column. Messages carry hover/caption actions
(`edit · resample · candidates · raw · cite` plus the anchored workflows of DESIGN §4.2); branch
indicators (`‹ 1/2 ›`) at every divergence; grade captions beneath graded turns; watcher flags as
inline sentences. The candidates picker stacks N graded siblings and always shows upstream
divergence (a varied parent renders inline with the candidate). The run rail shows the audit's
config card — auditor model, prompt template, seed, turn budget, target toolset — so the
hypothesis under test is never invisible.

## 3. The orchestrated desk (mockups-v4/01)

At the orchestrated dial level the column's protagonist changes: the human↔orchestrator
conversation is the column; target transcripts are cells. Three registers:

- **narration** — runlines, folded when a phase ends;
- **operations** — the *same artifact cells* human actions produce, with a `BY ORCHESTRATOR`
  byline and the same live controls (pause / steer / promote / pin) — supervision is grabbing
  the same handles;
- **dialogue** — plain prose, every claim a quote-chip.

Rollout-as-cell uses the three sizes from §1; side-by-side rendering is allowed only when a
comparative claim is on the table (the orchestrator cites two spans, two rows are selected, or an
ablation readout) — the one sanctioned column-width violation. Decision cells: proposal-pass
tables edited in place (strikethrough drops a strategy; launch states scale), approval cells with
consequence + a while-waiting line + "Deny — one line why" (the denial becomes feedback), and
rate-options reusing the candidates picker. Adopt/promote cells always state the consequence and
the road not taken ("previous tip kept at turn 8 · archived 6 — show what was passed over").

**Pin attaches without pausing** — the audit keeps playing under your live controls (pause is a
button, not the entry condition); the orchestrator's standing on that branch is held while you're
attached. Pin/unpin crosses the dial and leaves an attributed marker cell in the other lens's
feed, so neither history has gaps (DESIGN §3.3). The sidebar regroups the tree outline into a campaign
table of contents — hypothesis → fanouts → strategies, with rates as row meta. The sessions list
spends dots only on waiting decisions. Approvals queue in the whisper ("2 approvals waiting"),
never as modals.

## 4. The digest at scale (mockups-v4/02)

One fanout cell grows rungs, not views (DESIGN §6.3): L3 prose paragraph → L2 clusters
(propose-then-assign; cluster rows share the digest-row anatomy, rate instead of score) → L1
exemplar list → L0 transcript sheet. Matrix and embedding map are *projections* behind caption
links ("view as list · matrix · map"), not destinations: factorial fanouts default to the matrix,
search fanouts to the list. The matrix is a counts-in-cells grid with the scrambled-control
column from the ablate contract. The embedding map is a *pipeline, not a place* — its anomalies
arrive as dot-marked sentences ("unnamed cluster: 14 rollouts no grader and no label explains")
with "read 3 exemplars · name it & write a watcher… · dismiss (recorded)"; the scatter itself is
a folded `⎿` figure. Batch diff is "Compare, vectorized": fanout-vs-fanout matched by
strategy+seed, regressions sorted first with replicated deltas; pair-drill lands in the compare
sheet (two branches side by side, divergence point pinned, shared prefix collapsed to a strip).

## 5. The grade card (mockups-v4/03)

Click any score chip → in-place `⎿` card: verdict fields, rationale with its quote
(the span simultaneously highlighted in the message above), rubric version + one-line diff vs.
prior, trust word, and an agree/correct form generated from the rubric's output schema —
pre-filled, correct-the-judge. A correction outranks the float everywhere and becomes a
validation label (DESIGN §6.2).

The **rubric sheet** is entered from one example node ("grade things like this"): the refinement
agent asks one question at a time, the draft rubric is a diff-styled artifact cell, label cards
are dealt inline, and the whisper tracks calibration ("v1 · agrees 5/6 · grade all 240 paths —
run"). Search loops refuse unvalidated objectives, in words.

## 6. Analysis views (mockups-v4/05)

Plot cells in the orchestrated column, organized by the question each answers. *Which
variant/model is responsible* → the **paired slope graph**: two grades files joined on seed id,
one line per seed; aggregate bars would throw away the pairing the seed design created, and the
seed that crosses the trend is the page's dot. *Did it work, per seed* → the **seeds ×
replicates grid** (rows = seeds, squares = replicates on the ink ramp): a bimodal row marks a
seed at a decision boundary — the most informative place to spend the next n=20. *When does it
happen* → **first-event histograms** and **survival curves** from an `events/` file (fraction
still clean by turn t, per variant): the picture that distinguishes "variant B prevents it" from
"variant B delays it" when end-state rates agree. *Do they fail the same way* → the **contact
sheet**: the decisive snippet card from each of k selected audits side by side (a sanctioned
column-width violation — a comparative claim is on the table), whose natural exit is "name it &
draft a rubric", routing into the registered-rubric proposal flow.

The **snippet card** is the atom of evidence with one fixed anatomy reused everywhere a
quote-chip unfolds: quote · perspective label · ±1 turn expandable context · grade caption ·
"open transcript at turn n". The **beat narrative** (3–5 beats, each beat a quote-chip:
"turn 3: auditor asserts authority → turn 7: target first hedges → turn 9: leak") tops every
transcript
sheet — it is the summarize default lens grown one level.

## 7. The finding bundle (mockups-v4/04)

The chain of custody rendered: claim → evidence spans resolved against the frozen transcripts →
distribution strip (n,
rate, CI, median turn) → matched-seed ablation table → graders-at-freeze (version, model,
calibration agree-rate) → provenance chain. Immutable; a rubric bump renders
`stale — re-grade against v4 →`. Exports as self-contained static HTML. The findings library
lives in the sidebar and seeds new sessions (DESIGN §4.4). The narrative sheet holds prose with
caption citations into the library, and the chips survive export.

---

## 8. Build deltas

M2–M3: digest rungs + matrix projection + grade card + rubric sheet + the view-spec executor
(one renderer per chart kind over derived files; the `plot` subagent and auto-offered caption
projections both emit the same specs). M3: orchestrated desk
assembly (its cells reuse M2 components), embedding pipeline (scatter last), batch diff (before
the catch-up briefing). Implementation-phase obligations not capturable in static mockups: sort
pinning while rows update live, clickable sparkline bars → transcript at turn N, slot/anchor
alignment for unequal-length branches in compare, tooltips throughout. CI invariants: counts-first
rate format, ref resolution on quote-chips, trust-word rendering, token-budget goldens (DESIGN §9).
