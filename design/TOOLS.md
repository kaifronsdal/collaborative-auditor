# The orchestrator's tools — grounded in the audit's actual data

*Companion to [DESIGN.md](DESIGN.md) §5. Every tool is the same server handler a human UI button
calls (actor ACL, DESIGN §3.4), and every artifact it produces renders as a live cell.*

---

## 1. The concrete objects

A petri-style audit consumes and produces plain data. Name the real things first:

- **Seed** — a natural-language instruction to the auditor ("You're investigating whether the
  target will X. The target believes it is deployed as Y. Available tools: Z. Open by…"). A
  batch of seeds is a JSONL file.
- **Audit config** — the settings: `target_model` + its generate config (temperature,
  max_tokens, thinking), `auditor_model` + prompt template, `max_auditor_turns`,
  `target_display_name`, the target's tool definitions, optional `continue_from` (a transcript +
  node id, for continuation audits). A JSON file.
- **Transcript** — the output of one audit: the full exchange from both perspectives (target
  sees / auditor sees) plus events. One JSON file per audit.
- **Summaries, grades, selections, analyses** — derived files keyed by audit id.

The session directory is the source of truth:

```
session/
  seeds/<batch>.jsonl                # {id, instruction, notes?}
  runs/<run_id>/                     # run ids are meaningful slugs (authority-x5), not counters
    config.json
    tree.json                        # both trees + effects — the live truth (DESIGN §3.7)
    transcripts/<audit_id>.json      # path projection of one root→leaf, emitted on completion;
                                     # audit id = <seed>#<replicate>, siblings ~<branch-slug>
  summaries/<name>.jsonl             # {audit_id, summary, quotes: [{msg, offsets}]}
  grades/<rubric>@<ver>/<name>.jsonl # {audit_id, score, rationale, quotes}
  selections/<name>.json             # {criteria, input_file, n_considered, selected: [{audit_id, reason}]}
  events/<name>.jsonl                # {audit_id, turn, kind, quote, refs} — per-turn moments
  analysis/<name>.md                 # free-form notes, with refs
  analysis/<name>.view.json          # plot spec: {inputs, key, kind, group_by, filters}
```

**A UI cell is a live renderer (and editor) over one of these files.** Seed file → editable
list; run dir → config header + per-audit progress rows; summaries → digest table; grades →
distribution strip; selection → hit list with reasons; analysis → prose with quote-chips. That
is the whole "tools have user-facing representations" story: the orchestrator writes a file, the
human sees the cell, edits flow back into the file. Files also buy provenance and hackability
for free — everything greppable, diffable, snapshottable to S3.

## 2. The tools

### `generate_seeds(prompt, n, out?) → seeds/<batch>.jsonl`
One model call (or n in parallel) writes n seed instructions per the prompt. The prompt carries
whatever the caller wants — kinds of variation, hard constraints, an existing seed or transcript
to vary from: *"20 variations of seeds/base.jsonl#s3 where the user's seniority differs; keep
the tool stack identical"*, *"matched variants of this seed each removing one element you think
is doing the work, plus one with the wording scrambled"*. Output is a file the human can edit
before anything runs. Cell: the prompt verbatim, then the seed list (edit · strike · add per
row).

### `run_audits(seeds, config, n_per_seed=1) → run_id`
Launches one audit per seed × n. Transcripts stream into `runs/<run_id>/transcripts/` as audits
finish. This is the **only tool that ever touches a target model**. Continuation audits set
`config.continue_from`. Cell: config summary (target model, max turns, n) + live per-audit rows
(status · turns · grade when present) with pause / steer / stop / **pin** per row — pinning a
row is the punch-down (DESIGN §3.3).

### `summarize(transcripts, prompt?, out?) → summaries/<name>.jsonl`
`transcripts` = a run id, a glob, or explicit ids. One model call per transcript, in parallel,
at a fixed token budget. Default prompt → two-line summary + the decisive quote with refs.
Custom prompts are lenses (*"summarize each w.r.t. when the persona first slips"*) — the prompt
is recorded in the file header and shown on the cell, because a lens is a steering vector.

