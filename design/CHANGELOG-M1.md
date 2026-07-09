# M1: orchestrator column + hardened wire protocol

Squash-merge body for `m1/kernel-spike` → `main` (296 commits).

## Headline

M1 adds an **orchestrator column** alongside M0's auditor/target desk: an
in-process IPython kernel drives a Claude agent with `bash` / file /
`python` / `review_*` tools and a `wb.*` analysis namespace, so a single
session can launch subprocess evals, attach live progress cards, plot
results, and gate findings for human sign-off. The target column becomes a
**session-wide swimlane** whose L1×L2 rollback tree is grafted from the
event stream (no `Tape` reads). The wire protocol is rebuilt around
**typed deltas** in atomic `{t:"batch"}` frames, a `pending`/`{t:"ack"}`
overlay for optimistic UI, and a version-guarded `_desync` → full-state
resync path — verified under chaos, delay-injection, and reconnect-storm
stress. Replaces the legacy `src/collaborative_auditor/` + `frontend/`
tree entirely.

## New features

**Orchestrator (M1 column)**
- In-process `InteractiveShell` kernel with `DisplayPublisher` capture,
  `Gate` primitive, background cells, rewind, restart-kernel, interrupt.
- Tool surface: `bash` (streams stdout, parses `{"wb":…}` lines into live
  cards, `background=True` bg jobs), `read_file`/`write_file`/`edit_file`,
  `python`, `ask_human`/`review_seeds`/`review_finding` (gate cards).
- `wb.*` namespace: `attach`, `scan`, `transcript`/`excerpt`, `cite`,
  `diff`, `findings`, `plots.by_model` (+ `model_palette` — provider hue ×
  tier lightness, Okabe-Ito colorway), `DEFAULTS`.
- Evals run as **subprocesses** via `bash("inspect eval …")` +
  `WorkbenchDisplay` driver (`WORKBENCH_DISPLAY=1`); `wb.attach(log_dir)`
  polls the log + ctl socket for a live `ProgressCard`.
- `findings.jsonl` durable store + markdown export + `.ipynb` export
  (orch turns → notebook cells) + `file_hashes` prompt/seed versioning.
- Per-session `session_dir` cwd; per-turn + on-shutdown persistence;
  session-fork (`{t:"fork_orchestrator"}` → new `Session`, own dir,
  parent runs re-attached by absolute path).

**Scanners** — scout-backed library + `groups.yaml` named groups; M0
manual scan (column-head picker → `scan_branch`); live per-turn scanners
(`post_turn` hook → score badge on each target bubble as it lands).

**Desk (M0)** — Resample-N candidates picker; LLM-assisted rewrite +
`rewrite_tool_call`/`rewrite_target_message`; unqueue (× on ghost
bubble); honest pause = interrupt (cancels mid-generate, retracts pending
bubble); model swap on fork; `import_running` (ACP interrupt → adopt a
live batch sample into the desk).

**Shell** — `ModelPicker` combobox everywhere (free-text + `/models`
suggestions from inspect registry + `model_args`/open `GenerateConfig`);
`SettingsModal` (`settings.json` round-trip); ⌘K command palette +
keyboard shortcuts; bg-jobs panel (bash-bg + attached runs, cancel);
pinned-transcripts rail; annotation labels on pins; context-window gauge;
completion notifications; auto-reconnect with backoff + reconnecting
banner.

## Architecture

- **`by_role` event routing** — every `ModelEvent` bucketed by
  `(branch_id, role)` on emit; both timelines read from it.
- **A1-b-wide** — `build_target_timeline` is session-wide and purely
  event-sourced: `anchor_owner` maps each target message-id to the L1
  span that generated it live, and each L1 span grafts under
  `anchor_owner[branchedFrom]`. `_by_anchor`/`_index_anchor` deleted.
  `l1_spans` shipped on branch meta; `{t:"l1_spans"}` refresh on live L1
  rollback.
- **A3** — `_on_event` emits one atomic `{t:"batch", v, ops}` frame; the
  five mid-session `broadcast(view())` sites become typed deltas
  (`branch_created`/`current`/`batch_resolved`/`orch`/`queued_consumed`).
  `case "state"` runs on connect only. Version guard drops stale frames.
- **A2** — actions never mutate server-owned state: `send()` appends to
  `store.pending`, server replies `{t:"ack", req_id}`, selectors overlay
  `pending` on server state. One `useIsPending(pred)` hook + `FORK_KINDS`
  replaces ~200 LOC of per-button guards / `PENDING_*` sentinels /
  `reconcile*` machinery.
- **A4** — `Orchestrator.dirty()` → `{t:"orch"}` for process-only state
  (`bg_jobs`/`bg_cells`/`run_log_dirs`/`model`); transcript-derivable
  fields (`pending_gates`, `context_chars`) folded on the frontend.
