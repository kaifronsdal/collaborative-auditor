# M1 orch column — element-level audit (2026-07-02)

Consolidated from four "does this element earn its space" reviews +
user feedback on control location. Supersedes UI-ITERATION.md §Structural.
Screenshots: `screenshots/m1/*.png` @ `105ee23`.

## Governing principle: location follows interaction

- **Header** = passive status. What is this column, what state is it in.
  You *glance* at it. No action buttons.
- **Composer footer** = active controls. Your cursor lives here; every
  turn ends with you looking at this row. Transport (`⏹ ⏭ ▶/⏸`) + send
  live here, matching M0's `DeskView` composer.
- **In-content** = per-item actions. Approve/deny on the gate card;
  open-in-desk on the RunCard row; `run in background` on the running
  cell's own head. Act where the thing is.

The r1 structural pass put transport in the header (Jupyter-toolbar
model). That's wrong for a chat column: the user's eye is at the tail,
not the top. M0 already gets this right — match it.

## Redundancy map (facts stated N× — collapse to 1×)

| Fact | Currently at | Keep only |
|---|---|---|
| "this is the orchestrator" | header title, `to:` chip, placeholder, `↳` hint | header title (+ placeholder for first-use) |
| "waiting on you" | `.rl-dot-gate`, status text, `N waiting` pill, card `.gate-tag` | pill (absorbs dot); card border color |
| play/pause | header `▶/⏸`, composer morph | composer footer `▶/⏸` |
| detach current cell | header `→bg`, per-cell button, composer `send now` | per-cell button (+ composer link as the queued-msg escape) |
| turn number | header `t{N}`, per-cell `turn N` | per-cell |
| RunProposal seed count | `.out-meta`, `.gate-reason`, `.sp-toggle` | `.sp-toggle` |
| RunCard liveness | `.fx-dot`, `.out-live`, wrapper badge, bar<100% | `.fx-dot` + bar |
| open-full (excerpt/transcript) | header icon, footer link | footer link |

---

## §A — Bugs + backend data (do first)

### Backend (`src/workbench/m1/`)

**`kernel.py` `_settle`** — traceback payload:
- Emit `{kind:"traceback", ename, evalue, frames:[{file,lineno,line}], text}`.
- `frames` = `traceback.extract_tb(err.__traceback__)` filtered to drop
  entries whose `filename` matches `IPython/`, `asyncio/`, `anyio/`,
  `<frozen`, `concurrent/futures/`, or contains `run_cell`/`exec(code_obj`.
- `text` stays (full formatted tb) for the expandable body.

**`read.py` `TranscriptRef`** — preview slice:
- `messages[-3:]` not `[:3]` (last 3 = elicited behavior; first 3 =
  system prompt boilerplate). If `at` is set: `messages[max(0,at-1):at+2]`.
- Ship `n_messages: len(messages)` in WB_MIME.

**`read.py` `Excerpt`** — already ships `at_idx`; no change. (Frontend
bug.)

**`run.py` `RunHandle._repr_mimebundle_`**:
- `elapsed` unconditionally (not only when `finished`).
- Per-row `turns` already in `SampleRow`; ensure it's in the row dict.

**`run.py` `RunProposal._repr_mimebundle_`**:
- Add `config: {"model": ..., "max_turns": ..., "n_per_seed": ...}` —
  the decision-relevant fields.
- `seeds` already ships `[{id, text}]`; keep. (Frontend typing bug.)

**`kernel.py` `Prompt`**:
- `answered_at: str | None` (ISO); set in `resolve()`.

### Frontend (`cards/*.tsx`, `Output.tsx`)

**`RunProposalCard.tsx`** — `seeds` typing:
- `seeds: {id: string; text: string}[]` (not `string[]`). Render
  `seed.text`; approve verdict `surviving` = `seeds.filter(...).map(s =>
  s.id)` (backend `RunProposal.resolve` should accept `list[str]` of
  ids OR `list[dict]` — verify).

**`cards/index.ts`** — register `cite_proposal` + `finding`:
- Both hit `WbFallback` today. Map `cite_proposal` → the new `GateCard`
  variant (§D); `finding` → a minimal receipt card (icon + title +
  quote count + `open in desk`).

**`ExcerptCard.tsx`** — byline turn math:
- Use `at - at_idx + i` (backend ships `at_idx`), not
  `at - floor(len/2) + i`.
- Highlight message at `i === at_idx` with left accent border.

**`Output.tsx`** — `qref` regex:
- `AUDIT_ID_RE = />(a-[0-9a-f]{4})(?=<)/g` (exact 4-hex, whole-cell).

**`OrchTurn.tsx`** — stream coalesce:
- In the `deduped` pass, fold adjacent `STREAM_MIME` events with the
  same `.name` into one (concat `.text`).

**`RunCard.tsx` / `ScanCard.tsx`** — dead buttons:
- Cut `open log dir` (RunCard) and `view results` (ScanCard) — no
  `onClick`. ScanCard shows `df_head` inline instead (§C).
- `RunCard` `showAll` gets a `collapse` toggle.

---

## §B — Cuts (redundancy purge)

