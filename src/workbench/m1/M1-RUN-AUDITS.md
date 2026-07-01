# `wb.run_audits` — approaches, measurements, decision

Kai: *"`wb.run_audits` should run an inspect eval in a similar way to the
general `wb.run_eval` under the hood, just with some conveniences and
specialized UI."*

Four approaches prototyped end-to-end (2026-07-01, mockllm). Prototype
scripts in `~/.claude/jobs/db64a254/tmp/proto_{A,B,C}.py`.

## Measured comparison

| | **A · in-process `eval_async`** | **B · subprocess `inspect eval`** | **C · in-process `Branch` fanout** |
|---|---|---|---|
| Shared code with `run_eval` | ✓✓ same `_launch()` | ✓✓ same, different spawner | ✗ separate path |
| Warm startup | **0.30–0.35 s** / call | **~2.0 s** fixed (`import inspect_ai`) | ~0 |
| Cold startup | ~2.8 s (pre-warmable) | ~2.0 s every time | ~0 |
| Progress latency | 50–150 ms (`.eval` poll, `log_buffer=1`) | 50–300 ms (`--log-buffer 1`) | ~0 (event-streamed) |
| `.eval` log | ✓ native | ✓ native | via `export_run()` at end |
| `gather(run_audits×2)` (Scenario C t1) | ✓ with guard lifted; else `eval_async([tA,tB], max_tasks=2)` fallback | ✓ trivial | ✓ trivial |
| Cancel whole run | ✓ `task.cancel()` | ✓ `SIGINT` → `status='cancelled'` | ✓ per-branch |
| **Steer/stop one audit mid-run** | ✓ see §steer | ✗ (no in-process handle) | ✓ `branches[i]` |
| Pause/step/resample one audit | ✗ → `wb.pin()` | ✗ → `wb.pin()` | ✓ native |
| Survives workbench crash | ✗ | ✓ | ✗ |
| Scales past N≈50 | ✓ inspect's own concurrency | ✓✓ shard across procs | needs `_on_event` timeline-skip fix (O(N²)) |

## Decision: **A**, with B as `detached=True` and pin-to-desk for pause/resample

`run_audits` and `run_eval` share one launcher:

```python
async def _launch(task, *, log_dir, handle_cls, **kw) -> RunHandle:
    h = handle_cls(task, log_dir)
    h._dh = display(h, display_id=h.id)
    h._task = asyncio.create_task(
        eval_async(task, log_dir=log_dir, log_buffer=1, **kw)
    )
    asyncio.create_task(h._watch(h._task))   # polls .eval, dh.update()s
    return h
```

`run_audits(seeds, cfg, *, description, n_per_seed=1)` builds
`petri_audit_task(seeds, cfg, n_per_seed, auditor=batch_auditor)` and calls
`_launch(handle_cls=AuditRunHandle)`. `run_eval(task, **kw)` calls
`_launch(handle_cls=EvalHandle)`. The petri-specific "conveniences and
specialized UI" live entirely in `RunProposal` / `AuditRunHandle`:

- `RunProposal._repr_mimebundle_`: seed previews, strike-to-remove; verdict
  carries the surviving seed list.
- `AuditRunHandle`: per-audit rows (`audit_id · seed[:60] · turns · score`);
  each row is a `wb://audit/{id}` link → `wb.transcript`; **pin** action.
- `.audits` = `samples_df(log_dir)` + petri's `audit_summary` columns.

### `detached=True` → subprocess (B)

Same `RunHandle` (polls the same `.eval`), just
`asyncio.create_subprocess_exec("uv","run","inspect","eval", …,
"--log-buffer","1")` instead of `eval_async`. For overnight sweeps that
should survive the workbench process. 2 s startup is irrelevant there.
`SIGINT` for cancel (`SIGTERM` leaves the log at `status='started'`).

### Desk interaction — punch down into a running sample

