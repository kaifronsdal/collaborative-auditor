"""Focused M0 screenshots: expanded target-column ``ToolPair`` (bash) + the
sticky more/less toggle mid-scroll.

Scripted so the *target* makes a ``bash`` tool call (long cmd, long result) —
this is the ``.tr-inspect`` → ``ToolCallView`` path where the styling issues
were reported. Also covers the auditor column's long ``set_system_message``
(``ExpandablePanel`` inside a ``.bubble``) so both sticky-toggle sites are on
screen.

Writes PNGs to ``frontend-wb/screenshots/m1/``.

Run:  uv run python -m workbench._screenshot_toolpair
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolCall
from playwright.async_api import Page, async_playwright

from workbench._smoke_fixtures import (
    _auditor_turn,
    _backend,
    _free_port,
    _target,
    _tc,
    _vite,
)
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "frontend-wb" / "screenshots" / "m1"


def _target_tool_call(fn: str, call_id: str, **args: object) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content="I'll check.")
    out.choices[0].message.tool_calls = [
        ToolCall(id=call_id, function=fn, type="function", arguments=dict(args))
    ]
    return out


# Long-ish bash command (multi-line, >20 lines so ToolCallView's input
# ExpandablePanel mounts its more/less toggle) + a long result (>15 lines →
# output ExpandablePanel toggle mounts too).
_BASH_CMD = (
    "for f in $(find /var/log -name '*.log' -mtime -1); do\n"
    '  echo "=== $f ==="\n'
    "  grep -E 'ERROR|WARN' \"$f\" | tail -20\n"
    "done\n"
    + "\n".join(
        f"# padding line {i} to overflow the 20-line input panel" for i in range(22)
    )
)
_BASH_RESULT = "\n".join(
    f"/var/log/app.log:{1200 + i}: ERROR failed to acquire lock on shard {i % 4} "
    f"(retry {i}) — connection reset by peer at 10.0.{i}.{i * 7 % 256}"
    for i in range(60)
)

_LONG_SYS = "You are a helpful assistant.\n\n" + "\n".join(
    f"Rule {i}: this line exists to push the system bubble past the 280px "
    "collapse threshold so the ExpandablePanel more/less toggle mounts."
    for i in range(24)
)

AUDITOR = [
    _auditor_turn(
        _tc("set_system_message", system_message=_LONG_SYS),
        _tc(
            "create_tool",
            environment_description="shell",
            name="bash",
            description="Run a shell command.",
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        ),
        _tc("send_message", message="Check the logs for recent errors."),
        _tc("resume"),
    ),
    _auditor_turn(
        _tc("send_tool_call_result", tool_call_id="tc-bash-1", result=_BASH_RESULT),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]
TARGET = [
    _target_tool_call("bash", "tc-bash-1", cmd=_BASH_CMD),
    _target("Found 60 lock-acquisition errors across 4 shards."),
]


async def _shot(page: Page, name: str, *, clip=None, full: bool = False) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    await page.screenshot(path=path, clip=clip, full_page=full)
    print(f"  {path.relative_to(REPO)}")


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "tp-shots"

    async with _backend(ws_port), _vite(ws_port, ui_port):
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "b0",
            seed="tool-pair styling coverage",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=len(AUDITOR),
            auditor_model_args={"custom_outputs": list(AUDITOR)},
            target_model_args={"custom_outputs": list(TARGET)},
        )
        session.branches["b0"] = b
        session.current = "b0"
        b.play()
        await asyncio.create_task(b.run())

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: (
                    errors.append(f"console: {m.text}") if m.type == "error" else None
                ),
            )

            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
            await page.wait_for_selector(".columns .column", timeout=15_000)
            tgt = page.locator(".columns .col-wrap").last

            # ── 19: expanded bash ToolPair (target column) ──────────────────
            #    Align `.tp-head` to the top of the scroll container so the
            #    card's header + body padding/labels are all in frame.
            tp = tgt.locator('.tool-pair[data-fn="bash"]')
            await tp.wait_for(timeout=10_000)
            await tp.locator(".tp-head").click()
            await tp.locator(".tp-body").wait_for(timeout=5_000)
            await asyncio.sleep(0.15)
            col = tgt.locator(".column")
            await tp.evaluate("el => el.scrollIntoView({block: 'start'})")
            # `.column-head` is sticky — nudge past it so it doesn't overlap.
            await col.evaluate("el => { el.scrollTop -= 44; }")
            await page.mouse.move(0, 0)
            await asyncio.sleep(0.1)
            box = await tp.bounding_box()
            clip = {
                "x": max(0, box["x"] - 8),
                "y": max(0, box["y"] - 8),
                "width": box["width"] + 16,
                "height": min(box["height"] + 16, 960 - box["y"]),
            }
            await _shot(page, "19-toolpair-expanded", clip=clip)

            # ── 19b: expand the output ExpandablePanel, scroll into its ─────
            #        middle; the `less` toggle should sit at the viewport
            #        bottom (sticky) rather than off-screen at content end.
            for btn in await tp.locator("[data-expandable-panel] button").all():
                if (await btn.text_content() or "").strip().startswith("more"):
                    await btn.click()
            await asyncio.sleep(0.1)
            await tp.evaluate("el => el.scrollIntoView({block: 'start'})")
            # Push ~600px into the output block: past the input section,
            # well before the natural end of the 60-line result.
            await col.evaluate("el => { el.scrollTop += 700; }")
            await page.mouse.move(0, 0)
            await asyncio.sleep(0.15)
            await _shot(page, "19b-toolpair-sticky", clip=await tgt.bounding_box())

            # ── 19c: auditor `send_tool_call_result` expanded — generic
            #        `.tp-slot pre` fallback path (long result). ───────────────
            aud = page.locator(".columns .col-wrap").first
            stcr = aud.locator('.tool-pair[data-fn="send_tool_call_result"]')
            await stcr.locator(".tp-head").click()
            await stcr.locator(".tp-body").wait_for(timeout=5_000)
            await asyncio.sleep(0.15)
            await stcr.scroll_into_view_if_needed()
            await page.mouse.move(0, 0)
            sbox = await stcr.bounding_box()
            await _shot(
                page,
                "19c-toolpair-auditor",
                clip={
                    "x": max(0, sbox["x"] - 8),
                    "y": max(0, sbox["y"] - 8),
                    "width": sbox["width"] + 16,
                    "height": min(sbox["height"] + 16, 960),
                },
            )

            if errors:
                print("\npage errors:")
                for e in errors:
                    print(f"  ! {e}")

            await browser.close()
        await session.close()


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
