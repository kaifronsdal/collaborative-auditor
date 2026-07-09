"""Content-coverage UI e2e: target makes tool calls + markdown + reasoning;
Playwright asserts every shape renders.

Builds on `_smoke_ui_rollback` infrastructure but with a richer target script:
the target's first reply makes a tool call (`get_weather`), the auditor
supplies the result via `send_tool_call_result`, the target then replies in
markdown. Asserts the target column shows the tool-call card + paired result,
markdown renders (`<h2>`, `<code>`, `<li>` in DOM), and the auditor column's
custom renderers (`create_tool`, `send_tool_call_result`) show meaningful
bodies, not raw JSON.

Run:  uv run python -m workbench._smoke_ui_content
"""

from __future__ import annotations

import asyncio

import anyio
from inspect_ai.model import (
    ChatMessageAssistant,
    ContentReasoning,
    ContentText,
    ModelOutput,
)
from playwright.async_api import async_playwright, expect

from workbench._smoke_fixtures import (
    _auditor_turn,
    _backend,
    _free_port,
    _target_tool_call,
    _tc,
    _vite,
)
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session


def _target_md(md: str) -> ModelOutput:
    return ModelOutput.from_content(model="mockllm", content=md)


def _target_reasoning(thought: str, answer: str) -> ModelOutput:
    out = ModelOutput(
        model="mockllm",
        choices=[
            {
                "message": ChatMessageAssistant(
                    content=[ContentReasoning(reasoning=thought), ContentText(text=answer)]
                ),
                "stop_reason": "stop",
            }
        ],
    )
    return out


WEATHER_TOOL = {
    "type": "object",
    "properties": {"location": {"type": "string"}},
    "required": ["location"],
}

AUDITOR = [
    _auditor_turn(
        _tc("set_system_message", system_message="You are a weather assistant."),
        _tc(
            "create_tool",
            environment_description="Weather API",
            name="get_weather",
            description="Get current weather.",
            parameters=WEATHER_TOOL,
        ),
        _tc("send_message", message="Weather in Seattle?"),
        _tc("resume"),
    ),
    # Target replied with a tool_call → supply the result.
    _auditor_turn(
        _tc("send_tool_call_result", tool_call_id="tc-weather-1", result="72°F clear"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="Summarize in markdown"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="Is that beach weather?"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]

TARGET = [
    _target_tool_call("get_weather", "tc-weather-1", location="Seattle, WA"),
    _target_md("Seattle is **72°F** and clear."),
    _target_md(
        "## Summary\n\n- Temp: `72°F`\n- Sky: clear\n\n```json\n{\"t\":72}\n```"
    ),
    _target_reasoning(
        "72°F is warm enough for swimming and not too hot.",
        "Yes — good beach weather.",
    ),
]


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "ui-content"

    async with _backend(ws_port), _vite(ws_port, ui_port):
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "c0",
            seed="content coverage",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=len(AUDITOR),
            auditor_model_args={"custom_outputs": list(AUDITOR)},
            target_model_args={"custom_outputs": list(TARGET)},
        )
        session.branches["c0"] = b
        session.current = "c0"
        b.play()
        await asyncio.create_task(b.run())
        assert b.error is None, f"branch failed: {b.error}"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 960})
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")

            # Both columns render via `SwimlaneColumn`; select by layout slot.
            aud = page.locator(".columns .col-wrap").first
            tgt = page.locator(".columns .col-wrap").last
            await expect(tgt.locator(".model-event-row").first).to_be_visible(
                timeout=15_000
            )

            # ── target tool-call + result render as a .tool-pair card ──
            tgt_pairs = tgt.locator(".tool-pair")
            await expect(tgt_pairs).to_have_count(1)
            await expect(tgt_pairs.locator(".tp-fn")).to_have_text("get_weather")
            # expand it → result body shows the auditor-supplied "72°F clear"
            await tgt_pairs.locator(".tp-head").click()
            body = await tgt_pairs.locator(".tp-body").text_content()
            assert "72°F clear" in (body or ""), f"target tool result missing; body={body!r}"
            print("UI ✓ target tool-call card + paired result render")

            # ── markdown renders (h2, code, li, pre) ──
            await expect(tgt.locator(".md h2")).to_have_count(1)
            await expect(tgt.locator(".md li")).to_have_count(2)
            assert await tgt.locator(".md code").count() >= 1, "no inline code rendered"
            assert await tgt.locator(".md pre").count() >= 1, "no fenced code block"
            print("UI ✓ markdown rendered (h2, li, code, pre)")

            # ── reasoning block ──
            reasoning = tgt.locator(".reasoning")
            assert await reasoning.count() >= 1, "no .reasoning block"
            assert "warm enough" in (await reasoning.first.text_content() or "")
            print("UI ✓ reasoning content block rendered")

            # ── auditor custom renderers: create_tool body shows name+desc, not raw JSON ──
            ct = aud.locator('.tool-pair[data-fn="create_tool"]')
            await ct.locator(".tp-head").click()
            ct_body = await ct.locator(".tp-body").text_content()
            assert "get_weather" in (ct_body or ""), f"create_tool body: {ct_body!r}"
            # Custom renderer ⇒ a .tr-* element, not the generic args/result <pre> fallback.
            assert await ct.locator(".tp-body [class^='tr-']").count() > 0, (
                f"create_tool fell back to generic renderer; body={ct_body!r}"
            )
            stcr = aud.locator('.tool-pair[data-fn="send_tool_call_result"]')
            await stcr.locator(".tp-head").click()
            assert "72°F clear" in (await stcr.locator(".tp-body").text_content() or "")
            print("UI ✓ auditor custom renderers (create_tool, send_tool_call_result)")

            # ── raw modal opens (not inline) ──
            await aud.locator(".model-event-row").first.hover()
            await aud.locator('.actions button[title*="raw" i]').first.click()
            await expect(page.locator(".raw-modal")).to_be_visible()
            await page.keyboard.press("Escape")
            await expect(page.locator(".raw-modal")).to_have_count(0)
            print("UI ✓ raw modal opens + closes on Escape")

            assert not errs, f"page errors: {errs}"
            await browser.close()
        await session.close()
    print("✓ all content-coverage assertions passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
