"""Capture deterministic screenshots of the M1 orchestrator column.

Drives a mockllm-scripted `Orchestrator` through a fixed sequence of `python`
tool calls covering every card type (`.out`, `.out.gated`, `.out-stream`,
DataFrame HTML, plotly, RunHandle, traceback), and captures the frontend at
each key state. Writes PNGs to ``frontend-wb/screenshots/m1/``.

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
from workbench._smoke_m1_run import make_task
from workbench.server import sessions
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "frontend-wb" / "screenshots" / "m1"


# ── scripted orchestrator model ─────────────────────────────────────────────
# Turn 1: display + stable-update, print, DataFrame last-expr.
# Turn 2: gated ask_human (blocks until resolve()).
# Turn 3: run_eval → RunHandle card ticks to done.
# Turn 4: plotly figure.
# Turn 5: cell error → traceback path.
# Turn 6: no tool call (parks).
CELLS = [
    (
        "Let me start by displaying a stable value and a DataFrame.",
        "dh = display('v1', display_id='job')\n"
        "dh.update('v2')\n"
        "print('hello from stdout')\n"
        "pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})",
    ),
    (
        "I need to ask the human before proceeding.",
        "ans = await wb.ask_human('proceed?', ['y', 'n'])\nans",
    ),
    (
        "Launching an eval and waiting for it to finish.",
        "h = wb.run_eval(make_task('demo', 3), model='mockllm/model',\n"
        "                description='three mockllm samples')\n"
        "await h.wait()\nh",
    ),
    (
        "Here is a bar chart of the counts.",
        "fig = px.bar(x=['a', 'b', 'c'], y=[4, 5, 6])\n"
        "fig.update_layout(width=420, height=260)\nfig",
    ),
    (
        "This cell will raise.",
        "1 / 0",
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
    return ModelOutput.from_content(model="mockllm", content="done.")


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


async def _amain() -> None:  # noqa: PLR0915
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
            max_turns=len(CELLS) + 1,
        )
        orch = session.orchestrator
        assert orch is not None
        # Seed `make_task` so cell 3 can build a Task without importing it.
        orch.kernel.shell.user_ns["make_task"] = make_task

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

            # ── 01 StartView → Orchestrator tab ─────────────────────────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session=fresh")
            await page.wait_for_selector(".start-view", timeout=15_000)
            await page.get_by_role("button", name="Orchestrator").click()
            await page.wait_for_selector(".start-card .start-btn")
            await _shot(page, "01-orch-start")

            # ── connect to the scripted session; orch column mounts ──────────
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
            await page.wait_for_selector(".orch-col-wrap", timeout=15_000)
            orch_col = page.locator(".orch-col-wrap")

            # ── turn 1: display/update/print/DataFrame ──────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='1'] .out.html", timeout=15_000
            )
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await asyncio.sleep(0.15)
            await _shot(page, "02-turn1-outputs", clip=await orch_col.bounding_box())

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

            # ── turn 3: run_eval → RunHandle card ───────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='3'] .fx-out", timeout=30_000
            )
            # let the watcher tick to done (mockllm is fast)
            for _ in range(200):
                h = orch.kernel.shell.user_ns.get("h")
                if h is not None and h.finished:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            await orch_col.locator(".column").evaluate(
                "(el) => { el.scrollTop = el.scrollHeight; }"
            )
            await _shot(page, "04-runcard", clip=await orch_col.bounding_box())

            # ── turn 4: plotly ──────────────────────────────────────────────
            orch.step()
            await page.wait_for_selector(".orch-col .plotly-host", timeout=15_000)
            # give the CDN <script> a moment; plot mount is best-effort here.
            await asyncio.sleep(1.5)
            await orch_col.locator(".column").evaluate(
                "(el) => { el.scrollTop = el.scrollHeight; }"
            )
            await _shot(page, "05-plotly", clip=await orch_col.bounding_box())

            # ── turn 5: 1/0 → traceback path ────────────────────────────────
            orch.step()
            await page.wait_for_selector(
                ".orch-col .turn[data-turn='5'] .code-cell", timeout=10_000
            )
            await asyncio.sleep(0.3)
            await orch_col.locator(".column").evaluate(
                "(el) => { el.scrollTop = el.scrollHeight; }"
            )
            await _shot(page, "06-traceback", clip=await orch_col.bounding_box())

            # ── 07 full column (all turns) ──────────────────────────────────
            # Reveal any collapsed code cells so the full-page shot shows
            # everything.
            for btn in await page.locator(".orch-col .cc-more").all():
                if "show" in ((await btn.text_content()) or ""):
                    await btn.click()
            await orch_col.locator(".column").evaluate("(el) => { el.scrollTop = 0; }")
            await _shot(page, "07-full-column", full=True)

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
