# M1 orch column — UI iteration findings (2026-07-02)

Consolidated from three visual reviews of `screenshots/m1/*.png` @ `5ae3226`.
Mockup source of truth: `audit-workbench-design/mockups-m1/shared.css` +
`scenario-{a,b,d}.html`. Re-run `uv run python -m workbench._screenshot_m1`
after each fix batch.

## Done

- Traceback wire (`kernel._settle` emits `{kind:"traceback"}` DisplayEvent)
- `pd.set_option` caps (max_columns=12, max_rows=20, max_colwidth=80)
- Card body CSS port + plotly bundle + pandas table + StartView tabs (in flight)

## Structural (component/tsx — not CSS)

Ranked by visual/interaction impact. File owners noted.

### Column header (`OrchColumn.tsx`)
1. **`waiting` status** — when `gate.pending.length > 0`, header shows
   `waiting on you` (not `running`). Backend: `Orchestrator.view()["status"]`
   = `"waiting" if kernel.gate.pending else self.status`; add `"waiting"` to
   `view.py` `Status` literal + `wire.ts`.
2. **`N waiting` pill** — when `pending_gates.length > 0`, header shows a
   clickable pill that scrolls to the first unresolved gate. Popover lists
   each gate's `.gate-desc` + per-row approve/deny + `Approve all (N)` for
   `run_proposal`s (not `ask_human` — those need a value).
3. **Header content** — `ORCHESTRATOR · {model short-name} · t{turn} ·
   {status text}  [→bg if running] [⏹] [⏭] [▶]` grouped left. Status dot
   tri-state: grey idle / pulsing-blue generating / pulsing-amber
   kernel-executing / purple gate-pending.
4. **`⏹` interrupt** — sends `{t:"cancel_cell", turn: current}` (or a new
   `{t:"interrupt", target:"orch"}` if we want kernel-level cancel).

### Composer (`OrchColumn.tsx`)
5. **Addressee chip** — `<span class="composer-to">to: orchestrator</span>`
   top-left of textarea (color-matched to orch accent), so it's
   distinguishable from M0's auditor composer even after typing clears the
   placeholder.
6. **Running sub-hint** — when `isRunning && hasText`: `cell running — this
   will be read after turn N · <a>send now (background current cell)</a>`
   where the link fires `detach_cell` then `orch_send`.
7. **Placeholder** — `Instruct the orchestrator…` (not "Ask").

### Cards (`cards/*.tsx`)
8. **`.out-head` labels** — drop `KIND` word. Gates: icon only. Run/scan:
   icon + `{task_name}` (lowercase) + faint `{id[:8]}`. Receipts: icon +
   verb.
9. **PromptCard** — vertical `.ask-opts` when any option ≤2 chars or ≤3
   options; number-key hints `[1] y`; free-text row gets a trailing send
   button. Resolved: keep a slim `.out-head` with `bi-person-check` +
   `you answered · HH:MM`.
10. **RunProposalCard** — `deny` far-left, `flex:1` spacer, `edit`/`approve`
    right. Deny-reason input renders *above* the bar (approve stays
    visible). Seed rows: leading `<input type="checkbox">` instead of
    click-to-strike.
11. **RunCard rows** — `role="button" tabIndex=0`; `.ar-id` as
    `<a class="qref">`; leading `bi-record-fill row-dot {status}`;
    conditional `title` (`running` → "watch live in desk", else "open
    transcript in desk"); post-click `in desk →` chip for ~2s. Error rows
    show exception class inline (`ValueError`) not the word `error`.
12. **RunCard counter** — `{done}/{total}` + thin bar; `+{errored}` in
    danger only when >0; `· {elapsed}` when finished.

### `OrchTurn.tsx` / `Output.tsx`
13. **CodeCell collapse** — label from `collapsed`: `show {loc-6} more
    lines` / `collapse`. `useEffect` re-eval when `loc` first crosses 6.
    `bi-chevron-{down,up}` icon.
14. **`→ bg` label** — `run in background` (not `→ bg`). After detach,
    `cc-bg-chip` `title="running in background; result will post here"`.
15. **Stable-update badge** — thread `stable`+`updateCount` through *all*
    `Output` branches (not just HTML). `<span class="out-live">live</span>`
    corner badge on any `stable` output; increments on `dh.update()`;
    `just-updated` class for one frame.
16. **Dedupe** — suppress a second card in the same turn whose
    `payload.id` matches an already-mounted stable card's `payload.id`.
17. **Plotly loading** — `<div class="plotly-loading">rendering figure…
    </div>` placeholder until `window.Plotly` mounts.
18. **Traceback chip** — `cell-status err` chip in `.cc-head` when the
    cell errored (visual even before the traceback body renders).
19. **Prose soft-cap** — `AssistantProse` max-height ~12 lines with
    `show more ↓` fade.

## Process for each iteration round

1. `git pull` / check tree clean
2. Apply top-N fixes from this doc (or from the previous round's review)
3. `npx --yes pnpm@latest tsc --noEmit` (must be clean modulo baseline)
4. `uv run python -m workbench._screenshot_m1` → regenerates
   `screenshots/m1/`
5. Read each new screenshot; note what's fixed vs still broken
6. Commit `frontend-wb/` + `_screenshot_m1.py` + this doc; push
7. Update this doc's "Done" section
