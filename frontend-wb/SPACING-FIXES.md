# M1 orch column — spacing/alignment fix batch (2026-07-03)

Consolidated from two pixel-measured audits of
`screenshots/m1-impl-0702/*.png`. Apply to `orch.css` + `styles.css`;
two items need tsx (`OrchColumn.tsx` scroll-new DOM move, `GateCard.tsx`
inline-style → CSS).

## Highest-impact (fix first)

| Selector | Issue | Fix |
|---|---|---|
| `.turn > .ba-host:first-child > .asst-prose` | 12px asymmetry around `.turn-sep` — prose `mt:18` can't collapse through `.ba-host` flow-root | `margin-top: 0;` + `.turn-sep { margin-bottom: 34px; }` |
| `.cc-head .cc-bg-chip/.cc-bg-btn/.cell-status/.cc-dur/.turn-no` (orch.css:578-582) | `gap:8` + `margin-left:8` → 16px double-gap on right cluster | delete `margin-left: 8px` (keep `flex-shrink:0`) |
| `.ba-host:has(> .asst-prose)` | hover icon row 0px clearance to next `.code-cell` | `margin-bottom: 6px;` |
| `.composer-controls` | first icon 7px right of textarea rail; `gap:2` too tight; buttons 28px vs `.primary` 32px | `margin-left: -7px; gap: 4px;` + `.composer-controls button { width: 32px; height: 32px; }` |
| `.scroll-new` | `bottom: 96px` fixed — inside composer when textarea auto-grows | **DOM**: move into `.composer` (`OrchColumn.tsx`); CSS: `top: -40px; bottom: auto;` (`.composer` already `position:relative`?) |
| `.pc-cols` | headers don't align with row columns (headers flow `gap:14` from x=14; rows are dot@14→id@32→seed@106) | `padding-left: 32px;` + `.pc-col:first-child { width: 74px }` (matches 64px id + 10px gap) |
| `.audit-row, .eval-row, .fx-more` | `padding-inline: 12px` vs everything else at 14px → 2px rail stagger | `padding: 7px 14px;` |
| `.eval-row .er-score` | both `.ar-seed` + `.er-score` are `flex:1` → half-row gap on short seeds | `flex: 0 1 auto; margin-left: auto; max-width: 55%; text-align: right;` |
| `.cite-q .sp-check` | `margin-right:8` + `gap:10` → 18px double-gap; `margin-top:3` → 2px low vs qref | `margin: 1px 0 0 0;` |
| `.out.finding .out-head` | `border-bottom` above card's own bottom border → 2px double rule | `border-bottom: none;` |
| `.seed-preview.collapsed .sp-row:nth-child(-n+3)` | forces `display:block` — kills flex layout | `display: flex;` |

## Column shell (audit A)

| Selector | Fix |
|---|---|
| `.orch-head .head-left` | `min-height: 20px;` |
| `.head-gate-jump` | `line-height: 1.6; padding: 0 10px;` |
| `.orch-head .rl-dot` | `margin: 0 0 0 2px; position: relative; top: -1px;` |
| `.composer` | `padding: var(--sp-2) 14px;` (10/10, was 10/14/6) |
| `.orch-col .turn` | `margin: 0;` (dead — collapses into sep) |
| `.ask-wrap + .ba-host > .asst-prose` | `margin-top: 0;` |
| `.asst-prose` | `margin-left: 0; margin-right: 0;` |
| `.ba-host:has(> .asst-prose) > .block-actions` | `left: 0;` |
| `.code-cell .cc-head .turn-no { margin-left: auto; }` | delete (dead — overridden) |
| `.code-cell .cc-head` | `min-height: 30px; padding: 5px 12px;` |
| `.cc-bg-btn` | `padding: 1px 8px;` |
| `.cc-chev` | `position: relative; top: 0.5px;` |
| `.code-cell` | `margin-bottom: 10px;` |
| `.code-cell pre` | `padding-right: 34px;` (block-actions overlap) |
| `.code-cell ~ .ba-host + .ba-host` | `margin-top: 8px;` (10/8 rhythm — `.out+.out` never matches through `.ba-host`) |
| `.out.bare table.dataframe th:first-child, td:first-child` | `padding: 4px 6px;` |
| `.out.bare table.dataframe + p` | `margin: 6px 0 0;` |
| `.out-stream` | `padding: 4px 0 2px;` |
| `.ba-host:has(> .out.bare) > .block-actions` | `bottom: -4px; right: 0;` |

## Cards (audit B)

| Selector | Fix |
|---|---|
| `.start-controls` | `padding-top: 14px;` |
| `.mp-field` | `padding: 5px 8px 5px 12px;` |
| `.start-tab-sub` | `min-height: 2.7em;` |
| `.ask-opts` | `padding-bottom: 14px;` |
| `.ask-other` | `padding: 5px 0;` |
| `.ask-opt.own` | `padding: 4px 2px;` |
| `.gate-desc:has(+ .gate-config)` | `border-bottom: none; padding-bottom: 2px;` |
| `.gate-config` | `border-bottom: 1px solid var(--border); padding-top: 0; padding-left: 36px;` |
| `.sp-check` | `margin: 0 8px 0 0;` |
| `.sp-toggle i` | `width: 13px; text-align: center;` |
| `.cite-quotes` | drop `border-top` |
| `.out-head .fx-dot, .audit-row .row-dot, .eval-row .row-dot` | `top: 0;` |
| `.pc-chip:first-child` | `padding-left: 0;` |
| `.pc-chip .sep` | `margin: 0 6px 0 -2px;` |
| `.pc-hist-wrap` | `padding: 6px 14px 6px;` |
| `.out:not(.gated) > .gate-desc` | `padding: 8px 14px 0;` |
| `.ar-stop` | `margin-left: -28px;` + `.audit-row:hover .ar-stop, .eval-row:hover .ar-stop { margin-left: 0; }` |
| `.rd-role` | `left: 14px;` + `.rd-msg { padding-left: 60px; }` |
| `.rd-foot .iv-url` | `flex: 1; min-width: 0;` |
| `.out-head .out-actions a` | `padding: 3px 5px; border-radius: 4px; cursor: pointer;` + hover |
| `.out.answered > .out-head` | `padding: 7px 2px;` |
| `.out.bare > .out-plain.out-result` | `padding: 2px;` |
| `.out-head` | `padding: 7px 14px;` + `.out-actions { margin-right: -2px; }` |
| `.gate-desc > i.bi` (tsx inline style) | move to CSS: `margin-right: 8px; color: var(--gate-text);`; drop inline `style` in `GateCard.tsx` |

## While you're in orch.css

Consolidate the three `/* -- {agent} additions -- */` EOF blocks into
their proper sections and delete the marker comments (deferred from
the cleanup pass).

## Verify

`tsc --noEmit` (2 baseline); `uv run python -m workbench._screenshot_m1`;
read `02/03/04a/04b/07/08/09` shots; copy PNGs to
`~/GitHub/audit-workbench-design/screenshots/m1-impl-0702/`.
