"""Capture a deterministic set of UI screenshots for visual review.

Reuses the mockllm scripted-auditor from `_smoke_ui_rollback` so the rendered
content is identical run-to-run. Writes PNGs to `frontend-wb/screenshots/`.

Run:  uv run python -m workbench._screenshot_ui
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
from playwright.async_api import Page, async_playwright

from workbench._smoke_ui_rollback import (
    AUDITOR_SCRIPT,
    TARGET_SCRIPT,
    _backend,
    _free_port,
    _vite,
)
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session

OUT = Path(__file__).resolve().parents[2] / "frontend-wb" / "screenshots"


async def _shot(page: Page, name: str, *, full: bool = False, clip=None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    await page.screenshot(path=path, full_page=full, clip=clip)
    print(f"  {path.relative_to(OUT.parent.parent)}")


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()

    async with _backend(ws_port), _vite(ws_port, ui_port):
        # ── session with completed rollback branch (swimlanes + tool pairs) ──
        sid = "shots"
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "rb",
            seed="scripted rollback for screenshots",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=len(AUDITOR_SCRIPT),
            auditor_model_args={"custom_outputs": list(AUDITOR_SCRIPT)},
            target_model_args={"custom_outputs": list(TARGET_SCRIPT)},
        )
        session.branches["rb"] = b
        session.current = "rb"
        b.play()
        await asyncio.create_task(b.run())

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})

            print("Capturing screenshots:")

            # ── 01 StartView (fresh session, no branch) ──
            await page.goto(f"http://127.0.0.1:{ui_port}/?session=fresh")
            await page.wait_for_selector(".start-view, .start-textarea", timeout=10_000)
            await _shot(page, "01-start-view")
            # config disclosure open
            cfg = page.locator(".picker-config-toggle").first
            if await cfg.count() > 0:
                await cfg.click()
                await _shot(page, "01b-start-config-open")

            # ── 02 DeskView — full (rollback session) ──
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
            await page.wait_for_selector(".lane-gantt-host", timeout=10_000)
            await _shot(page, "02-desk-full")
            await _shot(page, "02b-desk-full-page", full=True)

            # ── 03 Sidebar ──
            sb = page.locator(".sidebar")
            if await sb.count() > 0:
                box = await sb.bounding_box()
                if box:
                    await _shot(page, "03-sidebar", clip=box)

            # ── 04 Runline + seed strip ──
            rl = page.locator(".runline")
            box = await rl.bounding_box()
            if box:
                # include the seed strip above it
                await _shot(
                    page,
                    "04-runline",
                    clip={"x": box["x"], "y": 0, "width": box["width"], "height": box["y"] + box["height"] + 2},
                )

            # ── 05 Auditor column (top + a tool-pair expanded) ──
            aud = page.locator(".columns .column").first
            # Column auto-scrolls to bottom on mount — scroll to top so the
            # system-prompt bubble (and its [more] clamp) is in frame.
            await aud.evaluate("(el) => { el.scrollTop = 0; }")
            box = await aud.bounding_box()
            if box:
                await _shot(page, "05-auditor-col", clip=box)
            # expand the first 2 tool pairs
            heads = aud.locator(".tp-head")
            for i in range(min(2, await heads.count())):
                await heads.nth(i).click()
            await _shot(page, "05b-auditor-tools-expanded", clip=await aud.bounding_box())

            # ── 06 Target column (swimlane) ──
            tgt = page.locator(".swimlane-column")
            box = await tgt.bounding_box()
            if box:
                await _shot(page, "06-target-swimlane", clip=box)
            # switch to branch 1 (click first gantt row's label cell)
            await page.locator('.lane-gantt-host [role="row"]').first.locator("> div").first.click()
            await _shot(page, "06b-target-branch1", clip=await tgt.bounding_box())

            # ── 07 Gantt close-up ──
            gantt = page.locator(".lane-gantt-host")
            box = await gantt.bounding_box()
            if box:
                pad = 8
                await _shot(
                    page, "07-gantt",
                    clip={"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                          "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad},
                )

            # ── 08 Composer (3 states) ──
            comp = page.locator(".composer-zone")
            box = await comp.bounding_box()
            if box:
                await _shot(page, "08-composer-empty", clip=box)
            await page.fill(".composer-input", "some feedback text")
            await _shot(page, "08b-composer-with-text", clip=await comp.bounding_box())
            # (dest toggle removed — composer is auditor-only)

            # ── 09 One model-event-row close-up (auditor) ──
            row = aud.locator(".model-event-row").nth(1)
            if await row.count() > 0:
                await row.scroll_into_view_if_needed()
                box = await row.bounding_box()
                if box:
                    await _shot(page, "09-model-event-row", clip=box)

            # ── 10 Error banner ──
            await page.evaluate(
                "() => window.__zustand_set?.({error:'example error message for review'})"
            )
            # fallback: trigger via store if exposed
            if await page.locator(".error-banner").count() == 0:
                # inject via console using the store (it's a module — try the global hook vite exposes in dev)
                pass
            else:
                eb = page.locator(".error-banner")
                box = await eb.bounding_box()
                if box:
                    await _shot(page, "10-error-banner", clip=box)

            await browser.close()
        await session.close()
    print(f"\n{len(list(OUT.glob('*.png')))} screenshots → {OUT}")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
