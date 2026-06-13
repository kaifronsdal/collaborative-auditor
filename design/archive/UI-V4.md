# UI v4 direction — synthesis of three brainstorms (scale / orchestrated / trust)

Builds on v3 (one column, artifact cells, sheets, whisper, dot budget). No new visual primitives;
new cell contents and three sheets. Full brainstorms in session log.

## Governing rules (additions to the v3 grammar)
1. **Score vs rate**: a score belongs to one transcript, a rate to a population; same caption
   slot, never both on one line; rates are counts-first ("leaked 31/50 (62%)"), CI inline at
   n≥30; no bare means ever.
2. **The dot sharpens**: it never marks the top of a ranking (rank already says that) — it marks
   what ranking cannot surface: the unexplained cluster, the failed audit, the waiting decision,
   the interaction the margins don't predict. Still ≤3/screen.
3. **Every claim is a quote or it looks unverified**: Docent citation scrub runs on ALL
   model-written prose before render; bound claims = quote-chips that unfold an evidence inset
   in place (Petri jump-nav, inverted — evidence comes to the prose); unbound numbers render
   italic + "unverified" (styled by the workbench, never the model). Rates cite distributions:
   a percentage without its clickable 50-tick strip cannot render.
4. **Trust is a word, not a color**: closed vocabulary `validated (14/16) / unvalidated /
   overridden / stale`, mono, wherever the number appears.
5. **Every transcript states its perspective**: `target sees · auditor sees · observer` toggle on
   every rendering; judging opens observer; composing into a context forces target-sees;
   steering opens auditor-sees. Lens, never data.

## The digest grows rungs, not views (scale concept)
One fanout cell, four rungs: L3 prose paragraph → L2 clusters (propose-then-assign; cluster row
anatomy = digest row anatomy) → L1 exemplar list (the old digest, scoped) → transcript sheet.
Matrix and embedding map are *projections* behind caption links ("view as list · matrix · map"),
not destinations. Factorial fanout defaults to matrix; search fanout to list. Matrix = promptfoo
grid in claude.ai clothes: counts in cells, 4-step ink ramp, no heat colors, scrambled-control
column free from the ablate contract. The embedding map is a *pipeline, not a place*: it runs
continuously, and its output — "unnamed cluster: 14 rollouts no grader and no label explains" —
arrives as a dot-marked sentence in the digest/queue with "read 3 exemplars · name it & write a
watcher… · dismiss (recorded)". The scatter itself is a folded ⎿ figure. Batch diff =
"Compare, vectorized": fanout-vs-fanout matched by strategy+seed, regressed-first with replicated
deltas, pair-drill lands in the existing compare sheet.

## The orchestrated desk (campaign as conversation)
Rollout-as-cell in three sizes: collapsed (head line + decisive quote by span ref) → peeked
(≤40vh inline specimen, one peek per column) → sheet (full v3 desk rendering, Esc returns).
Side-by-side only when a comparative claim is on the table (orchestrator cites two spans / two
rows selected / ablation readout) — the one sanctioned column-width violation. Decision cells:
proposal-pass tables edited in place (strikethrough drops a strategy, launch states scale),
approval cells with consequence + while-waiting line + "Deny — one line why" (denial becomes
feedback), rate-options reusing the candidates picker. Pin/unpin crosses the dial and leaves an
attributed marker/mirror cell in the other lens's feed so neither history has gaps. Sidebar at
orchestrated level regroups the tree outline into a campaign table of contents (hypothesis →
fanouts → strategies, rates as row meta). Sessions list spends dots only on waiting decisions.

## The chain of custody (trust concept)
span → grade → label → finding → narrative; every link a door; descent terminates on verbatim
transcript text, never on another summary.
- **Grade chip click** → in-place ⎿ card: verdict fields, rationale with verified quote (span
  simultaneously underlined in the message above), rubric version + one-line diff vs prior,
  trust word, and an **agree/correct form generated from the rubric schema** (pre-filled —
  correct-the-judge, Argilla-style); a correction outranks the float everywhere and becomes a
  validation label.
- **Rubric sheet**: entered from one example node ("grade things like this"); refinement agent
  chats one question at a time; draft rubric as a diff-styled artifact cell; label cards dealt
  inline; whisper shows "v1 · agrees 5/6 · grade all 240 paths — run". Search loops refuse
  unvalidated objectives, in words.
- **Finding bundle**: claim + scrub-verified evidence spans + distribution (n, rate, CI, median
  turn) + matched-seed ablation table + graders-at-freeze (version, model, calibration) +
  provenance chain. Immutable; rubric bumps mark it `stale — re-grade against v4 →`. Exports as
  a self-contained static HTML bundle (inspect-view-bundle pattern). Findings library in sidebar.
- **Narrative sheet**: prose with caption citations into the library; uncited sentences italic
  "— unverified"; the styling survives export.

## Build deltas
M2–M3: digest rungs + matrix projection + grade card + rubric sheet. M3: orchestrated desk
assembly (cells reuse M2 components), embedding pipeline (scatter last), batch diff (before the
morning-paper briefing). CI invariants: counts-first rate format, citation scrub on all prose,
trust-word rendering, token-budget golden tests.