- **Pause = interrupt** — `_gen_scope` cancel; pending `ModelEvent` marked
  `rewound`; `Step(None)` cleanup; `generating` on `{t:"status"}`.
- **Dispatch lock** released before slow waits (`_register_and_spawn`
  replay, `rewind` bg-cell gather under `move_on_after`, `_do_end`);
  `switch` pauses the outgoing branch; `{t:"approve_all"}` server-side.
- **Persistence** — `schedule_save()` bg flush (2s debounce) + shutdown
  hook; dangling tool_call → synthetic result on resume; bash procs +
  kernel bg cells reaped on `close()`.
- **Resync** — `_enqueue` overflow sets `_desync` → one `push_full_state`;
  `send()` outbox flushes on reconnect; `onclose` code≥4000 = no-retry.

## Frontend (`frontend-wb/`, replaces `frontend/`)

- `SwimlaneColumn` renders session-wide target lanes via
  `convertServerTimeline → computeFlatSwimlaneRows → splice()` (same path
  as auditor); cross-branch lane click → `switchBranch`.
- `OrchColumn`/`OrchTurn`/`Output` + card set (`GateCard`, `ProgressCard`
  with sort/filter/histogram/per-sample-stop, `ReaderCard`, `FindingCard`,
  `DiffCard`); `.bash-cell` streams stdout with inline WB_MIME cards.
- Store perf: **mutable `events` Map + `eventsRev` counter** (bumped on
  structural change only), incremental `pool` resolve, `turnScores` index,
  `React.memo` on `ModelEventRow`/`OrchTurn`, WeakMap `stripCache` for
  `convertServerTimeline` — ~72× render reduction at 100 turns × 10
  branches (perf probe `_e2e_m1_perf`).
- `useIsPending`/`FORK_KINDS`; error banner in `App`; a11y (aria-labels
  on icon buttons, modal autofocus, Esc `stopPropagation`).

## Testing

- Playwright chaos: `_e2e_m1_chaos` s1-s8, `_chaos_v2` s9-s13
  (adversarial rapid-click), `_chaos_delay` (`WORKBENCH_BROADCAST_DELAY_MS`
  jitter), `_reconnect` (8× close/reopen storm), `_perf` (render probe).
- `_smoke_actions_wire` W1-W16 (every wire cmd → typed-delta contract);
  `_smoke_combos` C1-C15 (edit×branch×rollback edge cases);
  `_smoke_target_timeline_stress` (5 candidates × L1 rollback × mid-poll
  L2 fork — graft invariants + 0.08ms/call).
- `_smoke_m1_{kernel,orchestrator,hybrid,features,streaming,scan,coverage}`
  + `_e2e_m1_real` drives to `review_finding` against live models.
- `store/session.test.ts` reducer units for every `Down` variant;
  regenerated fixtures post-A3.

## Dependencies

- **petri** `tape-replay-v2` (PR #111) — multi-level rollback,
  `History.dump(root=)`, `auditor_prelude`/`run_turn_tools` export. Petri
  patch carried in `m1/patches/petri-auditor-prelude.patch`.
- **inspect_ai** `model-event-output-streaming` fork — 8 streaming
  commits kept; concurrent-`eval_async` guard commits reverted (evals now
  subprocess). Also tracks `#4222` (`MessagePoolIndex`).

## Migration notes

- `WORKBENCH_STORE` (or `--store-dir`) is the single persistence root
  (default `~/.workbench/`): `sessions/{id}/` (orch cwd, `runs/`,
  `findings.jsonl`, branch JSON), `settings.json`, `scanners/` +
  `groups.yaml`.
- `bash` tool sets `WORKBENCH_SESSION_DIR` + `WORKBENCH_DISPLAY=1` +
  `TERM=dumb`/`NO_COLOR` in the subprocess env; `_audit_task.py`
  re-anchors relative `-T seeds_file=` against `WORKBENCH_SESSION_DIR`.
- `WB_PORT` for parallel instances; `VITE_BACKEND` for `/sessions` proxy.
- Legacy `src/collaborative_auditor/` + `frontend/` deleted.

## Known limitations / follow-ups (OVERNIGHT-SWEEP §P4)

- Per-role `eventsRev` split (residual ~1600 `SwimlaneColumn` recomputes
  from cross-role structural events; would cut to ~400).
- `Settings.default_*` not yet read by `StartView` (round-trips in
  `SettingsModal` only — wire or delete).
- `session.events` unbounded (~100MB at long sessions) — needs cap/rotate
  design that preserves rewound + cross-branch refs.
- Row virtualization (react-window) deferred behind `React.memo`.
- Shared `<PendingButton>` spinner idiom; `notifications` cap;
  `stop_sample`/`cancel_bg` no-op toast feedback.