### `OrchColumn.tsx` header (→ passive status only)

**Cut:** `·` separators, `.head-model`, `.head-turn`, `.head-status`
text span, `.head-bg-btn`, all four transport buttons.

**Keep:** `.head-title` (with `title="{model} · turn {N} · started
{HH:MM}"` tooltip), status dot **or** gate pill (mutually exclusive):
```tsx
<span className="head-left">
  <span className="head-title" title={meta}>orchestrator</span>
  {gates.length > 0
    ? <GatePill gates={gates} onJump={...} />   // dot inside pill
    : <span className={`rl-dot rl-dot-${dot}`} title={statusText} />}
</span>
```
No `.orch-head-actions` at all. `GatePill` interaction: hover →
popover, click → scroll-to-first.

### `OrchColumn.tsx` composer (→ transport + send)

**Read `DeskView.tsx`** for M0's composer layout and match structurally.

**Cut:** `.composer-to` chip, `↳ orchestrator` under-hint, primary
button morph.

**Add:** `.composer-controls` row *below* textarea, *left* of send:
```tsx
<div className="composer-lower">
  <div className="composer-controls">
    {isRunning && <button onClick={cancelCell} title="interrupt cell"><IconStop/></button>}
    <button disabled={isRunning} onClick={step} title="run one turn"><IconStep/></button>
    <button onClick={playPause} title={isRunning ? "pause after this turn" : "run"}>
      {isRunning ? <IconPause/> : <IconPlay/>}
    </button>
  </div>
  {cellRunning && hasText
    ? <span className="composer-hint-running">reads after turn {N} · <a onClick={sendNow}>send now</a></span>
    : <span className="composer-hint">enter to send · shift+enter newline</span>}
  <button className="primary" disabled={!hasText} onClick={sendText}><IconSend/></button>
</div>
```
Composer disambiguation from M0: `border-left: 2px solid var(--gate)`
on `.orch-col-wrap .composer` (replaces the `to:` chip's job at zero
height cost).

### `OrchTurn.tsx`

**Cut:** `.prose-capped` / `.prose-more` / `long` heuristic entirely.
`AssistantProse` = `md → html → <div class="asst-prose md">` + cursor.

**Cut:** `.ask-by "you"` label on `<UserAsk>` (right-alignment already
signals authorship).

### `cards/*.tsx`

- `PromptCard`: cut `.gate-tag` "waiting on you" (border color + header
  pill suffice).
- `RunProposalCard`: cut `.sp-id` index, `.gate-reason` count text,
  `edit` button (dup of `.sp-toggle` expand).
- `RunCard`: cut inline `.out-live` badge (wrapper handles it per §C).
- `ExcerptCard`/`TranscriptCard`: cut header `open full` icon, cut
  `.out-kind` label.

### `orch.css`

Delete rules for: `.head-sep`, `.head-model`, `.head-turn`,
`.head-status*`, `.head-bg-btn`, `.orch-head-actions`, `.composer-to`,
`.orch-col-wrap .composer { padding-top: 26px }`, `.prose-capped`,
`.prose-more`, `.ask-by`.

---

## §C — Replaces (reshapes)

### `OrchTurn.tsx` — CodeCell collapsed-first

Default `collapsed = !running && !errored && !userExpanded`. Drop
`COLLAPSE_LOC` and the 6-line peek. When collapsed, `.cc-head` shows a
1-line gist:
```tsx
<code className="cc-gist">{firstNonBlankLine(code)}</code>
```
between status chips and `turn N`. Head is `role="button"` + click
toggles. Chevron `bi-chevron-{right,down}` at far left. No `<pre>` body
when collapsed. Force-expand while `running || errored`.

### `Output.tsx` — traceback summary

Read new backend payload (`ename/evalue/frames/text`). Render:
```
┌ ⚠ {ename} ────────────────────┐
│ {evalue}                       │
│ at {frames[-1].file}:{lineno}  │  ← last user frame, mono
│              [▾ {N} frames]    │
└────────────────────────────────┘
```
Toggle reveals `<pre>{text}</pre>`.

### `Output.tsx` — `.repr` → result rail

Drop grey-pill. `text/plain` last-expr renders as `<pre>` with
`border-left: 2px solid var(--accent)`, `padding-left: 14px`,
`background: none`, `color: var(--ink)`. Same left-rail language as
stream: grey rail = print, accent rail = result, red rail = stderr.

### `Output.tsx` — DataFrame bare

`if (html.includes('class="dataframe"'))` → `.out.bare` wrapper
(`overflow-x: auto`), not `.out.html` box. Table self-delimits.

### `Output.tsx` — `.out-live` threshold

`if (!stable || (settled && updates.current < 2)) return inner;` —
suppress `updated 1×` on trivially-displayed values. When shown while
settled, render as a hover-only `title` on a 6px grey dot.

### `Output.tsx` — plotly affordance

After `gd.on('plotly_click', ...)`, if `gd.data?.some(t =>
t.customdata)`:
- Append `<div class="fx-more plotly-hint">click a point to open in
  desk</div>` below figure.
- CSS: `.plotly-host .scatterlayer .points path { cursor: pointer }`.

