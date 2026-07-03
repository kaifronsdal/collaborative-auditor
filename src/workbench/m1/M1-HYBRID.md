# M1 hybrid — CC tools + IPython kernel + explicit review (2026-07-03)

Supersedes the in-process `eval_async` design (M1-RUN-AUDITS.md
approach A). The concurrent-`eval_async` audit
(patches/CONCURRENT-EVAL-DESIGN.md) found ~10 process-global hazard
sites; fixing them properly needs structural upstream changes we
don't control. Instead: **evals run in subprocesses via a `bash`
tool**, the IPython kernel stays for analysis/display, and approval
is an explicit tool the agent opts into.

## Tool surface

The orchestrator agent has these tools (inspect `Tool`s registered on
`orchestrator_agent`):

| Tool | Signature | For |
|---|---|---|
| `bash` | `(cmd: str, timeout: int = 300, background: bool = False) -> str` | Run `inspect eval`, scripts, file ops. `background=True` returns immediately with a `[bg-{id}]` handle; output arrives as a `{"wb":"bg_done",...}` notification. |
| `read_file` | `(path: str, offset: int = 0, limit: int = 2000) -> str` | Read task files, seeds, `.eval` metadata |
| `write_file` | `(path: str, content: str) -> str` | Write task files, seed lists |
| `edit_file` | `(path: str, old: str, new: str) -> str` | Tweak configs |
| `python` | `(code: str, background: bool = False) -> str` | **Existing kernel.** Analysis, DataFrames, plotly, `wb.*` read helpers. |
| `ask_human` | `(question: str, options: list[str] \| None = None) -> str` | General y/n/free-text. Renders `GateCard[prompt]`. |
| `review_seeds` | `(seeds: list[str], description: str, config: dict = {}) -> dict` | Seed-list approval. Renders `GateCard[run_proposal]` + Modal. Returns `{"approved": bool, "seeds": list[str], "reason": str \| None}`. |
| `review_finding` | `(claim: str, quotes: list[dict], description: str) -> dict` | Sign-off on a claim. Renders `GateCard[cite_proposal]`. Returns `{"signed": bool, "quotes": list, "reason": str \| None}`. |

The three review tools are thin `@tool` wrappers around
`kernel.gate(Proposal(...))` — same `Gate` primitive as today, just
exposed at the tool level instead of only inside `python` cells. They
also remain callable as `wb.ask_human()`/`wb.review_seeds()`/`wb.cite()`
inside `python` for when the agent computes → reviews → launches in
one cell.

## `bash` tool — sandboxing + rendering

