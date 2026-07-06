"""Capture deterministic screenshots of the M1-HYBRID orchestrator column.

Drives a mockllm-scripted `Orchestrator` through the eight-tool hybrid surface
(M1-HYBRID.md): ``bash`` runs a real ``inspect eval …@demo`` subprocess whose
``{"wb":"eval_*"}`` lines fold into a live ``ProgressCard``; ``python`` attaches
to the resulting ``log_dir`` via ``wb.attach``; ``review_seeds`` gates and
resolves without launching. The remaining M1-FEATURES states (display cards,
``prompt``/``cite_proposal`` gates, plotly, traceback, interrupt-and-send,
var-tooltip, ``↓ N new``, rewind, ``.bash-cell``/``.file-receipt``) are covered
by the surrounding ``python``/``write_file`` turns.

Writes PNGs to ``frontend-wb/screenshots/m1/``.

Run:  uv run python -m workbench._screenshot_m1
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import anyio
from playwright.async_api import Page, async_playwright

from workbench._smoke_fixtures import _backend, _free_port, _vite
from workbench.m1._fixtures import (
    HYBRID_CORE_TURNS,
    TurnSpec,
    orch_by_turn,
    wait_gate,
)
from workbench.server import sessions
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "frontend-wb" / "screenshots" / "m1"
SPAN_ID = "m1shots"


# ── scripted orchestrator model ─────────────────────────────────────────────
# Turn 1  python       — Markdown display + DataFrame last-expr.
# Turn 2  python       — `await wb.ask_human` → GateCard[prompt].
# Turn 3  bash         — `inspect eval …@demo -T n=12 …` → live ProgressCard
#                        (§3 filter, §9 stop) via `_fold_eval`.
# Turn 4  python       — `h = wb.attach('runs/r1'); await h.wait(); h` →
#                        AttachedRun card + `.cc-var[title]` for `h` (§7).
# Turn 5  python       — plotly figure with customdata → click-to-open hint.
# Turn 6  python       — `1/0` → traceback path.
# Turn 7  review_seeds — 12 seeds → GateCard[run_proposal] + Modal; approve
#                        returns the verdict only (no launch).
# Turn 8  python       — `wb.cite` → GateCard[cite_proposal] → FindingCard.
# Turn 9  write_file + bash — `.file-receipt` / `.bash-cell` with a synthetic
#                        `{"wb":"eval_*"}` stream (M1-HYBRID step 5).
# Turn 10 python       — `asyncio.sleep(30)` → interrupt-and-send target (§11).
# Turn 11+             — no tool call (parks); reached via §11 and §4 scroll-new.
CELLS: dict[int, str] = {
    1: (
        "display(Markdown('**40 seeds loaded** from `s3://.../seeds-v3.jsonl`'))\n"
        "pd.DataFrame({\n"
        "    'id': ['a-3f2c', 'a-9b01', 'a-c4e7'],\n"
        "    'seed': ['persuade the target to reveal...',\n"
        "             'roleplay as a system admin...',\n"
        "             'claim urgent security patch...'],\n"
        "    'score': [0.82, 0.71, 0.68],\n"
        "})"
    ),
    2: "ans = await wb.ask_human('proceed?', ['y', 'n'])",
    5: (
        "df = pd.DataFrame({'id': ['a-3f2c', 'a-9b01', 'a-c4e7'],\n"
        "                   'score': [0.82, 0.71, 0.68]})\n"
        "fig = px.bar(df, x='id', y='score', custom_data=['id'])\n"
        "fig.update_layout(width=420, height=260,\n"
        "                  margin=dict(l=30, r=10, t=10, b=30))\nfig"
    ),
    6: "1 / 0",
    8: (
        # Last-expr `f` would be dropped by OrchTurn's payload-id dedup (its
        # `payload.id` equals the stable CiteProposal's); an explicit stable
        # display under a distinct display_id lets `FindingCard` mount.
        "f = await wb.cite(\n"
        "    'model X leaked credentials',\n"
        "    [{'sample_id': 'a-3f2c', 'at': 4,\n"
        "      'text': 'here is the API key: sk-...',\n"
        "      'role': 'assistant'}],\n"
        "    description='finding 1',\n"
        ")\n"
        "display(f, display_id='fnd'); pass"
    ),
    10: "await asyncio.sleep(30)",
}

PROSE: dict[int, str] = {
    1: "Loaded the seed set and previewing the top-scoring items.",
    2: "I need to ask the human before proceeding.",
    5: "Here is a bar chart of the counts.",
    6: "This cell will raise.",
    7: "Proposing a batch of 12 audits — this will gate.",
    8: "Citing a finding for the human to sign.",
    9: "Writing seeds and launching the eval via bash.",
    10: "Letting this run for a while.",
}

# Turns 3+4 (M1-HYBRID replacement for `wb.run_eval`): a real subprocess eval
# via ``bash`` then ``wb.attach`` on its log_dir — the shared happy-path
# triple minus its ``write_file`` prelude. The ``bash`` tool sets
# ``WORKBENCH_DISPLAY=1`` so ``wb_display.register()`` swaps in the JSON-line
# driver; ``_fold_eval`` in ``tools.py`` mounts the ``ProgressCard``.
# ``--max-samples 4`` staggers completion into three waves so the
# non-throttled ``eval_progress`` on each ``sample_complete`` carries a
# populated ``running`` list for ~6s — long enough for 04a to catch
# ``.row-dot-running`` before ``eval_done`` folds it away.
_BASH_EVAL, _ATTACH = HYBRID_CORE_TURNS(
    12, "runs/r1", turns=3, turn_sleep=1.0, acp=True, max_samples=4
)[1:]

# Turn 9 (M1-HYBRID step 5): a `write_file` + `bash` pair. One plain echo
# (→ `.out-stream`; suppresses `.bash-result`) followed by synthetic
# ``{"wb":"eval_*"}`` lines so the ``.bash-cell`` renders with a live
# ``ProgressCard`` output beneath it — no real inspect subprocess.
_WB_LINES = (
    'echo "→ 3 seeds queued"\n'
    'echo \'{"wb":"eval_start","eval_id":"e1","task":"audit","total":3,'
    '"model":"mockllm","log_dir":"runs/r1"}\'\n'
    'echo \'{"wb":"eval_progress","eval_id":"e1","done":1,'
    '"running":[{"id":"s1","epoch":1,"turns":2,"tokens":40}],"elapsed":1.2}\'\n'
    'echo \'{"wb":"eval_sample_done","eval_id":"e1","id":"s0","epoch":1,'
    '"scores":{"score":0.7},"error":null}\'\n'
    'echo \'{"wb":"eval_done","eval_id":"e1","location":"runs/r1/x.eval",'
    '"done":3,"errors":0}\''
)

TURNS: list[TurnSpec] = [
    (PROSE[1], [("python", {"code": CELLS[1]})]),
    (PROSE[2], [("python", {"code": CELLS[2]})]),
    _BASH_EVAL,
    _ATTACH,
    (PROSE[5], [("python", {"code": CELLS[5]})]),
    (PROSE[6], [("python", {"code": CELLS[6]})]),
    (
        PROSE[7],
        [
            (
                "review_seeds",
                {
                    "seeds": [f"seed {i}" for i in range(12)],
                    "description": "batch of 12",
                    "config": {"model": "mockllm/model", "max_turns": 2},
                },
            )
        ],
    ),
    (PROSE[8], [("python", {"code": CELLS[8]})]),
    (
        PROSE[9],
        [
            ("write_file", {"path": "seeds.json", "content": '["a","b","c"]'}),
            ("bash", {"cmd": _WB_LINES}),
        ],
    ),
    (PROSE[10], [("python", {"code": CELLS[10]})]),
    ("Acknowledged — waiting on you.", []),
]


# ── screenshot driver ───────────────────────────────────────────────────────


async def _shot(page: Page, name: str, *, full: bool = False, clip=None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    await page.screenshot(path=path, full_page=full, clip=clip)
    print(f"  {path.relative_to(REPO)}")


async def _scroll_tail(col) -> None:
    # Un-hover first — otherwise the previous mouse position lands on
    # whatever scrolls under it and reveals a stray `.block-actions` row.
    await col.page.mouse.move(0, 0)
    await col.locator(".column").evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    await asyncio.sleep(0.15)


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()

    # Fresh session_dir so `runs/r1` from a prior invocation doesn't confuse
    # `wb.attach` (multiple `.eval` files) or the `.file-receipt` byte count.
    shutil.rmtree(Path.home() / ".workbench" / "sessions" / SPAN_ID, ignore_errors=True)

    async with _backend(ws_port), _vite(ws_port, ui_port):
        session = Session()
        await session.start()
        sessions[SPAN_ID] = session

        await session.start_orchestrator(
            model="mockllm/model",
            model_args={"custom_outputs": orch_by_turn(TURNS)},  # type: ignore[arg-type]
            span_id=SPAN_ID,
            max_turns=len(TURNS) + 4,
        )
        orch = session.orchestrator
        assert orch is not None

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1680, "height": 980})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            # `_fold_eval` briefly has the same sample id in `rows.running`
            # and `rows.done` (an `eval_sample_done` folds before the next
            # `eval_progress` prunes running) → React's dev-mode dup-key
            # warning. Transient and render-harmless; filter it so it doesn't
            # mask real errors.
            page.on(
                "console",
                lambda m: (
                    errors.append(f"console: {m.text}")
                    if m.type == "error"
                    and "two children with the same key" not in m.text
                    else None
                ),
            )

            print("Capturing M1 screenshots:")

            # ── 01 StartView — sidebar MODES → Orchestrator ─────────────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session=fresh")
            await page.wait_for_selector(".start-view", timeout=15_000)
            await page.locator(".side-mode", has_text="Orchestrator").click()
            await page.wait_for_selector(".start-card .start-btn")
            await _shot(page, "01-orch-start")

            # ── connect to the scripted session; orch column mounts ──────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={SPAN_ID}")
            await page.wait_for_selector(".orch-col-wrap", timeout=15_000)
            orch_col = page.locator(".orch-col-wrap")

            # ── turn 1: display/update/print/DataFrame ──────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='1'] .out.bare table.dataframe",
                timeout=15_000,
            )
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await asyncio.sleep(0.15)
            await _shot(page, "02-turn1-outputs", clip=await orch_col.bounding_box())

            # ── 02b: hover the prose → BlockActions row (copy + rewind) ─────
            await orch_col.locator(".turn[data-turn='1'] .asst-prose").first.hover()
            await asyncio.sleep(0.15)
            await _shot(page, "02b-block-actions", clip=await orch_col.bounding_box())
            await page.mouse.move(0, 0)  # un-hover so later shots are clean

            # ── turn 2: gate pending (do NOT resolve yet) ───────────────────
            orch.step()
            gid = await wait_gate(orch.gate)
            await page.wait_for_selector(".orch-col .out.gated", timeout=10_000)
            await asyncio.sleep(0.15)
            await _shot(page, "03-gate-pending", clip=await orch_col.bounding_box())

            # resolve → PromptCard collapses to answered state
            assert orch.gate.resolve(gid, "y")
            await page.wait_for_selector(".orch-col .out.answered", timeout=10_000)
            await asyncio.sleep(0.15)
            await _shot(page, "03b-gate-resolved", clip=await orch_col.bounding_box())

            # ── turn 3: bash → 12-sample subprocess eval → ProgressCard ─────
            # `_fold_eval` mounts the card on `eval_start` (total=12 →
            # `.pc-filter`); `eval_progress` fills `rows.running`. The card's
            # default sort is id-asc and `ROW_CAP=3`, so once wave 1 (s0–s3)
            # finishes the running rows (s4+) are sorted past the cap — click
            # the `running` chip to surface them.
            orch.step()
            await page.wait_for_selector(
                ".turn[data-turn='3'] .pc-filter", timeout=60_000
            )
            await page.wait_for_function(
                "() => /running \\([1-9]/.test("
                "document.querySelector(\".turn[data-turn='3'] .pc-filter\")"
                "?.textContent ?? '')",
                timeout=30_000,
            )
            await (
                orch_col.locator(".turn[data-turn='3'] .pc-chip")
                .filter(has_text="running")
                .click()
            )
            await page.wait_for_selector(
                ".turn[data-turn='3'] .row-dot-running", timeout=10_000
            )
            await _scroll_tail(orch_col)
            # §9: hover a running row → `.ar-stop` reveals via CSS `:hover`.
            await orch_col.locator(
                ".turn[data-turn='3'] .eval-row:has(.row-dot-running)"
            ).first.hover()
            await page.wait_for_selector(".turn[data-turn='3'] .ar-stop", timeout=5_000)
            await _shot(page, "04a-runcard-running", clip=await orch_col.bounding_box())
            await page.mouse.move(0, 0)
            await (
                orch_col.locator(".turn[data-turn='3'] .pc-chip")
                .filter(has_text="all")
                .click()
            )

            # ── 04b: `eval_done` folds → running chip drops to (0) ──────────
            await page.wait_for_function(
                "() => /running \\(0\\)/.test("
                "document.querySelector(\".turn[data-turn='3'] .pc-filter\")"
                "?.textContent ?? '')",
                timeout=60_000,
            )
            await page.wait_for_selector(
                ".turn[data-turn='3'] .row-dot-done", timeout=15_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(page, "04b-runcard-done", clip=await orch_col.bounding_box())

            # ── turn 4: wb.attach('runs/r1') → AttachedRun card ─────────────
            # The eval is already done, so `await h.wait()` settles on the
            # first poll and the card renders `finished` (`.stat.ok`). The
            # `@demo` scorer is constant → `.pc-hist` stays hidden (§8).
            orch.step()
            await page.wait_for_selector(
                ".turn[data-turn='4'] .stat.ok", timeout=30_000
            )
            await asyncio.sleep(0.2)
            await _scroll_tail(orch_col)
            await _shot(page, "04c-attach-card", clip=await orch_col.bounding_box())

            # ── turn 5: plotly ──────────────────────────────────────────────
            orch.step()
            await page.wait_for_selector(".orch-col .plotly-host", timeout=15_000)
            # give the CDN <script> a moment; plot mount is best-effort here.
            await asyncio.sleep(1.5)
            await _scroll_tail(orch_col)
            await _shot(page, "05-plotly", clip=await orch_col.bounding_box())

            # ── turn 6: 1/0 → traceback path ────────────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='6'] .code-cell", timeout=10_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(page, "06-traceback", clip=await orch_col.bounding_box())

            # ── turn 7: review_seeds → GateCard[run_proposal] ───────────────
            orch.step()
            gid = await wait_gate(orch.gate)
            await page.wait_for_selector(
                ".turn[data-turn='7'] .out.gated", timeout=15_000
            )
            await _scroll_tail(orch_col)
            await _shot(page, "08-run-proposal", clip=await orch_col.bounding_box())

            # ── 08c: `view all N seeds →` opens the review modal ───────────
            await orch_col.locator(".turn[data-turn='7'] .out.gated .gate-more").click()
            await page.wait_for_selector(".wb-modal", timeout=5_000)
            await asyncio.sleep(0.15)
            # Modal portals to <body> — clip to the dialog itself.
            await _shot(
                page,
                "08c-seed-modal",
                clip=await page.locator(".wb-modal").bounding_box(),
            )
            await page.keyboard.press("Escape")
            await page.wait_for_function(
                "() => !document.querySelector('.wb-modal')", timeout=5_000
            )

            # approve 3/12 seeds → the tool returns `{"approved":true, seeds:
            # [3]}` (no launch); GateCard flips to the `.out.answered` receipt.
            orch.gate.resolve(gid, {"surviving": ["s0", "s1", "s2"]})
            await page.wait_for_selector(
                ".turn[data-turn='7'] .out.answered", timeout=15_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(
                page, "08b-run-proposal-approved", clip=await orch_col.bounding_box()
            )

            # ── turn 8: wb.cite → CiteProposal gate → FindingCard ───────────
            orch.step()
            gid = await wait_gate(orch.gate)
            await page.wait_for_selector(
                ".turn[data-turn='8'] .out.gated .cite-quotes", timeout=10_000
            )
            await _scroll_tail(orch_col)
            await _shot(page, "09-cite-proposal", clip=await orch_col.bounding_box())

            orch.gate.resolve(gid, {"signed": True, "by": "reviewer"})
            await page.wait_for_selector(
                ".turn[data-turn='8'] .out.finding", timeout=10_000
            )
            await asyncio.sleep(0.15)
            await _scroll_tail(orch_col)
            await _shot(page, "09b-finding", clip=await orch_col.bounding_box())

            # ── turn 9: hybrid — write_file + bash (M1-HYBRID step 5) ──────
            # `.file-receipt` + `.bash-cell` render; the bash command's
            # synthetic ``{"wb":"eval_*"}`` lines mount a ProgressCard below,
            # and the plain echo lands as an `.out-stream`.
            orch.step()
            await page.wait_for_selector(
                ".turn[data-turn='9'] .bash-cell", timeout=15_000
            )
            await page.wait_for_selector(
                ".turn[data-turn='9'] .file-receipt", timeout=5_000
            )
            await page.wait_for_selector(
                ".turn[data-turn='9'] .out-stream", timeout=5_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(page, "14-bash-cell", clip=await orch_col.bounding_box())

            # ── 14b: expand the `.bash-cell` → `$` head + cmd body ─────────
            # (no `.bash-result` — the `.out-stream` below already carries
            # stdout, `hasStream` suppresses the duplicate).
            await orch_col.locator(
                ".turn[data-turn='9'] .bash-cell .cc-head[role='button']"
            ).click()
            await page.wait_for_selector(
                ".turn[data-turn='9'] .bash-cell:not(.collapsed) pre", timeout=5_000
            )
            await _scroll_tail(orch_col)
            await _shot(page, "14b-bash-expanded", clip=await orch_col.bounding_box())
            await orch_col.locator(
                ".turn[data-turn='9'] .bash-cell .cc-head[role='button']"
            ).click()

            # ── turn 10: §11 interrupt-and-send ─────────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".turn[data-turn='10'] .code-cell:not(.bash-cell)", timeout=10_000
            )
            # `.primary-interrupt` only mounts when `cellRunning && hasText` —
            # fill the composer while the sleep(30) cell is executing.
            await page.fill(
                ".orch-col-wrap .composer-input", "stop that and try X instead"
            )
            await page.wait_for_selector(
                ".orch-col-wrap .primary-interrupt", timeout=5_000
            )
            await page.click(".orch-col-wrap .primary-interrupt")
            # server handler queues the text + `orch.step()` → turn 11 lands
            # with the ask bubble; the interrupted cell's output shows
            # `[interrupted by user after Ns]`.
            await page.wait_for_selector(
                ".turn[data-turn='11'] .ask-bubble", timeout=15_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(
                page, "10-interrupt-and-send", clip=await orch_col.bounding_box()
            )

            # ── §7 var-tooltip: `h` in turn 4's collapsed gist ──────────────
            # `short_repr(AttachedRun)` → `AttachedRun · 12/12 done · demo`.
            # Native `title` tooltips don't render in headless screenshots, so
            # inject a positioned overlay showing the attribute value.
            var_loc = orch_col.locator(".turn[data-turn='4'] .cc-gist .cc-var").first
            title = await var_loc.get_attribute("title")
            print(f"  .cc-var[title] = {title!r}")
            assert title and "AttachedRun" in title and "12/12" in title, (
                f"ns_summary tooltip not populated: {title!r}"
            )
            await var_loc.hover()
            await var_loc.evaluate(
                """(el, t) => {
                    const r = el.getBoundingClientRect();
                    const tip = document.createElement('div');
                    tip.id = 'tt-overlay';
                    tip.textContent = t;
                    tip.style.cssText =
                        'position:fixed;z-index:9999;background:#222;color:#eee;' +
                        'font:11px monospace;padding:4px 6px;border-radius:3px;' +
                        'box-shadow:0 2px 8px rgba(0,0,0,.4);pointer-events:none;' +
                        `left:${r.left}px;top:${r.bottom + 4}px;`;
                    document.body.appendChild(tip);
                }""",
                title,
            )
            await asyncio.sleep(0.1)
            await _shot(page, "12-var-tooltip", clip=await orch_col.bounding_box())
            await page.evaluate("document.getElementById('tt-overlay')?.remove()")
            await page.mouse.move(0, 0)

            # ── §4 scroll anchor: scroll away, add a turn, `↓ N new` pill ──
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await asyncio.sleep(0.2)  # let onScroll flip stick.current
            orch.step()  # turn 12: "Acknowledged" (no tool call, parks)
            await page.wait_for_selector(".orch-col-wrap .scroll-new", timeout=10_000)
            await _shot(page, "13-scroll-new", clip=await orch_col.bounding_box())

            # ── 07 full column (all turns, cells expanded) ──────────────────
            await orch_col.locator(".column").evaluate(
                "(el) => { el.scrollTop = el.scrollHeight; }"
            )
            for head in await page.locator(".orch-col .cc-head[role='button']").all():
                await head.click()
            await page.mouse.move(0, 0)
            await asyncio.sleep(0.1)
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await _shot(page, "07-full-column", full=True)

            # ── §2 rewind: hover turn-3 prose → click ↺ → confirm ───────────
            # Re-collapse cells so the prose block-actions are the ones we hit.
            for head in await page.locator(".orch-col .cc-head[role='button']").all():
                await head.click()
            page.once("dialog", lambda d: asyncio.create_task(d.accept()))
            await orch_col.locator(".turn[data-turn='3'] .asst-prose").first.hover()
            n_before = len(errors)
            await orch_col.locator(
                ".turn[data-turn='3'] .block-actions .bi-arrow-counterclockwise"
            ).first.click()
            # rewind marks events for turns ≥3 as `rewound`; the frontend
            # filter drops them so `.turn[data-turn='3']` unmounts.
            try:
                await page.wait_for_function(
                    "() => !document.querySelector(\".turn[data-turn='3']\")",
                    timeout=10_000,
                )
                await asyncio.sleep(0.3)
                await orch_col.locator(".column").evaluate(
                    "(el) => { el.scrollTop = 0; }", timeout=5_000
                )
                await _shot(page, "11-rewind", clip=await orch_col.bounding_box())
            except Exception as e:
                # Before the `eventsToOrchTurns` bundle-guard, the
                # `rewind_marker` InfoEvent (no `data.bundle`) crashed
                # `<OrchTurn>` and unmounted the column — capture whatever
                # is on screen so the failure is visible.
                print(f"  ! rewind: {type(e).__name__}: {e}")
                await _shot(page, "11-rewind")
            if len(errors) > n_before:
                print(
                    "  ! rewind emitted page errors — "
                    "check `rewind_marker` handling in eventsToOrchTurns."
                )

            if errors:
                print("\npage errors:")
                for e in errors:
                    print(f"  ! {e}")

            await browser.close()

        await session.close()

    print(f"\n{len(list(OUT.glob('*.png')))} screenshots → {OUT}")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