Batch runs are fire-and-collect; `wb.steer`/`wb.stop` reach running
samples via `CONTROL`, and `wb.transcript`/`wb.excerpt` read the `.eval`.
Live pause/step/**resample/edit** is what the M0 desk *is* — the human
gets there by clicking a `RunHandle` row:

- **completed sample** → `{"t": "import", "path": log, "sample_id": id}`
  (existing).
- **running sample** → `{"t": "import_running", "sample_id": id}` —
  `adopt_running` interrupts the sample (its `AuditTape` flushes to
  `.eval`), then `import_eval`s it. Adopt semantics: the batch loses that
  sample; the desk `Branch` picks up at the exact turn it was on.

That's a UI action, not something the agent calls, so there's no
`wb.pin`. **v2** (snapshot without stopping — the batch sample keeps
running, the desk `Branch` is a fork) needs `ActiveSample.store` on the
inspect fork so the tape can be read without waiting for a flush; ~3
lines, tracked in M1-REFACTOR-NOTES.md.

## §steer — steering under A

Verified working end-to-end (in-process, no ACP server):

```python
CONTROL: dict[str, dict] = {}   # sample_id → {"queued": [msg], "stop": bool}

# batch_auditor — petri's auditor loop with a 3-line drain, before each generate:
ctl = CONTROL.get(str(state.sample_id), {})
state.messages.extend(
    ChatMessageUser(content=m, source="operator") for m in ctl.pop("queued", [])
)
if ctl.get("stop"):
    await channel.end_conversation(); break

# wb.*:
def steer(ids, msg): ...   # CONTROL[id]["queued"].append(msg)
def stop(ids): ...         # CONTROL[id]["stop"] = True
```

Same drain point as M0's `workbench_auditor` (`branch.queued["auditor"]`),
just keyed by `sample_id`. For `run_eval` on arbitrary tasks: `wb.stop`
uses `active_samples()[i].interrupt("score")` (no cooperation needed);
`wb.steer` requires `acp_server=True` + a channel-based agent
(`active_samples()[i].acp_transport.submit_user_message(msg)`).

## §guard — the `_eval_async_running` restriction

`_eval/eval.py:720-722` blocks concurrent `eval_async`. The code's own
comment (:715-719) says the original reason (per-task `chdir`) is gone. A
global-state audit + empirical test found:

- Correctness state (active model, roles, transcript, concurrency
  controllers) is ContextVar-scoped; recorder/sample-buffers are keyed by
  `log_dir`; display-type/log-handler/hooks-cache are set-once idempotent.
- 2-way and 3-way concurrent `eval_async` (guard patched out) succeed with
  no cross-contamination, cwd preserved. 3-way wall 0.84 s vs solo 0.49 s.

Applied on `inspect_ai` `model-event-output-streaming` @ `fa94cd82`
(upstream candidate; patch archived at
`patches/inspect-eval-async-guard.patch`). Fallback if upstream
declines: `eval_async([taskA, taskB], max_tasks=N)` writes one `.eval` per
task into a shared `log_dir`; each `RunHandle._find_log()` picks its own by
task name (verified working).

## Required at kernel init

```python
from inspect_ai.util._display import init_display_type
from inspect_ai._util.platform import platform_init
init_display_type("none")   # suppress inspect's progress spew → stdout
platform_init()             # print-idempotent hooks banner once, not per-eval
```

Plus optionally one throwaway `eval_async` to pay the ~2.5 s cold cost.

## Gotchas found

- `read_eval_log_sample_summaries_async` is at `inspect_ai.log._file`, not
  re-exported from `inspect_ai.log`.
- `EvalSampleSummary.completed=True` for cancelled/errored samples too;
  filter on `s.completed and s.error is None`.
- `list_eval_logs()[i].task` is filename-cleaned (`_→-`); match against
  `clean_filename_component(task_name)`.
- Subprocess: absolute task paths break (`list.py:66` glob); pass relative
  or `package/task` entry-point syntax.
- C's bottleneck is `build_auditor_timeline` (O(N²), fires on every
  `ModelEvent`). Fix regardless: skip the rebuild in `session._on_event` for
  `meta.batch`-tagged branches.

## What this changes vs M1-NOTEBOOK.md v4

- `wb.steer`/`wb.stop` work on batch audits (via `CONTROL` registry), not
  just pinned ones.
- The "kernel must share the loop so `run_audits` can spawn `Branch`
  coroutines" rationale (§Kernel) narrows to "so gates can `await` Futures
  the WS handler resolves, and `wb.steer` can reach `active_samples()`".
- Desk interaction on a batch audit is a *UI* action (`RunHandle` row →
  `import`), not a `wb.*` helper.
- `run_audits`/`run_eval` gain `detached: bool = False`.
