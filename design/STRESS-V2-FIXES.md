# Stress-test wave v2 findings + fixes

Post-A1-A4 (2026-07-09). 5 agents: adversarial-UI-v2, delay-injection,
reconnect-storm, deadlock-hunt, b-wide-graft-load.

## Results summary

| agent | verdict | headline |
|---|---|---|
| Reconnect storm | 2/2 PASS | Solid. Gap: no auto-reconnect on `onclose`. |
| Deadlock hunt | 7 hazards | H7 livelock (`rewind`); H5 drop-unrecoverable |
| Delay injection | s4 1/3, s5 3/3 fail | `{t:"status"}` writes `version` but not `GUARDED` → drops |
| b-wide graft load | all pass | 0.08ms/call, deterministic, invariants hold |
| Adversarial UI v2 | 4/5 (s13 fail) | `Approve all` misses concurrently-opening gate |

## P0 — livelock / correctness

### H7 — `rewind` holds `_dispatch_lock` across uncancellable bg-cell
`server.py:1193` — `Orchestrator.rewind` does `kernel.cancel(tid)`
then `await asyncio.gather(*tasks)`. A cell in blocking sync code
(`time.sleep`, pandas, C-ext) never observes cancel → `gather`
blocks → lock held indefinitely → all locked dispatches hang.
**Fix:** wrap `Orchestrator.rewind`'s `gather` in
`anyio.move_on_after(2.0)` (best-effort — the cell eventually
finishes and its output is discarded via the rewound-uuid filter);
or spawn the gather as a bg task (R5 pattern) and let dispatch
return. Prefer the timeout: rewind's contract is "state.messages
truncated when this returns", which the bg-spawn would break.

### H5 + version-guard — dropped frames unrecoverable post-A3
Two independent problems that combine:

**H5a** — `session._enqueue` drops on 2048-slot overflow
(`session.py:359`); the drop comment says "next `state` snapshot
resyncs" but A3 removed mid-session `{t:"state"}`. `pool_sent`
(`:379`) and `version` (`:333`) are advanced *before* `_enqueue`,
so a dropped `{t:"batch"}` leaves a permanent client-side pool
hole; a dropped `{t:"branch_created"}` leaves the branch's
`span_role_delta` unknown → all its events unbucketed forever.
**Fix:** `_enqueue` returns `bool` (False on drop). On drop, set
`self._desync = True`. Drain task, next iteration, if `_desync`:
`push_full_state()` to every connection, clear the flag. This
restores the "next snapshot resyncs" contract with one full-state
push per overflow episode, not per dropped frame. Also: don't
advance `pool_sent` until `_enqueue` succeeds (move `:379` after
the enqueue check).

**H5b** — `{t:"status"}` writes `state.version = msg.v`
(`session.ts:629`) but is *not* in the `GUARDED` set. Under
per-frame delay (artificial — real WS is TCP-ordered), a `status`
at v=N+1 arriving before a `batch` at v=N bumps `version` and the
guard drops the batch. Real-world impact: only via H5a's overflow
(dropped frame → gap → next frame's `v` skips). **Fix:** either
add `status` to `GUARDED` (it's cheap to drop a stale one), or
stop writing `version` from non-`GUARDED` frames. Prefer the
former.

## P1 — product bugs

### s13 — `Approve all` misses concurrently-opening gate
`OrchColumn` `Approve all` iterates `usePendingGates()`
(client-side snapshot) and sends `{t:"approve", display_id}` per
gate. A gate opening between the snapshot and the last send is
missed → orch stuck `waiting`. **Fix:** new `{t:"approve_all"}`
handler in `server.py` that iterates `orch.gate.pending`
server-side; `OrchColumn` sends one message. `useIsPending(c =>
c.t === "approve_all")` disables the button.

### H1 — `_do_end` kills whatever is running *now*
`server.py:747-748` — bg task calls `_stop_running_branches(
session)` with `only=None`. A fork spawned between `end` dispatch
and `_do_end` acquiring the lock gets killed. **Fix:** capture
`branch_id` at dispatch, `_do_end(session, branch_id)` calls
`_stop_running_branches(session, only={branch_id})`.

### H2 — `_do_end` hangs on `end_conversation()` rendezvous
`server.py:744` + `petri _channel.py:77` — zero-buffer stream; if
the branch task is cancelled first (by a concurrent fork), the
consumer is dead and `send` blocks forever. **Fix:**
`with anyio.move_on_after(2.0): await channel.end_conversation()`.

## P2 — leaks / hygiene

### H3 — `close()` awaits drain flush under `_dispatch_lock`
`session.py:191-192` — `await self._run_task` waits for `drain()`
to flush 2048 queued frames; second-connection dispatches stall.
**Fix:** `Session.close()` should not be called under
`_dispatch_lock` — but `fork_orchestrator` (`server.py:1188`)
does. Move the `await self.close()` in `fork_orchestrator` to
after the lock is released (spawn as bg task), or `close()` wraps
the drain-await in `move_on_after(1.0)`.

### H4 — kernel bg cells survive `close()`
`Orchestrator.close()` reaps bash procs but not `kernel.bg` cell
tasks. **Fix:** `close()` also `for t in kernel.bg.values():
t.cancel()`.

### Auto-reconnect
`session.ts` `onclose` clears `{ws:null, sessionId:null}` but
doesn't reconnect. **Fix:** `onclose` schedules
`setTimeout(() => connect(sid), backoff)` with exponential
backoff (1s→2s→4s→…→30s), reset on successful `onopen`.

## Not a bug

- **b-wide graft** — all invariants hold under 5 concurrent
  candidates × rollback × mid-poll fork. 0.08ms/call.
- **`useIsPending`** — holds correctly under 500ms delayed ack.
- **s9-s12** — rapid switchBranch, session-fork mid-turn,
  orch+M0 concurrent, click-every-gantt-lane all pass.
- **Reconnect** — `pending:[]` reset works; all events survive
  8× close+reconnect cycles.

## Order

P0 first (H7, H5a+b — one server.py + one session.py + one
session.ts commit). Then P1 (s13, H1, H2 — one server.py commit).
Then P2 (H3, H4, auto-reconnect — separate commits).
