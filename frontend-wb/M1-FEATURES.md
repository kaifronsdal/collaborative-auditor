# M1 orchestrator — feature batch (2026-07-02)

Selected from the polish brainstorm. Per-block actions use the
**claude.ai idiom** (hover-reveal horizontal icon row under each
block), not a `⋯` overflow menu.

## 1. Streaming prose — ALREADY WIRED (verify with real model)

Investigated: the path is complete end-to-end. inspect
`update_active_model_event_output()` mutates `ModelEvent.output` +
calls `_event_updated()` (throttled) → `Session._on_event` ships
`{t:"update"}` → store replaces by uuid → `eventsToOrchTurns` reads
`.output.choices[0].message.content` live → `AssistantProse` renders
with `.cursor` while `pending`. The `.shimmer-bubble` is only for the
pre-first-token gap (empty output), not during streaming.

**Remaining:** verify `assignByRole` (events.ts:95-99) creates a new
array on update (not in-place mutate) so the `useMemo([events])` in
`eventsToOrchTurns` re-fires. Screenshot harness (mockllm) doesn't
stream — verify on the e2e worker with a real model + browser, or add
a mockllm streaming variant.

## 2. Rewind to turn N

**Backend** (`orchestrator.py`, `server.py`, `session.py`):
- `Orchestrator.rewind(turn: int)`: cancel any running cell; truncate
  `self.state.messages` to before turn N's assistant message; clear
  `kernel.outputs[turn:]`; emit a synthetic `InfoEvent` marking the
  rewind point; re-arm step gate to `paused`.
- `session.events` for the orch span: mark events after the rewind
  point with `data.rewound = True` (don't delete — the wire's
  version-monotonicity forbids removal). Frontend filters them.
