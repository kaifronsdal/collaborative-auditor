# M1 kernel — notes for M1.1

What the spike (`kernel.py` @ this commit) proved, what it deliberately
punts, and what the subagent reviews surfaced that changes the M1.1 plan.
Cross-ref M1-NOTEBOOK.md v4 (`audit-workbench-design` `m1/mockups-iter-0630`).

## Proven

- `run_cell_async` cooperates with a running loop; concurrent cells overlap
  and share `user_ns`. `display_trap`/`builtin_trap` are refcounted and
  re-entrant. ipykernel *serializes* cells, so concurrent `run_cell_async`
  has **no upstream precedent** — the four races below are ours to own.
- One `display_pub` hook + one `displayhook` override is enough for every
  output (explicit `display()`, last-expr, `dh.update()`, streams) to
  arrive as a `DisplayEvent` in emission order under the correct turn's
  `ContextVar`.
- `display + await Future + dh.update` is the whole gate mechanism.
- Cells-as-tasks: fg = await, bg/detach = stop awaiting; done-callback
  enqueues the `[done]` chip only for detached cells.

## Concurrent-cell races we accept (documented, disarmed where cheap)

| Shared slot | Consequence | Mitigation |
|---|---|---|
| `displayhook.exec_result` | `r.result` may land on wrong `ExecutionResult` | never read `r.result`; last-expr → `DisplayEvent` |
| `history[-1]` in `quiet()` | `;` in cell B suppresses cell A's last-expr | `quiet()` → `False` |
| `_`/`_oh[N]` via `execution_count` | wrong value in `_`/`Out[]` | `update_user_ns` no-op; `cache_size=0`; agent told not to use `_` |
| `sys.excepthook` swap in `run_code` | leaks `shell.excepthook` process-wide | `_showtraceback` writes to real stderr out-of-cell, so leak is cosmetic |

## M1.1 integration work (out of spike scope)

1. **Route `DisplayEvent` through `transcript()._event()`.** The
   `on_display → session._enqueue(ev.wire())` shortcut breaks reconnect and
   persistence — display events never reach `session.events`, so
   `push_full_state()` after a browser reconnect ships zero orchestrator
   outputs and the client can't render pending gate cards → soft-lock.
   Make `DisplayEvent` an inspect `Event` subclass, `uuid = display_id`
   (so `dh.update()` reuses M0's `is_update = ev.uuid in self.events`
   path), run each cell inside a `span(type="orchestrator")`, and register
   an `"orch"` role in `session.span_role`. `on_display` survives as a
   test seam only.

2. **`Session.view()` gains orchestrator state:** `pending_gates:
   list(kernel.pending)`, `bg: {tid: status}`, and `notifications` —
   otherwise reconnect loses the whole orchestrator column. `_dispatch`
   gains `case "approve": session.kernel.resolve(...)` (add to `UNLOCKED`).

3. **Singleton is a *process* boundary, not a Session boundary.**
   `IPython.display.display` resolves via `InteractiveShell.instance()`;
   a second `OrchestratorKernel` repoints `display_pub.kernel` and
   `sys.stdout` and both sessions' outputs cross. `server.py` currently
   hosts many `Session`s per process → for M1.0, `_get_or_create` must
   raise if a second session wants a kernel. Multi-session M1 =
   subprocess-per-orchestrator (jupyter's model).

4. **`wb.run_audits` cancellation.** `k.cancel(turn_id)` cancels the cell
   task, but `Branch.run()` children spawned via `asyncio.create_task`
   inside the cell survive it. Either wrap the cell body in a `TaskGroup`
   so cancel propagates, or `RunHandle` registers its branches with the
   kernel for explicit teardown.

5. **Subprocess / C-level stdout bypasses `_CellStream`.** `subprocess.run`
   inside a cell writes to real fd 1. ipykernel does `os.dup2` + a watch
   thread. Defer unless the agent needs to shell out.

## M1-NOTEBOOK.md amendments

- §Risks: upgrade "singleton — fine for one orchestrator per Session" to
  "**one orchestrator per process**; multi-session M1 is subprocess-backed."
- §Kernel: `transformed_cell = shell.transform_cell(code)`, not `= code`.
- §Kernel: `store_history=False` (don't pollute `~/.ipython/history.sqlite`).
- §Background execution: agent prompt gains "don't rely on `_` / `Out[]`".
- §Renderer table: model-facing text prefers `text/markdown` over
  `text/plain`; `go.Figure`/`plt.Figure` get a compact `text/plain`
  formatter registered on the shell.
- §Scenario deltas: seed `pd.set_option("display.max_colwidth", 200)` etc.
  so DataFrame columns the agent needs to `.query()` on aren't truncated.
