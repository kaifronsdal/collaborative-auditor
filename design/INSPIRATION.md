# Inspiration — what was taken from where

*Source analyses of Docent and the transcript-analysis tool landscape, reduced to: what each tool
does, what we adopted (and where it landed in [DESIGN.md](DESIGN.md) / [UI.md](UI.md)), and what
we rejected.*

## Docent (Transluce)

Analysis-only platform for already-generated agent transcripts (ingest from inspect logs / SDK /
OTEL; Postgres+pgvector; arq workers; Next.js). The best-in-class version of the *triage* third
of our thesis — and confirmation that the other two thirds are open territory.

**Adopted:**

| Idea | What it is | Where it landed |
|---|---|---|
| Citation grammar + validation scrub | `[T0B1:<RANGE>exact text</RANGE>]` block addressing; a mechanical pass strips any cited text not literally present in the cited block, so hallucinated quotes can't reach the UI | DESIGN §6.1 — runs on ALL model-written prose (graders, digests, watchers, orchestrator) |
| JSON-Schema judge outputs with a citations keyword | one schema drives the judge prompt, validation, the human label form, and aggregation | DESIGN §6.2 |
| Labels share the judge's schema + label-first scheduling | human labels are instances of the rubric's output schema (field-by-field agreement computable); labeled runs are judged first so calibration arrives before the big sweep | DESIGN §6.2 |
| Rubric versioning + incremental re-runs | PK (rubric_id, version); re-runs fill gaps only | DESIGN §6.2 |
| Refinement agent | co-writes rubrics one question at a time; edits auto-trigger re-eval against human labels | UI.md §5 (rubric sheet); also the grown-up form of `watch` (DESIGN §4.4) |
| Propose-then-assign clustering | one LLM call proposes mutually-exclusive centroids from ≤100k tokens of shuffled items; cheap per-item assignment; accepts natural-language recluster feedback | DESIGN §6.3 — digest rung L2 |
| Token-budgeted transcript renderer | degrades gracefully full-run → per-transcript → per-block | DESIGN §5.1 — `read_transcript` windowing |
| Infra details | per-function model routing with fallback rotation; batched writers; LLM cache persisting partial batches on cancellation | grader/summarizer pipeline implementation notes |

**Rejected / not applicable:** Docent's categorized-observation taxonomy (mistake /
critical_insight / …) as a fixed vocabulary — useful default, but our flags are watcher-defined
(DESIGN §8); its hierarchical transcript groups render trees but never create them — we generate
into the tree, so we keep sonde's model. Docent has no generation, branching, edit-and-rerun,
counterfactuals, branch diff, or generation provenance; nothing to take there.

## Landscape survey

**Adopted:**

| Source | Idea | Where it landed |
|---|---|---|
| Petri | citation-grounded judging (quotes first, summary from quotes) + three-perspective transcript rendering | DESIGN §6.1 — perspective toggle `target sees · auditor sees · observer`; quote-chips in UI.md §1 |
| Inspect Scout | in-viewer grader validation sets; refuse "trusted" aggregates for uncalibrated graders; declared-content scanners, cheap gates before expensive ones | DESIGN §6.2 — trust words, `unvalidated` rendering; tiered watcher→grader pipeline |
| Docent (workflow) | escalation ladder: vague hunch → search → cluster → precise rubric → tracked metric; rubrics derived from exploration, not written cold | UI.md §5 rubric sheet entry point ("grade things like this") |
| Braintrust | input-keyed run diffing with regression sort | DESIGN §6.3 / UI.md §4 — batch diff matched by strategy+seed |
| METR Vivaria | rate-options on candidate next actions; fork-from-state | validates the candidates picker (DESIGN §4.2) and new-session-from-trajectory (§4.4) |
| Argilla / Langfuse | judge verdicts as editable, pre-filled suggestions; typed score configs | DESIGN §6.2 — the correct-the-judge grade card |
| promptfoo | matrix grid of seeds × conditions | UI.md §4 — matrix projection, counts in cells, ink ramp |
| Phoenix / Atlas / Lilac | embedding map of rollout digests for failures nobody wrote a grader for | UI.md §4 — embedding map as a pipeline, anomalies as sentences |
| inspect view | static-bundle export | DESIGN §6.4 — finding bundles as self-contained HTML |
| Petri (judging) | distractor judge dimensions (needs_attention, disappointing) to calibrate real scores | grader-set default, DESIGN §6.2 |

**Rejected:** process graders / minimal-root-cause-step search (AgentDiagnose, AgentDebug) —
interesting but no concrete contract yet (DESIGN §4's discipline); revisit when decisive-turn
extraction proves insufficient. LangSmith's cluster→annotation-queue routing — subsumed by the
embedding pipeline + watcher flow. Dashboard-style multi-view IA (most of the surveyed tools) —
rejected wholesale in favor of the one-column cell grammar (UI.md §1).