### `Output.tsx` — `WbFallback`

Drop redundant `.gate-desc` above the JSON. Add explicit `<div
class="err">no renderer for kind={kind}</div>` above the dump.

### `OrchColumn.tsx` — sys-chip → origin-cell badge

Delete `.sys-chip` map. Parse `orch.notifications` for
`cell-{N}.*bound: (\w+)`, build `settled: Map<turn, binding>`, thread
into `OrchTurn`. `.cc-bg-chip` renders `done · {binding}` when present
(instead of `bg`). Optional 2s toast on notification-array growth.

### `RunCard.tsx` — row reshape

- Drop `.ar-status` text for `done`/`running` (dot suffices). Keep for
  `error` (exception class) and `stopped`.
- Running rows: show `t{row.turns}` where status text was.
- Persistent `.ar-opened` `↗` glyph on any row in a component-local
  `Set` (survives the 2s highlight).
- `.ar-seed` → CSS single-line ellipsis.

### `PromptCard.tsx` — resolved single-line

Resolved state → one `.out-head` line: `bi-person-check` +
`{question} → <b>{answer}</b> · {HH:MM}`. No body.

Free-text row: only when `options === null`. Otherwise a small
`other…` link that expands the input on click.

Key hints: only when `opts.length > 3`; render as faint superscript,
no brackets.

### `ScanCard.tsx` — single card

One `.out` with header + one `.scan-row` per scanner:
`{name} · {results} found · {scans}/{total} · {bar}`. Location once in
footer (basename + `title` tooltip). On finish, render `df_head` (the
3-row preview per scanner backend already ships) inline below rows.

### `ExcerptCard.tsx` / `TranscriptCard.tsx` — dense reader

Replace `<Bubble>` with a flat `.rd-msg` list: gutter-left role tag
(`asst`/`user`/`tool`, xs mono), body `renderContent(msg)` in a plain
`<div>`. No rounded box, no `CollapsibleContent` wrapper. `ExcerptCard`
adds `.rd-anchor` left-border on `i === at_idx`.

`TranscriptCard` `.iv-url` → basename only + `title` tooltip.

---

## §D — 6→3 card refactor

Three families in `cards/`:

### `GateCard.tsx` (Prompt / RunProposal / CiteProposal)

Shared shell:
```tsx
<div className={`out gated ${pending ? 'gate-waiting' : ''}`}>
  <div className="out-head"><i className={icon}/> {resolvedHead}</div>
  {pending && <div className="gate-desc">{desc}</div>}
  {pending && <BodySlot />}   {/* variant */}
  {pending && <div className="gate-bar">
    <button className="deny" onClick={deny}>deny</button>
    {denyOpen && <input className="gate-deny-reason" ... />}
    <span className="gate-spacer" />
    <button className="approve" onClick={approve}>{approveLabel}</button>
  </div>}
</div>
```
Variants supply `{icon, desc, BodySlot, approveLabel, buildVerdict,
resolvedHead}`. `prompt` variant's approve is the option buttons (no
gate-bar); `run_proposal` body = seed checklist; `cite_proposal` body =
quote list with per-quote checkboxes.

### `ProgressCard.tsx` (Run / AuditRun / Scan)

Shared shell: `.fx-dot` + task/id header, `.gate-desc` subtitle,
counter line (`.fx-bar` + `{done}/{total}` + `+err` + `elapsed`), row
list with `N more`/`collapse`. Variants supply `{rowRenderer,
onRowClick, footerSlot}`.

### `ReaderCard.tsx` (Excerpt / Transcript)

Shared shell: minimal header (icon + `sample_id · t{at}`), `.rd-msg`
list body, footer (`from … · <a>open in desk →</a>`). Variants:
excerpt highlights `at_idx`; transcript adds empty-preview fallback.

**`index.ts`** maps 8 kinds → 3 components + variant key.

---

## §E — Verify

1. `tsc --noEmit` clean (baseline: 2 ts-mono dual-react errors)
2. `uv run python -m workbench._smoke_m1_{kernel,orchestrator,run}` green
3. `uv run python -m workbench._screenshot_m1` — read all 8 shots
4. Fresh-eyes reviewer on new shots
5. e2e on `m1-e2e-0702`: `waiting` broadcast, steer, seeds render
   (not `[object Object]`)
6. `aisi instance terminate --name m1-e2e-0702`

---

## File ownership (parallel agents — disjoint)

| Agent | Files |
|---|---|
| **backend** | `src/workbench/m1/{kernel,run,read,cite}.py` |
| **column** | `frontend-wb/src/components/orch/OrchColumn.tsx`; `orch.css` §header/§composer only |
| **turn/output** | `frontend-wb/src/components/orch/{OrchTurn,Output}.tsx`; `orch.css` §turn/§output only; **not** §cards |
| **cards** | `frontend-wb/src/components/orch/cards/*.tsx` (refactor to 3); `orch.css` §cards only |

`orch.css` sections are comment-delimited. Each agent edits only rules
whose selectors match its prefixes; new rules append at EOF under a
`/* -- {agent} additions -- */` marker. Merge conflicts resolved after.
