# Product gaps — what's missing for a shippable workbench

Consolidated from 4 surveys (2026-07-06): persistence coverage,
model-config surface, hardcoded knobs, comparable-product features.
Prioritised by "does the absence lose user work / block a real
audit?" over "would be nice".

## P0 — loses user work today (fix before anyone relies on it)

### P0.1 Pure-M1 sessions never persist
`session.save()` is called only from `Branch.run()`'s `finally`
(`run.py:525`). An orchestrator-only session — the primary M1
flow — writes **nothing** to disk. Server restart loses every
turn, finding, and gate verdict.
**Fix:** call `session.save()` from `Orchestrator.record_turn()`
(already the per-turn seam), or debounced on `_broadcast_status`.
**S** — one call site.

### P0.2 No save on server shutdown
No lifespan/`atexit` hook iterates `sessions` and saves. Ctrl-C
mid-turn loses everything since the last M0 branch settle.
**Fix:** FastAPI lifespan `yield; for s in sessions.values():
s.save()` + `signal.SIGTERM` handler. **S**.

### P0.3 `--store-dir` not honoured everywhere
`server.py:801` accepts `--store-dir` (and `STORE_DIR` global at
:55), but `orchestrator.py:140` hardcodes
`~/.workbench/sessions/{span_id}` for `session_dir` — so eval
logs, seeds, and `write_file` artifacts land somewhere the flag
doesn't control. A user pointing `--store-dir` at durable storage
(S3 mount, NFS, whatever their setup uses) still loses half the
session's files.
**Fix:** `Orchestrator.__init__` reads `session_dir` root from
the same setting; add `WORKBENCH_STORE` env var as the single
source both `server.py` and `orchestrator.py` read. Deployment
docs say where to point it for durability. **S**.

### P0.4 Pending gate → dangling tool_call on restart
`messages_for_save()` includes the assistant message whose
`review_*` call was mid-gate, but no tool result. Resumed
`generate` hits provider error on unpaired tool_call.
**Fix:** `messages_for_save()` truncates to before the last
assistant-with-unresolved-tool-call (same idea as rewind), or
synthesises a `[session restarted — please re-propose]` tool
result. **M**.

### P0.5 Orphaned eval subprocesses on restart
`bash_tool` spawns via `create_subprocess_shell`; no
process-group kill on `Session.close()`/shutdown. Subprocess
keeps burning tokens; workbench can never re-attach to its
stdout (only to its `--log-dir`).
**Fix:** track child pids on `orch`, `os.killpg` on close;
`start_new_session=True` on the `Popen`. **M**.

### P0.6 Findings have no durable store
A signed `Finding` exists only as an `InfoEvent` bundle inside
`orchestrator.eval` + a JSON string in a tool-result. No
`findings.jsonl`; not exportable without parsing the event
stream. This is the **product's primary output**.
**Fix:** `review_finding` (on approve) appends to
`session_dir/findings.jsonl` (`Finding` model_dump + timestamp +
session_id); `wb.findings()` reads it back; `GET
/sessions/{id}/findings` for the UI. **M**.

## P1 — ship blockers (can't run a real audit without)

### P1.1 Orchestrator model picker is a closed `<select>`
`StartView.tsx:55-65` — native dropdown over hardcoded `MODELS`;
no free-text, no `GenerateConfig`, `ModelPicker` role union is
`"auditor"|"target"` only. Can't pick a predep codename or set
reasoning effort for the orchestrator.
**Fix:** reuse `<ModelPicker role="orchestrator">`; extend
`{t:"start_orchestrator"}` wire to carry `config:
GenerateConfigDict`; `Orchestrator.model_args`/`generate_config`
already exist — just populate from wire and pass to
`model.generate(config=…)`. **M**.

