# audit-workbench-design

Design proposal synthesizing [kaifronsdal/collaborative-auditor](https://github.com/kaifronsdal/collaborative-auditor)
and `sonde-alpha-may-2026` into one successor red-team workbench: one conversation tree, pens
held by humans or Runs, an orchestrator that fans out populations of audits, and a digest funnel
that keeps it all readable.

- `DESIGN.md` — the single source of truth: the converged shape, repo contributions,
  architecture, workflow catalog, the orchestrator, trust & evidence, plan (M0–M4), generality
  contract.
- `TOOLS.md` — the orchestrator's tools, grounded in the audit's actual data (seeds, configs,
  transcript files; summaries/grades/selections as derived files; cells as renderers over
  files): a small composable tool set, chains instead of a catalog, and the few rails that
  matter.
- `UI.md` — the visual grammar (cells, sheets, whisper, dot) and the surfaces (desk, orchestrated
  desk, digest rungs, grade card, finding bundle).
- `STREAMING.md` — how tokens reach the UI as the model generates: inspect's event-mutation
  path (mutate the pending `ModelEvent.output`, call `_event_updated`); how it composes with
  `tape.replayable`; ts-mono evaluated as reference-not-dependency.
- `resampling.md` — the record/replay formalism (`Tape`/`Step`/`Node`, `nondet(c,k)`, n-level
  composition) as landed in petri PR #110; `AuditTape` persistence; the `resample` task and
  `tape_from_messages`.
- `RESAMPLING-REVIEW.md` — the red-team review of resampling.md. Findings addressed in PR #110;
  kept as the review record.
- `TREE-IMPL.md` — *(superseded by resampling.md + PR #110; kept as archaeology.)* Early
  implementation recon for the tree mechanics: collaborative-auditor as-built, the invariant,
  what pre-PR-#110 petri carried.
- `ARCHITECTURE.md` — *(superseded by PR #110 + DESIGN.md §3.1; kept as the reasoning that led
  there.)* The store/petri-seam/fork-mechanics brainstorm whose `Effect`/`AuditorTurn` data
  model became petri's `Tape`/`Step`/`Node`. Q2 (embed in-process; `.eval` as interop), Q3
  (fork mechanics walked step by step), and the five-hardest-problems list remain useful
  background.
- `INSPIRATION.md` — Docent + tool-landscape analysis, reduced to adopted ideas (with landing
  spots) and rejections.
- `mockups-v4/` — **current** mockups: the desk (manual/supervised, one audit), orchestrated
  desk, digest at n=500, grade card, finding bundle, analysis views (paired slope / replicate
  grid / survival / contact sheet). Open directly in a browser; no build step.
- `mockups/` (v2 dashboard) and `mockups-v3/` (first claude.ai-style pass) — historical, kept for
  comparison.
- `screenshots/` — rendered at 1600×1000; `screenshot.py` re-renders
  (`uv run --with playwright python screenshot.py`).
- `archive/` — superseded process records (workflow/orchestrator/UI drafts, mockup critiques),
  folded into `DESIGN.md` and `UI.md`.