- `server._dispatch`: `case "rewind": orch.rewind(msg["turn"])`.
- Kernel `user_ns` is *not* rolled back (same as Jupyter "run all
  above" — bindings persist). Note this in the confirm dialog.

**Frontend** (`OrchTurn.tsx`, `wire.ts`):
- Icon-row action `bi-arrow-counterclockwise` on each turn.
- Confirm modal: `Restart from turn {N}? Later turns will be
  discarded. Kernel bindings are kept.` → `send({t:"rewind", turn})`.
- `eventsToOrchTurns` drops events with `data.rewound`.

## 3. RunCard sort/filter — write locally

Investigated: `@tsmono/inspect-components` exports content/usage
widgets only; sort/filter is ag-grid-coupled inside the inspect *app*
(Redux, `GridState`, rich `SampleRow` schema). Not reusable. Local
~40-line implementation:
- Column headers clickable (`id`/`score`/`status`/`turns`) → toggle
  sort asc/desc. `useState<{col, dir}>`.
- Filter chip row above rows: `all · running · done · error` +
  free-text filter on `id`/`seed`.
- Applies to both `run` and `scan` variants of `ProgressCard`.

## 4. Scroll anchor + `↓ N new`

**Frontend** (`OrchColumn.tsx`):
- Track `atTail` (existing follow-tail logic). When `!atTail` and
  `turns.length` grows, increment `unseenCount`.
- Floating pill bottom-center: `↓ {unseenCount} new` → scrolls to
  tail + resets. Hidden when `atTail`.
- Existing follow only auto-scrolls when already at tail — keep.

## 5. Cell timing

**Backend** (`kernel.py`): `TurnResult.duration: float` (monotonic
delta around `run_cell_async`). `_settle` includes it in the
turn-done notification, and `python_tool` returns it in the tool
result text (so the model sees it too: `[2.3s]` suffix).

**Frontend** (`OrchTurn.tsx`): `.cc-head` right side, faint, next to
`turn N`: `{duration.toFixed(1)}s` when settled.

## 6. Copy + per-block icon row (claude.ai style)

**Frontend** (`OrchTurn.tsx`, `Output.tsx`, new `BlockActions.tsx`):
- `<BlockActions>` = hover-reveal horizontal row under the block,
  right-aligned, `gap: 4px`, xs icon buttons. Fades in on parent
  `:hover`.
- **Per-turn** (under `.asst-prose`): `bi-clipboard` (copy prose md),
  `bi-arrow-counterclockwise` (rewind here — §2).
- **Per-code-cell** (in/under `.cc-head`): `bi-clipboard` (copy code).
- **Per-output** (under each `.out`): `bi-clipboard` (copy — for
  DataFrames copies TSV via `navigator.clipboard.write`, for
  text/plain copies text, for cards copies the WB_MIME JSON).
- Copy shows `bi-check2` for 1s after success.

## 7. Variable-inspector hover tooltip

**Backend** (`kernel.py` / `orchestrator.py`): after each cell
settles, compute `ns_summary: dict[str, str]` = `{name:
f"{type(v).__name__} · {short_repr(v)}" for name, v in user_ns.items()
if not name.startswith('_') and name not in BUILTINS}`. Cap repr to
60 chars; special-case `RunHandle`/`ScanHandle`/`DataFrame` for
useful summaries (`RunHandle · 3/3 done`, `DataFrame · 40×5`). Ship
in the orch `view()` payload (or a dedicated `{t:"ns", v:...}` on
change).

**Frontend** (`OrchTurn.tsx`): tokenize `.cc-gist` — wrap each
`\b[a-zA-Z_]\w*\b` that's a key in `ns_summary` in `<span
class="cc-var" title={ns_summary[name]}>`. Native tooltip is enough
for v1; a Popover with the full repr later.

## 8. Mini score histogram

**Backend** (`run.py` `RunHandle._repr_mimebundle_`): when
`finished`, include `scores: list[float | None]` (first numeric
score per row, from `SampleRow.scores`).

**Frontend** (`ProgressCard.tsx`): when `finished && scores`, render
a 40px-tall SVG sparkline below the counter line: 10 bins,
`rect` per bin, fill `--accent`. Hover a bin → `title="{lo}-{hi}:
{n}"`. Click a bin → filter rows to that range (ties into §3).

## 9. Per-sample stop

**Backend** (`server.py` `_dispatch`): `case "stop_sample":
stop([msg["id"]], hard=msg.get("hard", True))`.

**Frontend** (`ProgressCard.tsx`): row hover → `bi-stop-fill` icon
right of status, only when `row.status === "running"`. Click →
`send({t:"stop_sample", id: row.id})`. Optimistic: row status →
`stopping` (grey pulse) until next `dh.update`.

## 11. Interrupt-and-send (Claude Code / Cursor Escape pattern)

User types feedback while a cell is running and wants it read *now*,
not queued. Distinct from `cancel_cell` (kill, no message) and
`detach_cell` (background, message queued for after).

**Backend** (`kernel.py`, `server.py`, `orchestrator.py`):
- `kernel.interrupt(turn_id)`: cancel the cell task; `_settle` on
  `CancelledError` when `_user_interrupted[turn_id]` is set produces
  `TurnResult(text=f"[interrupted by user after {dur:.1f}s]\n{partial}",
  success=False, error=None)` — *not* a traceback. Partial outputs
  already in `kernel.outputs[turn_id]` are preserved.
- `server._dispatch` `case "interrupt_and_send"`: `kernel.interrupt(
  msg["turn"])` + `orch.queued.append(ChatMessageUser(msg["text"]))`.
  The queued message drains into the *same* generate that reads the
  interrupted tool result.

**Frontend** (`OrchColumn.tsx`):
- Composer primary button while `cellRunning && hasText`: instead of
  `disabled`, becomes **"interrupt & send"** (icon `bi-stop-fill` +
  `bi-send`, or a split button). `send({t:"interrupt_and_send",
  turn, text})`.
- The existing `send now (background)` link stays as the softer
  option in the hint row.
- `⏹` alone (no text) stays as pure cancel.

## 10. Not `⋯` — icon row per block

Covered by §6. No overflow menus. Every action is a visible icon on
hover, in a consistent row position (bottom-right of the block).

---

## File ownership (parallel agents after current pair lands)

| Agent | Features | Files |
|---|---|---|
| **streaming** | §1 | `session.py` (verify), `orchestrator.py`, `OrchTurn.tsx`, `lib/events.ts` |
| **kernel** | §2 rewind, §5 timing, §7 ns_summary | `kernel.py`, `orchestrator.py`, `server.py`, `wire.ts` |
| **column** | §4 scroll anchor, §6 BlockActions + copy | `OrchColumn.tsx`, `OrchTurn.tsx`, `Output.tsx`, new `BlockActions.tsx`, `orch.css` |
| **progress** | §3 sort/filter, §8 histogram, §9 per-sample stop | `ProgressCard.tsx`, `run.py` (scores), `server.py` (stop_sample), `orch.css` |

`orchestrator.py` is shared by streaming + kernel agents — kernel
agent owns it; streaming agent only *reads* to verify the generate
path and edits `session.py`/frontend if needed.
