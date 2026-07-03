# Concurrent `eval_async()` — proper fixes

Design consolidated from three subagent audits (registries / process
side-effects / UI singletons) after PR #4411's safety audit found the
`_eval_async_running` guard is **not** vestigial. The PR's
`nested: bool` threading works but puts the refcount at the wrong
layer; this doc is the "right" shape.

**Branch topology** (corrected): upstream `main` @ `64e0ff05` still has
the guard. `fa94cd82` (our own guard-removal, with a false
"ContextVar-scoped" claim) is on `model-event-output-streaming` only —
our fork, not main. So #4411's env-var opt-out is correctly positioned
relative to main; the design below is what #4411 should become.

## The general pattern

For every module-global registry touched during `eval_async`:

> **Keep the container process-global.** Make each *entry*
> self-identifying (`eval_id`/`run_id`) or self-cleaning (CM
> append/remove). **Never bulk-`.clear()`.** Reset only at the 0→1
> refcount transition; clean up only your own entries on exit.

`ContextVar` is *wrong* for the containers — every one has an
out-of-tree reader (control server, display task, ACP picker,
workbench polling `active_samples()`) that must see all evals in the
process. `ContextVar` is *right* for per-eval config
(`_log_refusals`, `_max_subprocesses`, etc.).

## Cluster 1 — registries (load-bearing)

| State | Right scoping | Fix |
|---|---|---|
| `_active_samples` | Already correct (per-entry CM) | Delete dead `init_active_samples()` |
| `_concurrency_registry` + observers | Process-global is **semantically correct** — two evals on the same API key SHOULD share one `AdaptiveConcurrencyController` | `concurrency_scope()` CM with internal refcount; reset registry only at 0→1. `DynamicSampleLimiter.close()` unregisters its observer in `task/run.py` `finally`. |
| `_eval_states` | Already keyed by `eval_id` | `clear_eval_states_for_run(run_id)`; fix `_control/state.py:107` group key to `(run_id, task_id)` (comment cites the removed guard) |
| `_refusal_count` / `_log_refusals` | Count = shared TUI (combined is correct UX); flag = per-eval config | `_log_refusals` → `ContextVar[bool]`; count reset moves under 0→1 |

