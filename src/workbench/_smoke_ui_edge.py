"""Playwright e2e for the turn-0 `edit_auditor_call` edge case.

A turn-0 edit produces a child whose shared L2 prefix carries no
`GEN_SOURCE` step (only `today` etc.), so `build_auditor_timeline` emits
the child span with ``branched_from=None`` while still nesting it under
the parent. The frontend's `splice()` (core.ts:404-408) handles a null
anchor by clearing `out` and contributing nothing from that ancestor —
i.e. the child's rendered lineage is exactly its own content, no parent
prefix. This test pins that contract end-to-end against the live page,
plus the gantt path (`computeFlatSwimlaneRows` / `computeBranchMappings`
both tolerate a `branchedFrom: null` branch row).

Backend wire-level coverage of the same scenario lives in
`_smoke_edge_cases.e1_edit_turn0`.

Run:  uv run python -m workbench._smoke_ui_edge
"""

from __future__ import annotations

import asyncio
import json
import sys

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo
from playwright.async_api import Page, async_playwright, expect

from workbench._smoke_fixtures import (
    _auditor_turn,
    _backend,
    _free_port,
    _tc,
    _vite,
)
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session, build_auditor_timeline

# ── scripted model callables (2-turn base) ─────────────────────────────────


def _auditor_out(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    del tools, tool_choice, config
    n_assistant = sum(1 for m in input if m.role == "assistant")
    if n_assistant == 0:
        return _auditor_turn(
            _tc("set_system_message", system_message="be helpful"),
            _tc("send_message", message="hello-one"),
            _tc("resume"),
        )
    return _auditor_turn(_tc("end_conversation"))


def _target_out(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    del tools, tool_choice, config
    last_user = next(m for m in reversed(input) if m.role == "user")
    return ModelOutput.from_content(model="mockllm", content=f"reply-to:{last_user.text}")


# ── helpers (lifted from _smoke_ui_actions.py) ──────────────────────────────


async def _wait_fork(session: Session, prev: int) -> Branch:
    for _ in range(200):
        if len(session.branches) > prev and len(session.branch_tasks) > 0:
            break
        await anyio.sleep(0.05)
    assert len(session.branches) > prev, (
        f"no new branch after action (still {len(session.branches)})"
    )
    assert session.current is not None
    return session.branches[session.current]


async def _play_to_end(session: Session, fork: Branch) -> None:
    fork.play()
    await session.broadcast_status()
    for _ in range(400):
        if fork.status == "ended":
            break
        await anyio.sleep(0.05)
    assert fork.status == "ended", f"fork never ended (status={fork.status})"
    assert fork.error is None, f"fork failed: {fork.error}"
    session.version += 1
    await session.broadcast({"t": "state", "v": session.version, **session.view()})


def _aud_col(page: Page):
    return page.locator(".columns .col-wrap").first


# ── the test ────────────────────────────────────────────────────────────────


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "ui-edge-t0"
    seed = "turn-0 edit edge case"

    async with _backend(ws_port), _vite(ws_port, ui_port):
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "rb",
            seed=seed,
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=2,
            auditor_model_args={"custom_outputs": _auditor_out},
            target_model_args={"custom_outputs": _target_out},
        )
        session.branches["rb"] = b
        session.current = "rb"
        b.play()
        task = asyncio.create_task(b.run())
        session.branch_tasks.append(task)
        await task
        assert b.error is None, f"root branch failed: {b.error}"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")

            aud = _aud_col(page)
            await expect(aud.locator(".model-event-row")).to_have_count(2, timeout=15_000)
            print("setup ✓ root branch rendered: 2 auditor turns")

            # ── edit_auditor_call @ turn_index=0 ────────────────────────────
            pair = (
                aud.locator(".model-event-row")
                .nth(0)
                .locator('.tool-pair[data-fn="send_message"]')
            )
            await pair.locator(".tp-head").click()
            await pair.locator(".tp-edit-actions button", has_text="edit args").click()
            edit_ta = pair.locator(".tp-edit-textarea")
            await expect(edit_ta).to_be_visible()
            await edit_ta.fill(json.dumps({"message": "EDITED-T0"}))
            await pair.locator(".tp-edit-actions button", has_text="save & replay").click()

            fork = await _wait_fork(session, prev=1)
            await _play_to_end(session, fork)

            # ── backend: child span nested under parent, branched_from=None ─
            tl = build_auditor_timeline(session)
            base_span = next(s for s in tl["root"]["branches"] if s["id"] == "rb")
            kids = {s["id"]: s for s in base_span["branches"]}
            assert fork.branch_id in kids, (
                f"child {fork.branch_id!r} not nested under base in auditor "
                f"timeline; base.branches = {sorted(kids)}"
            )
            child_span = kids[fork.branch_id]
            assert child_span["branched_from"] is None, (
                f"turn-0-edit child should have branched_from=None (no "
                f"GEN_SOURCE step in shared prefix); got "
                f"{child_span['branched_from']!r}"
            )
            assert len(child_span["content"]) > 0, (
                "child span content is empty — convertServerTimeline would drop it"
            )
            print(
                f"backend ✓ child {fork.branch_id!r} nested under rb, "
                f"branched_from=None, {len(child_span['content'])} events"
            )

            # ── UI: auditor column = child's own content only (splice took
            #    zero from parent). The EDITED T0 is row 0; live T1 is row 1;
            #    the parent's original `hello-one` send_message must NOT
            #    appear anywhere (it would if splice() had spliced parent
            #    content in, or thrown and left a stale render).
            await expect(aud.locator(".model-event-row")).to_have_count(2, timeout=10_000)
            sig0 = (
                aud.locator(".model-event-row")
                .nth(0)
                .locator('.tool-pair[data-fn="send_message"] .tp-sig')
            )
            await expect(sig0).to_contain_text("EDITED-T0")
            await expect(
                aud.locator('.tool-pair[data-fn="send_message"] .tp-sig').filter(
                    has_text="hello-one"
                )
            ).to_have_count(0)
            await expect(
                aud.locator(".model-event-row")
                .nth(1)
                .locator('.tool-pair[data-fn="end_conversation"]')
            ).to_have_count(1)
            print("UI ✓ auditor lineage = [EDITED-T0, end_conversation]; no parent prefix")

            # ── UI: gantt renders the child as a branch row (rows.length>1
            #    triggers `.lane-gantt-host`); computeFlatSwimlaneRows keys a
            #    null-anchor branch as `…/branch-null-1` and computeBranchMappings
            #    falls back to the parent's start time via resolveForkTimestamp.
            await expect(aud.locator(".lane-gantt-host")).to_be_visible()
            gantt_rows = aud.locator('.lane-gantt-host [role="row"]')
            n_rows = await gantt_rows.count()
            # parent (auditor wrapper) + rb + child = 3
            assert n_rows == 3, (
                f"expected 3 gantt rows (root + base + null-anchor child); got {n_rows}"
            )
            print(f"UI ✓ gantt rendered {n_rows} rows including null-anchor child")

            # `splice()` throwing on a missing anchor would surface as a
            # pageerror; an empty lineage would have failed the row-count
            # assertion above.
            assert not errors, f"page errors: {errors}"
            await browser.close()

        await session.close()

    print("✓ turn-0 edit_auditor_call: branched_from=None handled end-to-end")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    sys.exit(main())
