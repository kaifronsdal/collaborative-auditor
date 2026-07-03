"""Capture deterministic screenshots of the M1 orchestrator column.

Drives a mockllm-scripted `Orchestrator` through a fixed sequence of `python`
tool calls covering every M1-FEATURES.md state: display cards, gates
(`prompt`/`run_proposal`/`cite_proposal`), the `ProgressCard` sort/filter/
histogram/per-sample-stop cluster, interrupt-and-send, var-tooltip, the
`↓ N new` scroll pill, and rewind. Writes PNGs to
``frontend-wb/screenshots/m1/``.

Run:  uv run python -m workbench._screenshot_m1
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo
from playwright.async_api import Page, async_playwright

from workbench._smoke_fixtures import _backend, _free_port, _vite
from workbench._smoke_m1_run import make_scored_task, make_task
from workbench.server import sessions
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "frontend-wb" / "screenshots" / "m1"


# ── scripted orchestrator model ─────────────────────────────────────────────
# Turn 1: Markdown display + DataFrame last-expr.
# Turn 2: gated ask_human (blocks until resolve()).
# Turn 3: run_eval → 12-sample scored RunHandle (§3 filter/sort, §8 hist, §9 stop).
# Turn 4: plotly figure with customdata → click-to-open hint.
# Turn 5: cell error → traceback path.
# Turn 6: run_audits → RunProposal gate; approve → AuditRunHandle flip.
# Turn 7: wb.cite → CiteProposal gate → Finding card.
# Turn 8: asyncio.sleep(30) → interrupt-and-send target (§11).
# Turn 9+: no tool call (parks) — reached via §11 and again via §4 scroll-new.
CELLS = [
    (
        "Loaded the seed set and previewing the top-scoring items.",
        "display(Markdown('**40 seeds loaded** from `s3://.../seeds-v3.jsonl`'))\n"
        "pd.DataFrame({\n"
        "    'id': ['a-3f2c', 'a-9b01', 'a-c4e7'],\n"
        "    'seed': ['persuade the target to reveal...',\n"
        "             'roleplay as a system admin...',\n"
        "             'claim urgent security patch...'],\n"
        "    'score': [0.82, 0.71, 0.68],\n"
        "})",
    ),
    (
        "I need to ask the human before proceeding.",
        "ans = await wb.ask_human('proceed?', ['y', 'n'])",
    ),
    (
        "Launching a 12-sample scored eval; the card ticks while I keep going.",
        "h = wb.run_eval(make_scored_task('demo', 12, turn_sleep=1.0),\n"
        "                model='mockllm/model',\n"
        "                description='twelve scored mockllm samples')\n"
        "h",
    ),
    (
        "Here is a bar chart of the counts.",
        "df = pd.DataFrame({'id': ['a-3f2c', 'a-9b01', 'a-c4e7'],\n"
        "                   'score': [0.82, 0.71, 0.68]})\n"
        "fig = px.bar(df, x='id', y='score', custom_data=['id'])\n"
        "fig.update_layout(width=420, height=260,\n"
        "                  margin=dict(l=30, r=10, t=10, b=30))\nfig",
    ),
    (
        "This cell will raise.",
        "1 / 0",
    ),
    (
        "Proposing a batch of 12 audits — this will gate.",
        # Gate first (petri import happens before the gate, so wrap the whole
        # thing); on approve the AuditRunHandle displays under the same
        # display_id and we cancel it before mockllm-as-auditor can flail.
        "try:\n"
        "    hp = await wb.run_audits(\n"
        "        ['seed ' + str(i) for i in range(12)], {'max_turns': 2},\n"
        "        description='batch of 12', model='mockllm/model',\n"
        "    )\n"
        "    await asyncio.sleep(0.4)\n"
        "    hp.cancel()\n"
        "except Exception as e:\n"
        "    print(f'[post-gate launch: {type(e).__name__}: {e}]')",
    ),
    (
        "Citing a finding for the human to sign.",
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
        "display(f, display_id='fnd'); pass",
    ),
    (
        "Letting this run for a while.",
        "await asyncio.sleep(30)",
    ),
]


def _python(prose: str, code: str) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content=prose)
    out.choices[0].message.tool_calls = [
        ToolCall(id="c", function="python", type="function", arguments={"code": code})
    ]
    return out


def _orch_outputs(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    del tools, tool_choice, config
    n = sum(1 for m in input if m.role == "assistant")
    if n < len(CELLS):
        prose, code = CELLS[n]
        return _python(prose, code)
    return ModelOutput.from_content(
        model="mockllm", content="Acknowledged — waiting on you."
    )


# ── screenshot driver ───────────────────────────────────────────────────────


async def _shot(page: Page, name: str, *, full: bool = False, clip=None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    await page.screenshot(path=path, full_page=full, clip=clip)
    print(f"  {path.relative_to(REPO)}")


async def _wait_for(cond, *, timeout: float = 15.0, interval: float = 0.02) -> None:
    """Poll ``cond()`` until truthy or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out waiting for {cond}")
        await asyncio.sleep(interval)