`_eval/context.py` drops the `init_concurrency()`/`init_active_samples()`
calls; `eval.py` wraps the run body in `with concurrency_scope():`.
`_eval_async_running: bool` → depth counter (as #4411 already does).

## Cluster 2 — process side-effects (can't scope, must eliminate)

| Site | Root cause | Fix (in #4411) | Follow-up PR |
|---|---|---|---|
| `run.py:171` `chdir` around `await logger.init()` | Only `TaskLogger.__init__` (sync) reads cwd (`git_context()`, `cwd_relative_path()`); the `await` is inside by accident | Move `await logger.init()` **outside** the `with chdir:` — chdir wraps only the sync ctor | `git_context(cwd=)`, `cwd_relative_path(base=)`, `TaskLogger(run_dir=)`; drop chdir |
| `sandbox.py:211` `chdir` around `task_init_environment` | Sample-level `sandbox.config` isn't absolutized (task-level already is at `loader.py:430`) | Absolutize in `resolve_sandbox(…, run_dir=)` ; drop chdir | — |
| `run.py:938,958` `chdir + environ_vars` around `task_init`/`task_cleanup` | `ComposeProject.create` reads cwd for bare-docker/`ComposeConfig`; `SAMPLE_METADATA_*` reaches `docker compose` via `os.environ` | **Stage 1**: module-level `anyio.Lock` around chdir+environ+await (serializes sandbox startup across evals — acceptable) | **Stage 2**: `SandboxEnvironment.task_init(…, run_dir, env)` kwargs; `ComposeProject.create(run_dir=)`; signature-introspect shim + `DeprecationWarning` for third-party providers |
| `run.py:399` `cleanup_s3_sessions()` | Added for cosmetic aiohttp `__del__` warnings (s3fs #943); closes the *process-global* fsspec cache → first eval to finish kills everyone's S3 | **Delete from `eval_run` finally**; call once in `eval()`'s `run_task_app` finally (loop teardown). Export for `eval_async` callers to invoke on their own shutdown. | — |

`chdir_python()` in `loader.py` is sync-only (no `await`) → safe on a
single loop; out of scope.

## Cluster 3 — UI/control singletons (one terminal IS one resource)

| Singleton | Skip-when-nested correct? | Right behavior |
|---|---|---|
| `_active_display` / `task_screen()` | **No** — skip hides eval B | Refcounted reentrant `task_screen()`: first opens `Live`, later evals **join** (append tasks to the shared panel), last closes. Same UX as multi-task-in-one-eval. |
| `init_logger()` | **Yes** — already first-wins | Optionally widen level to `min()` on later calls |
| `set_run_shape()` (`_task_names`/`_max_epochs`) | No — skip means B's tasks never get `task=` prefix | Drop the globals; always print `task=`/`epoch=` when a sample is active (or accumulate: union / `max()`) |
| `_keep_alive` / `reset_keep_alive()` | Yes for reset | Gate reset on `depth==0→1`; gate the *park* on `depth 1→0` (last-out) |
| ctl server bind | Yes for bind; **no** for state registration | First eval binds one process-scoped `ControlServer`; nested evals `register_eval` but don't bind. `/evals` shows the union. |
| `set_model_cost()` | N/A — idempotent in practice | Document as process-global |

## What goes in #4411 (rework — idempotent init + per-eval cleanup)

Three mechanisms, each where it semantically fits. No new
abstractions; each change is local to its module.

### 1. Idempotent init (create-if-missing — no refcount coupling)

| File | Change |
|---|---|
| `util/_concurrency.py` | `init_concurrency()`: `if _concurrency_registry is not None: return` at top. Registry is process-lifetime; controllers keyed by API URL accumulate naturally. (Tests that want a fresh registry call a new `_reset_concurrency_for_tests()`.) |
| `log/_samples.py` | `init_active_samples()`: delete body (entries are already per-sample CM append/remove). |
| `log/_refusal.py` | `init_refusal_tracking()`: drop the `_refusal_count = 0` reset (display resets when it opens). Keep the `_log_refusals` set. |

No import from `_eval/eval.py` → no circular-dep risk.

### 2. Per-eval cleanup (remove-your-own — no resource creep)

| File | Change |
|---|---|
| `_control/eval_state.py` | New `clear_eval_states_for_run(run_id: str)` — deletes only entries whose `state.run_id == run_id`. `_eval/eval.py` `finally` calls this instead of `clear_all_eval_states()`. |
| `util/_concurrency.py` | `DynamicSampleLimiter.close()` → `_controller_created_observers.remove(self._on_...)` (suppressed). `_eval/task/run.py` calls it in the `finally` around the task body where the limiter is created. |
| `log/_samples.py` | Already per-entry (`active_sample()` CM). ✓ |

Long-running processes (workbench, notebooks) don't accumulate stale
`_eval_states` entries or dead observer callbacks across sessions.

### 3. Refcount-gated (last-out — genuinely process-singular)

`_eval_async_running: bool` → `_active_eval_count: int` +
`active_eval_count()` accessor. Only ~3 sites read it — the things
that *can't* be per-eval:

| File | Change |
|---|---|
| `_eval/eval.py` | Increment before init, decrement in `finally`. Guard raises on `> 0` (before increment) unless `INSPECT_ALLOW_CONCURRENT_EVAL_ASYNC`. In `finally` after decrement, `if _active_eval_count == 0:` gates keep-alive park. `reset_keep_alive()` gated on `== 0` before increment. |
| `_eval/run.py` | `cleanup_s3_sessions()`: `if active_eval_count() > 0: return` at top (fsspec cache is process-shared; can't close per-eval). |

### 4. chdir hazards (unchanged from earlier)

| File | Change |
|---|---|
| `_eval/run.py` | Move `await logger.init()` outside the `with chdir(…):` (chdir wraps only sync `TaskLogger()`). Module-level `_sandbox_lock = anyio.Lock()`; `async with _sandbox_lock:` around `chdir + environ_vars + await task_init/task_cleanup`. |

### Tests

- `test_concurrent_eval_async_opt_in` (existing)
- `test_concurrent_eval_shares_concurrency_registry` — `id(_concurrency_registry)` unchanged across two overlapping evals
- `test_concurrent_eval_cleans_own_eval_states` — after eval A finishes (B still running), `_eval_states` has only B's entries

## Follow-up PRs (separate, can iterate)

- `SandboxEnvironment.task_init(run_dir=, env=)` API extension
- `git_context(cwd=)` / `cwd_relative_path(base=)` / drop remaining chdirs
- `task_screen()` refcounted reentrancy
- `DynamicSampleLimiter.close()` + `_control/state.py` group key
- `set_run_shape()` → always-print or accumulate

## Workbench (regardless of upstream)

- **Cherry-pick 1-8 onto `model-event-output-streaming`** — our fork's
  `fa94cd82` removed the guard without fixing `init_concurrency()`, so
  every `wb.run_eval` while another runs currently replaces the
  adaptive-controller registry (silent throughput bug).
- `RunHandle.launch`: pass `ctl_server=False` (avoid N discovery
  sockets per session; workbench polls the log file directly).
- `display=none` (already via `_prewarm()`) is sufficient for the rest.
