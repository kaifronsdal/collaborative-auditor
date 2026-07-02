# M1 orch column — UI iteration findings (2026-07-02)

Consolidated from three visual reviews of `screenshots/m1/*.png` @ `5ae3226`.
Mockup source of truth: `audit-workbench-design/mockups-m1/shared.css` +
`scenario-{a,b,d}.html`. Re-run `uv run python -m workbench._screenshot_m1`
after each fix batch.

## Done

- Traceback wire (`kernel._settle` emits `{kind:"traceback"}` DisplayEvent)
- `pd.set_option` caps (max_columns=12, max_rows=20, max_colwidth=80)
- Card body CSS port + plotly bundle + pandas table + StartView tabs
- **r1 (94b1c8a)** structural pass: items 1–3, 5–14, 18–19 in tsx
- **r1 (css)** header layout (`.head-left`/`.head-sep`/`.head-model`/
  `.head-turn`/`.head-status-*`, `.rl-dot-{gen,exec,gate,idle}`,
  `.head-gate-jump`/pop); composer `to:` chip; `.cell-status.err` pill;
  RunCard `.fx-bar`/`.row-dot-*`/`.out-task`/`.out-id`/`.out-live`/
  `.ar-in-desk`; `.er-id.qref` de-boxed; PromptCard `.ask-opt-own-wrap`/
  `.ask-opt-send`/`.ask-opt-key` + hide `.out-head` on gated ask;
  `.prose-capped`/`.prose-more`; RunProposal `.sp-*`/`.gate-deny-reason`.
- **r1** `Output.tsx`: `kind:"traceback"` renders `.out.traceback` (not JSON
  fallback); `OrchTurn` suppresses redundant `<Traceback>` when the display
  card already covers it.
- **r1** hide script-only `.out.html` (`:not(:has(> :not(script)))`) — kills
  the two empty boxes plotly's CDN-loader emits above the figure.
- **r1 (infra)** `_screenshot_m1` was broken by `a73f0d5`'s lockfile shrink:
  `frontend-wb`'s `pnpm install` (which owns ts-mono packages via
  `pnpm-workspace.yaml`) emptied their `node_modules/`. Fixed by re-running
  `pnpm install` in `inspect_ai/.../ts-mono/`; `vite.config.ts` now aliases
  `@tsmono/*` subpaths + pins `esbuild.tsconfigRaw` (string) so the dev
  server survives either state.

## Backend-needed

- `RunHandle._repr_mimebundle_`: `elapsed` (formatted `"2m14s"`), per-row
  `error` (exception class name) — RunCard already reads both when present.
- `Orchestrator.view()["status"] == "waiting"` reaches the WS stream on gate
  open (currently only on next `view()` push; screenshot harness bypasses).

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

## New classes for `orch.css` (round-1 structural pass)

Emitted by the tsx pass; need styling. Grouped by component.

- **`OrchColumn` header** — `.head-left` (flex row, gap 6px),
  `.head-title` (uppercase, tracking), `.head-sep` (faint `·`),
  `.head-model` / `.head-turn` (mono xs, ink-dim), `.head-status` +
  `.head-status-{idle,paused,running,waiting,ended}` (waiting → gate accent).
  `.rl-dot-gen` (pulsing blue), `.rl-dot-exec` (pulsing amber),
  `.rl-dot-gate` (solid purple) — extend the existing `.rl-dot-*` set.
  `.head-bg-btn` (text-button, xs).
- **Gate pill/popover** — `.head-gate-wrap` (relative), `.head-gate-jump`
  (pill: gate-bg, gate-text, rounded), `.head-gate-pop` (absolute dropdown,
  card bg, shadow), `.hgp-row` (flex, gap), `.hgp-desc` (flex:1, truncate,
  cursor:pointer), `.hgp-btn` / `.hgp-btn.deny` / `.hgp-all`.
- **Composer** — `.composer-to` (chip top-left of textarea, orch accent),
  `.composer-hint-running` (`a { text-decoration: underline }`).
- **`OrchTurn`** — `.asst-prose.prose-capped` (`max-height: ~12lh`,
  overflow hidden, bottom fade mask), `.prose-more` (link-button, centered).
  `.cell-status.err` (danger chip in `.cc-head`).
- **`PromptCard`** — `.gate-waiting` (subtle pulse on `.out.gated`),
  `.ask-opts-vert` (`flex-direction: column; align-items: stretch`),
  `.ask-opt-key` (mono, ink-faint), `.ask-opt-own-wrap` (flex row, input
  flex:1), `.ask-opt-send` (icon button, disabled → opacity .4).
- **`RunProposalCard`** — `.sp-check` (accent-color: gate), `.sp-seed`
  (flex:1 truncate), `.sp-row-struck` (line-through, opacity .55),
  `.gate-deny-reason` (full-width input above `.gate-bar`, danger border).
  `.gate-bar` layout is now `[deny] [.gate-reason flex:1] [edit] [approve]`
  — existing `.gate-reason { flex:1 }` already provides the spacer.
- **`RunCard`** — `.out-task` (lowercase, ink), `.out-id` (mono, ink-faint),
  `.out-live` (xs badge, orch accent, top-right), `.fx-bar` (thin track,
  `> i` fill = orch accent), `.stat.err` (danger), `.row-dot` +
  `.row-dot-{running,done,error,stopped}` (8px, running pulses),
  `.ar-in-desk` (transient chip, orch accent, fade-out ~2s).

## Open after r1 (screenshot review)

- **04** RunCard duplicated (item 16 — `Output.tsx` owner). The stable
  `display()` inside `run_eval` and the last-expr `h` both mount.
- **05** plotly x-axis labels clipped at bottom edge (figure height 260 but
  container gives no bottom padding on `.out.bare.plotly-host`).
- **02** DataFrame `.out.html` caption `3 rows × 2 columns` renders in body
  font — should be `.df-more` xs faint (pandas emits `<p>`, not our class).
- **02** `'v2'` plain-text output floats with no context; mockup wraps
  reprs as `.repr` inline block.
- **03** `run in background` cc-head button icon (`bi-layer-backward`) is
  large relative to text; drop icon or size to 10px.

## Process for each iteration round

1. `git pull` / check tree clean
2. Apply top-N fixes from this doc (or from the previous round's review)
3. `npx --yes pnpm@latest tsc --noEmit` (must be clean modulo baseline)
4. `uv run python -m workbench._screenshot_m1` → regenerates
   `screenshots/m1/`
5. Read each new screenshot; note what's fixed vs still broken
6. Commit `frontend-wb/` + `_screenshot_m1.py` + this doc; push
7. Update this doc's "Done" section