### P1.2 Subprocess audit roles (target/auditor/judge) are prompt-text only
The only way to set them is the LLM interpolating
`{target}/{auditor}/{judge}` into its `bash` string (`prompt.py:
60-63`). No UI, no defaults, no per-role config.
**Fix:** an "audit defaults" card on `OrchStartCard` (three
`ModelPicker`s + `max_turns`/`judge_dimensions`) → interpolated
into `ORCHESTRATOR_SYSTEM_PROMPT` at `start_orchestrator` time
(so the LLM sees concrete defaults it can override, not
placeholders). Also thread as `wb.DEFAULTS` in the kernel
namespace so `python` cells can read them. **M**.

### P1.3 Model suggestions — per-provider where available
`presets.ts:8-14` is 5 hardcoded ids. `ModelPicker` already
accepts free-text (any `provider/model` string) — that's the
primary path and always works, including for vLLM/sglang/local
endpoints where enumeration is impossible. Suggestions are a
convenience layer on top.
**Fix:** `GET /models` → `{providers: [...], suggestions:
{provider: [id, ...]}}`, all offline:
- providers: `ensure_entry_points()` then
  `registry_find(lambda i: i.type=="modelapi")` →
  `registry_unqualified_name` on each. Gets all ~28 built-in +
  extension providers (narwhal etc.) registered via the
  `inspect_ai` entry-point group.
- suggestions: `inspect_ai.model._model_data.read_model_info()`
  (hand-maintained YAML per first-party org — anthropic/openai/
  gdm/grok/mistral/deepseek/together, with context-len/aliases)
  merged with our `model_palette.MODEL_ORDER`. Map org→provider
  (`gdm`→`google`). Gateway/local providers (`vllm/`, `sglang/`,
  `ollama/`, `hf/`, `openrouter/`) return prefix only — user
  types the rest.
No live SDK calls needed. **S** — ~40 LOC in `server.py` +
`ModelPicker` fetches on mount. (Still optional — P1.1's
free-text picker for the orchestrator is the actual blocker.)

### P1.4 `model_args` unreachable from wire
`base_url` / `api_key` / provider kwargs are plumbed
(`Orchestrator.model_args`, `Branch.meta.*_model_args`) but no
wire field carries them. Can't point at a local vLLM or a
project-key endpoint from the UI.
**Fix:** `ModelPicker` "advanced" expander → `model_args: dict`
JSON field; wire through `start`/`start_orchestrator`. **S**.

### P1.5 `GenerateConfigDict` is a fixed 6-key subset
No `reasoning_tokens`, `parallel_tool_calls`, `extra_body`,
`response_schema`, `logprobs`.
**Fix:** make it `Partial<GenerateConfig>` (open dict) on the
wire; backend already `**config`'s it. UI shows the common 6 +
"raw JSON" expander. **S**.

### P1.6 Export findings → markdown / issue
The whole point of an audit is the write-up. Currently: nothing.
**Fix:** `GET /sessions/{id}/export` → markdown with each signed
`Finding` (claim + quotes + `[a-XXXX·tN]` links resolved to
inspect-view URLs) + a run summary table. Button in the sidebar.
Depends on P0.6. **M**.

### P1.7 Settings panel
None exists. At minimum: store-dir, default models per role,
seed-review threshold (`>$5 or >20 samples` — currently baked
into the prompt), auto-approve toggle.
**Fix:** `~/.workbench/settings.json` + `GET/PATCH /settings` +
a modal from the sidebar. Prompt template reads thresholds from
settings at `start_orchestrator` time. **M**.

## P2 — expected conveniences (users will ask on day 2)

- **Session fork / "new session from here"** — branch the
  orchestrator conversation to try an alternate analysis.
  `Orchestrator` copy with `messages_for_save()[:N]` as the seed.
  **M**.
- **Background job panel** — list every `bash(background=True)` +
  every `AttachedRun` across the session with status/cancel.
  Data already in `orch.bg_tasks` + `run_log_dirs`. **M**.
- **Completion notifications** — desktop `Notification` when a
  bg eval's `eval_done` fires and the tab is blurred. **S**.
- **Kernel restart / interrupt orchestrator** — `⏹` exists
  (pause); add `↻` = drop `user_ns`, keep messages. **S**.
- **Context-window gauge** — `sum(len(m.text) for m in
  state.messages) / model_context_limit` in the header, with a
  "compact now" button. **S**.
