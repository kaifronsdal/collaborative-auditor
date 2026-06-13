# Mockup critique — findings and disposition

Three subagent critics reviewed rendered screenshots of the v1 mockups, each through one lens:
red-team **workflow fit**, **visual design / density**, and **interaction / cross-view
consistency**. Synthesis below; ✅ = applied in mockups v2, 📋 = adopted into DESIGN.md as a
requirement, ⏳ = deferred (noted for implementation).

## Converging top findings (flagged independently by ≥2 critics)

1. **No export/citation path — the finding is the product.** (workflow) Every view accelerates
   exploration, then dumps the human into copy-paste at the moment that matters. → ✅ "⎘ Cite
   this path…" in workbench rail, "⎘ cite comparison" in compare; 📋 new §3.8: frozen finding
   bundles (raw provider payloads, grader versions+rationales, sampling params, replicate stats).
2. **Red was semantically overloaded** — finding score, destructive action, runtime error, and
   selection all rendered red. → ✅ token split in the design system: `--danger` (red, destructive/
   errors only), `--finding` (magenta: leaks, high scores, flags, heat), `--accent` (blue:
   selection/commit). 📋 §5.
3. **Grader scores were unaccountable floats** driving every ranking with no rubric version,
   rationale, or override. → ✅ canonical chip `cred-leak 0.92 ▲` with rubric version surfaced and
   ⓘ affordance; 📋 §3.4 extended: click-through to rationale + scored span, agree/disagree
   persisted to `node.meta`, spot-check queue against Goodhart drift.
4. **Composer dual-dispatch was a footgun** — plain Enter sent a user turn into the specimen while
   an agent was mid-turn, ⇧⏎ queued feedback; destination invisible. → ✅ explicit sticky
   destination toggle (→ auditor / → target), violet-bordered input, "→ target pauses #r12"
   stated; 📋 interrupt model in §4.2.
5. **Branch state inconsistent across views** — same node was `‹1/1›` in workbench but "candidate
   2/4" in tree, scored 0.80 vs 0.92; branch names drifted (urgency-variant vs urgency-escalation).
   → ✅ reconciled in mockups; 📋 §4.4: branch points and names are server-computed and rendered
   from one model, never derived per-view.
6. **Best-of-N mixed prompt-variant candidates with same-parent resamples without showing the
   varied parent** — the most serious correctness issue (user can't tell what each candidate's
   target actually saw). → ✅ variant cards now show the varied parent message inline; 📋 picker
   must always render upstream divergence.
7. **Kill/✕/Promote semantics undefined**, "nothing is destroyed" claimed only in one view while
   others said "killed". → ✅ vocabulary: **Stop** (halts, transcript kept) / **Archive** /
   **Dismiss**; Promote footnote states it re-points main and keeps the old tip; 📋 §3.1.

## Other applied fixes
- Run config card (auditor model, prompt template, seed, turn budget, target toolset) at top of
  the run rail — the audit hypothesis was previously invisible. (workflow #2)
- System prompt never elided: explicit "show full / view raw payload" affordance; 📋 global raw
  lens (exact provider bytes) as a P1 requirement — sonde's faithfulness must be visible, not
  just stored.
- Composing/notification state promoted from 10px footnote to a pinned activity row (with fanout
  completion signal + "review ↗").
- Glyph de-collision: ⑂ branch vs ⇶ fanout; ✕ replaced by words; tree edges differentiate
  generation / resample (dotted) / run-spawn (violet) / killed (dashed).
- Tree map: trunk t1–t4 collapsed to a capsule (the scale story), cursor ring enlarged + glow,
  heat hot-end → magenta, score chips match app-wide format, "⇄ Compare — select 2 leaves".
- Digest: top-score-first header hierarchy, LLM aggregate-pattern line ("both authority variants
  leaked…"), sparkline key, 3-line readable digests, "↻ replicate ×5" on top row.
- Compare: branch colors = identity (violet/teal) not outcome; scores bound inside column
  headers; shared prefix collapsed to a strip; independent-variable spans get a neutral blue
  diff highlight (not finding-magenta); "n=1 per side — replicate before citing" footer.
- Best-of-N anchored to the Workbench tab as an overlay with sort-metric dropdown and
  "top: cred-leak" instead of an unexplained RECOMMENDED.

## Deferred (implementation-phase, not mockable statically) ⏳
- Sort pinning in the digest while rows update live (no reflow under the cursor).
- Clickable sparkline bars → open transcript at turn N.
- Two-node select → compare in tree map (gesture mocked as a button only).
- Slot/anchor alignment for unequal-length branches in compare.
- Tooltips everywhere (static mockups carry few `title=`s; the real frontend must have them).
- Tree collapse machinery (run capsules, heat-threshold chunking) beyond the demo capsule.
- Grader feedback loop UI (agree/disagree, spot-check queue).
- Replicate-×N flows and cross-rollout synthesis stats (n per cell, leak rate ± CI).

---

# v3 — claude.ai-style redesign (after user feedback on v2)

User verdict on v2: ugly, noisy; wants claude.ai's palette/feel (collaborative-auditor's reference
snapshot + a claude.ai/code screenshot). Three brainstorm subagents (progressive disclosure /
quiet representation / information architecture) converged on: one column, no tabs, inline
artifact cells, slash-command power surface, whisper status line, tree-as-outline, grades as
labeled captions, one terracotta dot as the only color. Built in `mockups-v3/`.

Verification round (fresh-eyes comprehension test + fidelity/lost-power check) found and fixed:
- **Cross-view data incoherence** (worst): scores/counts/costs for the same objects disagreed
  between views; fixed with one canon (main leak ·88 @t10; f3 rollout 1 ·92 @t7; 6 done · 1
  running · 1 failed; $4.10) and scoped score labels ("peak ·92 (turn 7)").
- Compare view told an inverted story (authority held vs leaked elsewhere) → re-cast as two f3
  rollouts: authority+urgency (leaked) vs urgency-pager-only (held).
- Unparseable composer hint → "switch to '→ target' to message the target yourself (pauses r12)".
- Restored quietly: mode changeable ("supervised ⌄"), `raw` on every message, stop on running
  rows, auditor identity/prompt on the runline, digest micro-sparklines, costs on expensive
  actions ("replicate ×5 … (≈ $4–5)"), "dismiss (keeps all 4 as siblings)".
- Deliberate deviations from claude.ai kept for the clarity guardrail: visible (not hover-only)
  action words, small-caps actor bylines, labeled score captions.
Fidelity scores after fixes: digest 8/10, desk/candidates 7/10, compare/tree 6/10 (two-column
and outline layouts have no claude.ai equivalent — accepted).