async def _scroll_tail(col) -> None:
    # Un-hover first — otherwise the previous mouse position lands on
    # whatever scrolls under it and reveals a stray `.block-actions` row.
    await col.page.mouse.move(0, 0)
    await col.locator(".column").evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    await asyncio.sleep(0.15)


async def _amain() -> None:  # noqa: PLR0912, PLR0915
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "m1shots"

    async with _backend(ws_port), _vite(ws_port, ui_port):
        session = Session()
        await session.start()
        sessions[sid] = session

        await session.start_orchestrator(
            model="mockllm/model",
            model_args={"custom_outputs": _orch_outputs},
            max_turns=len(CELLS) + 4,
        )
        orch = session.orchestrator
        assert orch is not None
        # Seed task builders so cell 3 can construct a Task without importing.
        orch.kernel.shell.user_ns["make_task"] = make_task
        orch.kernel.shell.user_ns["make_scored_task"] = make_scored_task

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1680, "height": 980})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: m.type == "error" and errors.append(f"console: {m.text}"),
            )

            print("Capturing M1 screenshots:")

            # ── 01 StartView — sidebar MODES → Orchestrator ─────────────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session=fresh")
            await page.wait_for_selector(".start-view", timeout=15_000)
            await page.locator(".side-mode", has_text="Orchestrator").click()
            await page.wait_for_selector(".start-card .start-btn")
            await _shot(page, "01-orch-start")

            # ── connect to the scripted session; orch column mounts ──────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
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
            await orch_col.locator(
                ".turn[data-turn='1'] .asst-prose"
            ).first.hover()
            await asyncio.sleep(0.15)
            await _shot(
                page, "02b-block-actions", clip=await orch_col.bounding_box()
            )
            await page.mouse.move(0, 0)  # un-hover so later shots are clean

            # ── turn 2: gate pending (do NOT resolve yet) ───────────────────
            orch.step()
            await _wait_for(lambda: orch.kernel.gate.pending)
            (gid,) = orch.kernel.gate.pending
            await page.wait_for_selector(".orch-col .out.gated", timeout=10_000)
            await asyncio.sleep(0.15)
            await _shot(page, "03-gate-pending", clip=await orch_col.bounding_box())

            # resolve → PromptCard collapses to answered state
            assert orch.kernel.gate.resolve(gid, "y")
            await page.wait_for_selector(".orch-col .out.answered", timeout=10_000)
            await asyncio.sleep(0.15)
            await _shot(page, "03b-gate-resolved", clip=await orch_col.bounding_box())

            # ── turn 3: 12-sample scored eval → §3/§8/§9 ProgressCard ───────
            orch.step()
            # `.pc-filter` gates on `total > 8`; running rows land once the
            # watcher's first `_poll` picks up `active_samples()` (~0.25s).
            await page.wait_for_selector(
                ".turn[data-turn='3'] .pc-filter", timeout=30_000
            )
            await page.wait_for_selector(
                ".turn[data-turn='3'] .row-dot-running", timeout=30_000
            )
            await _scroll_tail(orch_col)
            # §9: hover a running row → `.ar-stop` reveals via CSS `:hover`.
            await orch_col.locator(
                ".turn[data-turn='3'] .eval-row:has(.row-dot-running)"
            ).first.hover()
            await page.wait_for_selector(
                ".turn[data-turn='3'] .ar-stop", timeout=5_000
            )
            await _shot(
                page, "04a-runcard-running", clip=await orch_col.bounding_box()
            )
            await page.mouse.move(0, 0)

            # ── 04b: after finish → §8 histogram + §3 sort by score ─────────
            for _ in range(400):
                h = orch.kernel.shell.user_ns.get("h")
                if h is not None and h.finished:
                    break
                await asyncio.sleep(0.05)
            await page.wait_for_selector(
                ".turn[data-turn='3'] .pc-hist", timeout=15_000
            )
            await orch_col.locator(
                ".turn[data-turn='3'] .pc-cols .pc-col"
            ).filter(has_text="score").click()
            await asyncio.sleep(0.15)
            await _scroll_tail(orch_col)
            await _shot(page, "04b-runcard-done", clip=await orch_col.bounding_box())

            # ── turn 4: plotly ──────────────────────────────────────────────
            orch.step()
            await page.wait_for_selector(".orch-col .plotly-host", timeout=15_000)
            # give the CDN <script> a moment; plot mount is best-effort here.
            await asyncio.sleep(1.5)
            await _scroll_tail(orch_col)
            await _shot(page, "05-plotly", clip=await orch_col.bounding_box())

            # ── turn 5: 1/0 → traceback path ────────────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='5'] .code-cell", timeout=10_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(page, "06-traceback", clip=await orch_col.bounding_box())

            # ── turn 6: run_audits → RunProposal gate ───────────────────────
            orch.step()
            await _wait_for(lambda: orch.kernel.gate.pending, timeout=20.0)
            (gid,) = orch.kernel.gate.pending
            await page.wait_for_selector(
                ".turn[data-turn='6'] .out.gated", timeout=15_000
            )
            await _scroll_tail(orch_col)
            await _shot(page, "08-run-proposal", clip=await orch_col.bounding_box())

            # ── 08c: `view all N seeds →` opens the review modal ───────────
            await orch_col.locator(
                ".turn[data-turn='6'] .out.gated .gate-more"
            ).click()
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

            # approve 3/12 seeds → proposal card flips to the AuditRunHandle
            # (same display_id). mockllm can't drive petri's auditor, so the
            # cell cancels the handle right after; we only need the render.
            orch.kernel.gate.resolve(gid, {"surviving": ["s0", "s1", "s2"]})
            await page.wait_for_function(
                "() => !document.querySelector"
                "(\".turn[data-turn='6'] .out.gated\")",
                timeout=15_000,
            )
            await asyncio.sleep(0.5)
            await _scroll_tail(orch_col)
            await _shot(
                page, "08b-run-proposal-approved", clip=await orch_col.bounding_box()
            )
            # let the cell drain (cancel + settle) before moving on.
            for _ in range(200):
                if 6 not in orch.kernel.bg:
                    break
                await asyncio.sleep(0.05)

            # ── turn 7: wb.cite → CiteProposal gate → FindingCard ───────────
            orch.step()
            await _wait_for(lambda: orch.kernel.gate.pending, timeout=15.0)
            (gid,) = orch.kernel.gate.pending
            await page.wait_for_selector(
                ".turn[data-turn='7'] .out.gated .cite-quotes", timeout=10_000
            )
            await _scroll_tail(orch_col)
            await _shot(page, "09-cite-proposal", clip=await orch_col.bounding_box())

            orch.kernel.gate.resolve(gid, {"signed": True, "by": "reviewer"})
            await page.wait_for_selector(
                ".turn[data-turn='7'] .out.finding", timeout=10_000
            )
            await asyncio.sleep(0.15)
            await _scroll_tail(orch_col)
            await _shot(page, "09b-finding", clip=await orch_col.bounding_box())

            # ── turn 8: §11 interrupt-and-send ──────────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".turn[data-turn='8'] .code-cell", timeout=10_000
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
            # server handler queues the text + `orch.step()` → turn 9 lands
            # with the ask bubble; the interrupted cell's output shows
            # `[interrupted by user after Ns]`.
            await page.wait_for_selector(
                ".turn[data-turn='9'] .ask-bubble", timeout=15_000
            )
            await asyncio.sleep(0.3)
            await _scroll_tail(orch_col)
            await _shot(
                page, "10-interrupt-and-send", clip=await orch_col.bounding_box()
            )

            # ── §7 var-tooltip: `.cc-var` `title` in a collapsed gist ──────
            # Native `title` tooltips don't render in headless screenshots, so
            # inject a positioned overlay showing the attribute value.
            var_loc = orch_col.locator(".turn[data-turn='3'] .cc-gist .cc-var").first
            title = await var_loc.get_attribute("title")
            print(f"  .cc-var[title] = {title!r}")
            assert title, "ns_summary tooltip not populated on .cc-var"
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
            orch.step()  # turn 10: "Acknowledged" (no tool call, parks)
            await page.wait_for_selector(".orch-col-wrap .scroll-new", timeout=10_000)
            await _shot(page, "13-scroll-new", clip=await orch_col.bounding_box())

            # ── 07 full column (all turns, cells expanded) ──────────────────
            await orch_col.locator(".column").evaluate(
                "(el) => { el.scrollTop = el.scrollHeight; }"
            )
            for head in await page.locator(
                ".orch-col .cc-head[role='button']"
            ).all():
                await head.click()
            await page.mouse.move(0, 0)
            await asyncio.sleep(0.1)
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await _shot(page, "07-full-column", full=True)

            # ── §2 rewind: hover turn-3 prose → click ↺ → confirm ───────────
            # Re-collapse cells so the prose block-actions are the ones we hit.
            for head in await page.locator(
                ".orch-col .cc-head[role='button']"
            ).all():
                await head.click()
            page.once("dialog", lambda d: asyncio.create_task(d.accept()))
            await orch_col.locator(
                ".turn[data-turn='3'] .asst-prose"
            ).first.hover()
            n_before = len(errors)
            await orch_col.locator(
                ".turn[data-turn='3'] .block-actions .bi-arrow-counterclockwise"
            ).first.click()
            # rewind marks events for turns ≥3 as `rewound`; the frontend
            # filter drops them so `.turn[data-turn='3']` unmounts.
            try:
                await page.wait_for_function(
                    "() => !document.querySelector"
                    "(\".turn[data-turn='3']\")",
                    timeout=10_000,
                )
                await asyncio.sleep(0.3)
                await orch_col.locator(".column").evaluate(
                    "(el) => { el.scrollTop = 0; }", timeout=5_000
                )
                await _shot(page, "11-rewind", clip=await orch_col.bounding_box())
            except Exception as e:  # noqa: BLE001
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