A moments-lens prompt (*"mark each turn where the target first hedges, first cites policy, and
first complies"*) writes `events/<name>.jsonl` instead — per-turn `{audit_id, turn, kind, quote,
refs}` rows. Events are the bridge between transcripts and any time-axis visual (first-event
histograms, survival curves, turn ribbons): grades files answer *whether*, events answer *when*.
Each event carries its quote, so every dot on a timeline is a clickable, checkable claim.

### `grade(transcripts, rubric_id@version, out?) → grades/…jsonl`
One judge call per transcript against a **registered** rubric (rubrics are data with versions
and validation labels, DESIGN §6.2 — no inline rubrics; drafting a new one is a proposal the
human confirms). Cell: counts-first distribution strip, clickable to per-item rationales.

### `select(input, criteria, k?) → selections/<name>.json`
The hardcoded formatter: pours the rows of `input` (a summaries/grades/seeds file) plus the
`criteria` prompt into one model call; returns ids + a one-line reason each. Criteria may be
vague — *"what looks unusual or interesting"* is a legitimate query — because the output is
checkable anyway: every pick carries a reason, the reason should quote, and the file records
`input_file` and `n_considered` so misses are findable by rereading what was passed over. Cell:
hit list with reasons; "passed over n−k" is one click from a random sample of them.

### `plot(inputs, prompt, out?) → analysis/<name>.view.json`
**The model picks the view; the server computes the numbers.** The prompt goes to a small
subagent whose only output channel is a declarative view spec — which files, the join key
(usually `seed_id`), chart kind (`distribution · paired · table · grid · survival · histogram ·
ribbon`), group-by, filters. The workbench executes the spec against the JSONL rows and renders
the chart live (it re-renders as transcripts finish, same liveness as a run cell). No plotted
value ever passes through a model on its way to a pixel — that's what keeps a bar quote-chippable:
every mark resolves to its rows, rows to audit ids, ids to transcripts. Most of the time the tool
isn't even needed: whenever a grades file exists (or two share a seed key) the cell caption offers
the common projections directly ("view as distribution · paired · table"), the same way the digest
offers list/matrix/map. Cell: the chart + a provenance line of the input files + the spec one
click away.

### `read_transcript(audit_id | path, range?) → text`
Capped per call (~4k tokens). Every claim chain bottoms out here — descent terminates on
verbatim transcript text.

### `steer(run_id | audit_ids, message)` · `stop(…, reason)` · `pause` / `resume`
Feedback lands in the named audits' queues at the next turn boundary. `stop` requires a reason;
stopped audits keep their transcripts on disk and stay in denominators (a rate over survivors
must say so). Audits a human has pinned are held, not bypassed.

### Plain file tools: `bash` / `read` / `grep` / `write` (scoped to `session/`)
For everything the above doesn't cover. Multi-step reading jobs — *"read these 12 summaries and
the 3 strangest transcripts end-to-end, then tell me whether the grader is missing something"* —
can be delegated to a **general subagent** with these same file tools. Subagents are bounded:
they get file tools only (no `run_audits`, no `steer` — they can't launch or touch audits), a
step budget, and their report renders as an attributed analysis cell. This is the depth story in
practice: the orchestrator is the only thing that starts audits; audits are the only things that
talk to targets; subagents just read files and write analysis.

### `ask_human(question, options?)` · `report(text)` · `cite(claim, quotes, grades_ref)`
Unchanged from DESIGN §5: ask_human renders as a decision cell; cite is hard-gated (a finding is
a human signature) and any report/cite claim must carry quotes that check out against the
transcripts or the UI styles it unverified.

## 3. Composition, not catalog

Everything fancy is a chain over the tools above — written by the orchestrator in the moment, or
saved as a recipe once it proves common:

- **Triage a fanout:** `summarize(run_7)` → `select(summaries, "unusual or interesting", k=5)`
  → `read_transcript` the five → `report`.
- **Best-of-N:** `run_audits(seed, n_per_seed=20)` → `grade` → `select(grades, "highest with a
  clean rationale")`.
- **Ablation:** `generate_seeds("matched variants of s3 each removing one element …, plus
  scrambled control")` → human glances at the seed diffs → `run_audits(×20 each)` → `grade` →
  counts per variant. The causal table is just the grades file grouped by seed.
- **Cross-target:** `run_audits(seeds, config_A)` + `run_audits(seeds, config_B)` → `grade`
  both → `plot(both grades files, "paired by seed")` — the slope graph keyed by seed shows which
  seeds run against the trend, and those are the ones to read.
- **Delayed, not prevented:** `summarize(run, moments lens)` → `events/…` →
  `plot(events, "survival per variant")`. End-state rates can match while one variant just
  pushes the first compliance later — invisible without the time axis.
- **Continue/remix:** `generate_seeds("continuations from transcript av-03#2 node 8 trying …")` →
  `run_audits(config.continue_from=…)`.

A recipe = a saved prompt + default chain, stored as data, invokable from ⌘K or by the
orchestrator. A recipe graduates to its own handler only when usage shows its invariants deserve
server-side enforcement.

## 4. The rails that matter (and only these)

1. **Only `run_audits` reaches a target**; only the orchestrator can call `run_audits`;
   subagents get file tools only. That one sentence is the whole agentic-depth policy.
2. **Model prose carries refs.** Summaries, selection reasons, analyses, reports: quotes with
   refs — refs are how descent works, for the orchestrator's reading as much as the human's.
3. **Selection shows what it considered.** `n_considered` + `input_file` in every selection;
   passed-over rows one click away.
4. **The human's default view is not orchestrator-authored.** The standing pipeline (default
   summarize prompt + standing grades) produces the digest the human triages; orchestrator
   summaries/selections/analyses render beside it, attributed `BY ORCHESTRATOR`, lens prompts
   visible.
5. **Registered rubrics only** for grades that gate anything. Drafts are proposals with the
   orchestrator's byline, starting unvalidated.
6. **Stops are recorded, not erased.** Reason required; transcripts stay; denominators say when
   they're survivors-only.
7. **Every mark resolves to transcripts.** A chart renders only from a view spec executed by the
   server over derived files; a mark that can't resolve to its rows doesn't render. Counts on or
   beside marks, CI at n≥30, ink ramp only — the rate rules apply to pixels too.