**Execution:** `asyncio.create_subprocess_shell(cmd, cwd=session_dir,
env={...})`. `session_dir` is a per-orchestrator working directory
(`~/.workbench/sessions/{span_id}/`) so file paths are stable across
turns and don't collide with other sessions. `timeout` kills the
process; `background=True` detaches (like the kernel's cell-detach)
and the tool result is `[bg-{id} started]`.

**Rendering:** stdout/stderr stream to the frontend as
`DisplayEvent`s (same pipe as `python` outputs — `_on_display`).
Lines matching `^{"wb":` are parsed and emitted as WB_MIME cards
instead of text; everything else is `.out-stream`. So a single
`bash` call can produce interleaved plain output + rich cards.

## `{"wb":...}` protocol — `WorkbenchDisplay`

New `src/workbench/m1/wb_display.py` registers an inspect display
driver (`--display workbench`). It implements the `Display` protocol
and writes JSON lines to stdout:

```
{"wb":"eval_start","eval_id":"...","task":"audit-a5b","total":12,"model":"...","log_dir":"..."}
{"wb":"eval_progress","eval_id":"...","done":3,"running":[{"id":"s1","epoch":1,"turns":4,"tokens":1200}],"elapsed":42.1}
{"wb":"eval_sample_done","eval_id":"...","id":"s0","epoch":1,"scores":{...},"error":null}
{"wb":"eval_done","eval_id":"...","location":".../x.eval","done":12,"errors":0}
```

Emitted from `Display.task_screen()`/`.progress()`/sample-complete
hooks. The `bash` tool's line parser sees `{"wb":"eval_start"}` →
mounts a `ProgressCard` with `display_id = eval_id`; subsequent
`eval_progress`/`sample_done` lines `dh.update()` the same card.
`eval_done` settles it.

Registration: `wb_display.py` uses `entry_points` group
`inspect_ai.display` (check inspect's registration mechanism) or
monkeypatches `_display._displays["workbench"] = WorkbenchDisplay`.
The agent sets it via `INSPECT_DISPLAY=workbench` in the `bash` env
(so the agent doesn't have to remember `--display workbench` on
every command).

**Also renders:**
```
{"wb":"file","path":"out.html","kind":"plotly"}     → HtmlOutput
{"wb":"file","path":"df.csv","kind":"dataframe"}    → table
{"wb":"ref","location":"...","sample_id":"..."}     → clickable qref
```
so the agent can emit rich outputs from any script, not just
`inspect eval`.

## `wb.attach(log_dir)` — read-only handle

The polling/display half of today's `RunHandle`, minus launching:

```python
h = wb.attach("runs/r1")   # → AttachedRun
h                          # displays a live ProgressCard (dh.update ticks)
await h.wait()             # blocks until the .eval settles
h.audits                   # → audits_df(h.location)
h.location                 # → the .eval path
```

`AttachedRun` polls `list_eval_logs(log_dir)` +
`read_eval_log_sample_summaries_async` (existing code) and, if a
ctl discovery file for that `log_dir`'s process exists, also `GET
/evals/{id}/samples` for `running` rows. Same `_repr_mimebundle_` →
`ProgressCard` as today. No `.cancel()` (the agent `bash("kill
{pid}")` or lets it finish).

## `wb.*` (python namespace) — what remains

Read/analyze/present only. No launching, no steer/stop.

- `wb.attach(log_dir)` — above
- `wb.transcript(log, sample_id)` / `wb.excerpt(...)` /
  `wb.read_transcript(...)` — unchanged
- `wb.plots.*` — `by_model`, `model_label`, `model_colormap`,
  `link`, `annotate_top`, etc. — unchanged
- `wb.cite(...)` / `wb.ask_human(...)` / `wb.review_seeds(...)` —
  aliases for the review tools, callable in-cell
- `audits_df`, `flat_score_values` — re-exported from petri

## Import-to-auditor

Row click on a `ProgressCard` → `send({t:"import", path: log,
sample_id})` → `import_eval` → M0 replay (existing). For a *running*
sample: `send({t:"import_running", sample_id, pid})` → `bash`-side
`inspect ctl` interrupt (or ACP `inspect/cancel_sample` if we want
per-sample) → wait for flush → `import_eval`. Drop
`snapshot_running`; adopt-only.

## What's deleted

- `m1/run.py`: `RunHandle.launch` (the `eval_async` path),
  `RunProposal`, `CONTROL`, `steer()`, `stop()`, `BatchHooks`,
  `snapshot_running`, `adopt_running` (moves to `server.py` as a
  ctl-interrupt helper). Keep: `SampleRow`, `_PollingHandle` base,
  `AttachedRun` (renamed from `RunHandle`, launch removed),
  `ScanHandle` (still useful — scout runs in-process fine, no
  concurrent-eval issue).
- `m1/wb.py`: `run_audits`, `run_eval`, `steer`, `stop`,
  `GATE_THRESHOLD`. Add: `attach`, `review_seeds`.
- `auditor.py`: `BatchHooks` integration in `workbench_auditor` (M0
  desk hooks stay).
- `_smoke_m1_run.py`: cells 1-4 rewritten for `bash` + `attach`;
  concurrent-eval cell 3 dropped (no longer relevant); steer/stop
  cell 2 dropped.
- Fork patches: `inspect-eval-async-guard`,
  `inspect-active-sample-store`, `inspect-init-active-samples-noop`
  → delete. Fork commits `fa94cd82`/`4636d9a6`/`536002a8`/`1a36c4dc`
  → revert. Keep: the 8 streaming commits.

## What's new

- `m1/tools.py` — `bash`/`read_file`/`write_file`/`edit_file` +
  `ask_human`/`review_seeds`/`review_finding` `@tool` defs. `bash`
  streams via `_on_display`; review tools call `kernel.gate(...)`.
- `m1/wb_display.py` — `WorkbenchDisplay` inspect driver.
- `m1/_audit_task.py` — `@task def audit(seeds_file: str, config:
  str)` subprocess entrypoint (builds the same
  `Task(seeds_dataset, audit_solver(workbench_auditor(...)),
  audit_judge, ...)` as today's `wb.run_audits`).
- `m1/attach.py` — `AttachedRun` (extracted from `RunHandle`).
- `frontend-wb`: `OrchTurn.tsx` renders `bash` `ToolEvent`s (a
  `.bash-cell` alongside `.code-cell` — head shows `$ {cmd}`,
  body streams stdout with WB_MIME cards inline). `ProgressCard`
  reads either `attach`-payload or `eval_progress`-payload shape.

## Migration order

1. `wb_display.py` + `_audit_task.py` — standalone; verify
   `INSPECT_DISPLAY=workbench inspect eval _audit_task.py:audit -T
   seeds_file=... | jq 'select(.wb)'` produces the expected lines.
2. `m1/tools.py` — `bash` tool with the `{"wb":...}` line parser +
   review tools. `orchestrator.py` registers all 8 tools.
3. `attach.py` — extract polling half of `RunHandle`.
4. `_smoke_m1_run.py` rewrite → `_smoke_m1_hybrid.py`: agent runs
   `bash("inspect eval ...")`, then `python("h = wb.attach(...); await
   h.wait()")`, asserts card ticks + `.eval` written.
5. Frontend: `.bash-cell` render + `ProgressCard` payload union.
6. Delete the dead `run.py`/`wb.py` code + fork patches; revert
   fork commits.
7. Rewrite `prompt.py` for the new tool surface.
8. `_screenshot_m1.py` update for `.bash-cell` states.

## System prompt guidance (sketch)

> You have `bash`/`read_file`/`write_file`/`edit_file` for running
> evals and managing files, and `python` (a persistent IPython
> kernel with `pd`/`np`/`px`/`wb.*`) for analysis and rich display.
>
> To run audits: write seeds to a JSON file, then `bash("inspect
> eval workbench.m1._audit_task:audit -T seeds_file=... -T
> config='{...}' --model {target} --log-dir runs/{name}
> --log-buffer 1")`. Progress renders as a live card. When it
> finishes, `python("h = wb.attach('runs/{name}'); df = h.audits")`
> to analyze.
>
> Before any run you estimate at >$5 or >20 samples, call
> `review_seeds(seeds, description, config)` and only proceed if
> `approved`. Before publishing a finding, call `review_finding(...)`.
> Use `ask_human(...)` for anything else you need input on.
>
> `wb.plots.by_model(df)` for model-comparison charts;
> `wb.transcript(log, id)` / `wb.excerpt(...)` to cite;
> `display(Markdown(...))` for prose.