- **M0 model swap on fork** — `Branch.fork` copies parent `meta`
  verbatim; no way to A/B a target model mid-tree. Add optional
  `meta` override to fork. **S**.
- **Queued messages persist** — `branch.queued` / `orch.queued`
  aren't in `save_session`. **S**.
- **Candidate-batch state persist** — Resample-N picker lost on
  restart; child branches reload but grouping/`picked` doesn't.
  **S**.
- **Cell collapse / scroll position survive reload** — per-cell
  `open` state to `localStorage` keyed by turn id. **S**.
- **`nextConfig` selection survives reload** — currently resets
  to `DEFAULT_*`; move from Zustand-only to `localStorage`. **S**.
- **Keyboard shortcuts / ⌘K palette** — j/k in run-card rows,
  ⌘Enter approve, Esc deny, ⌘K → "launch eval / jump to run /
  new session". **M**.
- **Token usage per run** — `AttachedRun._poll` already reads
  `header.stats.model_usage`; surface `Ntok` on the eval_run card
  (informational, not a $ estimate). **S**.
- **Pin/bookmark transcripts** — star a sample row → floats to a
  "pinned" rail; the raw material for findings. **M**.
- **`wb.scan` / `generate_rewrite` GenerateConfig** — currently
  model-string-only / hardcoded `max_tokens=4096`. **S**.

## P3 — differentiation (post-MVP)

- **Run diff** — select two `.eval` logs → side-by-side score
  distributions + which samples flipped. `audits_df` join. **L**.
- **Annotation queue** — mark samples {confirmed, false-pos,
  interesting}; labels persist and filter. Scout may cover part
  of this. **M**.
- **Shareable permalink** — read-only URL to a session or a
  specific transcript+turn. Needs auth story. **L**.
- **Global search** — full-text over all sessions' transcripts +
  findings. **L**.
- **Scheduled runs** — "run this audit against the nightly
  checkpoint at 2am". **L**.
- **Inline comments** — Deepnote-style threads anchored to a
  transcript turn. **L**.
- **Export session as `.ipynb`** — orchestrator turns → notebook
  cells (prose → markdown, `python` → code, cards → outputs).
  **M**.
- **Dataset/prompt versioning** — content-hash seed sets +
  auditor prompts so a finding links to the exact version. **M**.
- **Rate-limit HUD** — surface per-key RPM/TPD remaining
  (`aisi-ratelimits` output) in the header. **S**.

## Settings inventory (what a settings panel would expose)

Per P1.7, grouped:

**Global** (`~/.workbench/settings.json`):
store-dir root · default orchestrator/auditor/target/judge
models · `MODEL_TEXT_CAP` · `bash` default timeout ·
`AttachedRun._grace` · pandas display caps · plotly template ·
`read_file` limit · seed-preset library path.

**Per-session** (on `OrchStartCard`, persisted in
`orchestrator.eval` metadata):
seed-review $/N thresholds · auto-approve-under-threshold toggle
· audit `max_turns`/`judge_dimensions`/`compaction`/
`realism_filter` defaults · orchestrator `max_turns` cap ·
subprocess log-dir root.

**Not user-facing** (correctness, keep hardcoded):
`_WIRE_MIME_CAP`, WS buffer, ANSI env, poll intervals, ACP
timeouts, `INSPECT_STREAM_FLUSH_INTERVAL`.

## Suggested order

1. **P0.1 + P0.2 + P0.3** together (persistence hardening) — one
   PR, ~½ day. Unblocks trusting the tool with real work.
2. **P0.6 + P1.6** (findings store + export) — the product's
   output. ~1 day.
3. **P1.1 + P1.2 + P1.4 + P1.5** (model config) — one PR,
   `ModelPicker` everywhere + open `GenerateConfig` + model_args.
   P1.3 (suggestions endpoint) optional follow-up. ~1 day.
4. **P1.7** (settings panel). ~1 day.
5. **P0.4 + P0.5** (gate/subprocess restart edge cases). ~½ day.
6. P2 batch as capacity allows.
