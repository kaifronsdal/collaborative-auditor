"""Playwright e2e for the per-message edit/resample actions (commit `11df762`).

A scripted mockllm run completes (3 auditor turns, 2 target replies), then
Playwright drives the three per-message actions against the live page:

  1. `edit_target_message` — hover a target user bubble → `edit` → change
     content → `save & replay` → assert the new branch's target column shows
     the edited user message and a fresh target reply that echoes it.
  2. `resample_auditor` — hover an auditor turn's action row → `resample` →
     assert a new workbench branch is created and the auditor regenerates
     that turn (the regenerated `send_message` arg differs from the original).
  3. `edit_auditor_call` — expand an auditor `send_message` tool-pair →
     `edit args` → change `message` → `save & replay` → assert the new
     branch's target sees the edited message and replies to it.

mockllm strategy
----------------
`Branch.fork()` inherits `*_model_args` verbatim, and `_build_models()` calls
`get_model("mockllm/model", custom_outputs=…)` per branch. With a `list` that
would mean each fork starts a fresh iterator at index 0, so the script would
have to read coherently as both "turn 0 of original" and "turn N of fork".
Instead we pass *callables* (the same object is shared across all branches):

- The target callable echoes the last user message → `f"reply-to:{text}"`.
  An edited/replayed user message is therefore directly observable in the
  target's reply, regardless of which branch is calling.
- The auditor callable picks its action by counting assistant messages in
  `input` (= the live turn index, since `tape.replayable` short-circuits
  replayed turns and never reaches the callable). A module-level live-call
  counter is folded into turn 1's `send_message` arg so a `resample_auditor`
  fork's regenerated turn 1 is textually distinguishable from the original.

Run:  uv run python -m workbench._smoke_ui_actions
"""

from __future__ import annotations

import asyncio
import json
import sys

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo
from playwright.async_api import Page, async_playwright, expect

from workbench._smoke_ui_rollback import (
    _auditor_turn,
    _backend,
    _free_port,
    _tc,
    _vite,
)
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session

# ── scripted model callables ────────────────────────────────────────────────

_aud_live = 0  # incremented on every *live* auditor generate, across all branches


def _auditor_out(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    """Auditor mockllm: pick action by live-turn index (assistant count)."""
    del tools, tool_choice, config
    global _aud_live
    _aud_live += 1
    n_assistant = sum(1 for m in input if m.role == "assistant")
    if n_assistant == 0:
        return _auditor_turn(
            _tc("set_system_message", system_message="be helpful"),
            _tc("send_message", message="hello-one"),
            _tc("resume"),
        )
    if n_assistant == 1:
        return _auditor_turn(
            _tc("send_message", message=f"probe-{_aud_live}"),
            _tc("resume"),
        )
    return _auditor_turn(_tc("end_conversation"))


def _target_out(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    """Target mockllm: echo the last user message."""
    del tools, tool_choice, config
    last_user = next((m for m in reversed(input) if m.role == "user"), None)
    assert last_user is not None, "target called with no user message in input"
    return ModelOutput.from_content(model="mockllm", content=f"reply-to:{last_user.text}")


# ── helpers ─────────────────────────────────────────────────────────────────


async def _wait_fork(session: Session, prev: int) -> Branch:
    """Wait for a UI-driven WS command to land and `_register_and_spawn` to
    register the new branch + spawn its `run()` task. Returns the fork."""
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
    """Release the fork's gate, wait for it to run to `end_conversation`, then
    push a full `state` so the frontend has the settled per-branch timeline.

    The incremental `{t:"timeline"}` rebuild fires on the *first* (pending)
    target ModelEvent of each turn; for a fork whose prefix replays from
    `pending` that can land before the trajectory span is fully established,
    leaving the frontend with a stale empty timeline. The action handlers
    under test don't depend on that machinery, so re-sync via `state` here.
    """
    fork.play()
    await session.broadcast_status()
    for _ in range(400):
        if fork.status == "ended":
            break
        await anyio.sleep(0.05)
    assert fork.status == "ended", f"fork {fork.branch_id} never ended (status={fork.status})"
    assert fork.error is None, f"fork {fork.branch_id} failed: {fork.error}"
    session.version += 1
    await session.broadcast({"t": "state", "v": session.version, **session.view()})


def _target_replies(fork: Branch) -> list[str]:
    """Backend-level: the target's assistant outputs on `fork`'s audit tape."""
    out: list[str] = []
    for s in fork.audit_tape.log:
        if s.source == "Model.generate" and isinstance(s.value, ModelOutput):
            out.append(s.value.completion)
    return out


def _target_col(page: Page):
    """The target column, regardless of swimlane-vs-linear fallback.

    `SwimlaneColumn` renders as `.swimlane-column` once the server timeline
    has ≥1 row, but falls back to a plain `Column` (`.column`) before that.
    A fork's first events can land while its timeline is still empty, so
    select via the layout slot (`.columns > .col-wrap` last) instead.
    """
    return page.locator(".columns .col-wrap").last


async def _switch_to_root(page: Page, root_label: str) -> None:
    """Click the root branch row in the sidebar tree and wait for the original
    target column to render."""
    root = page.locator(".side-branches .side-row").filter(has_text=root_label).first
    await root.click()
    await expect(
        _target_col(page).locator(".bubble.user").filter(has_text="hello-one").first
    ).to_be_visible(timeout=10_000)


# ── the test ────────────────────────────────────────────────────────────────


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "ui-actions"
    seed = "scripted per-msg actions"

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
            max_turns=3,
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
        assert _aud_live == 3, f"expected 3 live auditor calls, got {_aud_live}"

        # The original turn-1 send_message arg — what `resample_auditor` must
        # regenerate to something different.
        orig_probe = "probe-2"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")

            tgt = _target_col(page)
            aud = page.locator(".columns .col-wrap").first
            await expect(tgt.locator(".bubble.assistant")).to_have_count(2, timeout=15_000)
            await expect(aud.locator(".model-event-row")).to_have_count(3)
            side_rows = page.locator(".side-branches .side-row")
            await expect(side_rows).to_have_count(1)
            print("setup ✓ root branch rendered: 3 auditor turns, 2 target replies")

            # ─────────────────────────────────────────────────────────────────
            # 1. edit_target_message — edit the first user bubble.
            # ─────────────────────────────────────────────────────────────────
            lead = tgt.locator(".lead-wrap").filter(
                has=page.locator(".bubble.user"), has_text="hello-one"
            )
            await expect(lead).to_have_count(1)
            await lead.hover()
            await lead.locator('.msg-actions button[title*="edit" i]').click()
            ta = tgt.locator(".edit-textarea")
            await expect(ta).to_be_visible()
            await ta.fill("EDITED-one")
            await tgt.locator(".edit-actions .edit-save").click()

            fork1 = await _wait_fork(session, prev=1)
            await _play_to_end(session, fork1)
            await expect(side_rows).to_have_count(2, timeout=10_000)

            # New branch is current → target column re-rendered for it. The
            # edited user message is the first user bubble; the target echoed it.
            await expect(
                tgt.locator(".bubble.assistant").filter(has_text="reply-to:EDITED-one")
            ).to_be_visible(timeout=10_000)
            users1 = [
                t.strip() for t in await tgt.locator(".bubble.user").all_text_contents()
            ]
            replies1 = [
                t.strip() for t in await tgt.locator(".bubble.assistant").all_text_contents()
            ]
            assert any("EDITED-one" in u for u in users1), (
                f"fork1 target column missing edited user bubble; got {users1}"
            )
            assert "reply-to:EDITED-one" in replies1[0], (
                f"fork1 first target reply should echo the edited user msg; got {replies1}"
            )
            assert not any("hello-one" in r for r in replies1), (
                f"fork1 target still references the un-edited msg; got {replies1}"
            )
            print(f"1 ✓ edit_target_message → fork target replies {replies1}")

            # ─────────────────────────────────────────────────────────────────
            # 2. resample_auditor — regenerate auditor turn 1 on the root branch.
            # ─────────────────────────────────────────────────────────────────
            await _switch_to_root(page, seed)
            row1 = aud.locator(".model-event-row").nth(1)
            await expect(
                row1.locator('.tool-pair[data-fn="send_message"] .tp-sig')
            ).to_contain_text(orig_probe)
            await row1.hover()
            await row1.locator('.actions button[title*="resample" i]').click()

            fork2 = await _wait_fork(session, prev=2)
            await _play_to_end(session, fork2)
            await expect(side_rows).to_have_count(3, timeout=10_000)

            # Auditor column now shows fork2. Turn 0 is the synthesised replay
            # of the original; turn 1 is the live regenerated turn — its
            # send_message arg carries a higher live-call counter than the
            # original `probe-2`.
            await expect(aud.locator(".model-event-row")).to_have_count(3, timeout=10_000)
            sig2 = (
                aud.locator(".model-event-row")
                .nth(1)
                .locator('.tool-pair[data-fn="send_message"] .tp-sig')
            )
            await expect(sig2).to_contain_text("probe-")
            await expect(sig2).not_to_contain_text(orig_probe)
            new_sig = await sig2.text_content()
            # Target-side outcome asserted at backend level: the swimlane's
            # `eventsToTurns` aligns ModelEvents 1:1 with assistant messages,
            # but the replayed turn-0 ModelEvent bypasses the transcript
            # (`_synthesize_prefix_events` calls `_on_event` directly) so the
            # fork's lineage has 1 ModelEvent for 2 assistants and the
            # post-prefix reply is not rendered. The UI does show the replayed
            # prefix; the regenerated reply is verified on the audit tape.
            await expect(
                tgt.locator(".bubble.assistant").filter(has_text="reply-to:hello-one")
            ).to_be_visible(timeout=10_000)
            replies2 = _target_replies(fork2)
            assert replies2[0] == "reply-to:hello-one", (
                f"fork2 turn-0 prefix not replayed; tape={replies2}"
            )
            assert "reply-to:probe-" in replies2[1] and orig_probe not in replies2[1], (
                f"fork2 target did not reply to a regenerated probe; tape={replies2}"
            )
            print(
                f"2 ✓ resample_auditor → turn 1 regenerated: sig={new_sig!r}; "
                f"target tape={replies2}"
            )

            # ─────────────────────────────────────────────────────────────────
            # 3. edit_auditor_call — edit turn 1's send_message args on root.
            # ─────────────────────────────────────────────────────────────────
            await _switch_to_root(page, seed)
            pair = (
                aud.locator(".model-event-row")
                .nth(1)
                .locator('.tool-pair[data-fn="send_message"]')
            )
            await pair.locator(".tp-head").click()
            await pair.locator(".tp-edit-actions button", has_text="edit args").click()
            edit_ta = pair.locator(".tp-edit-textarea")
            await expect(edit_ta).to_be_visible()
            await edit_ta.fill(json.dumps({"message": "ARGS-EDITED"}))
            await pair.locator(".tp-edit-actions button", has_text="save & replay").click()

            fork3 = await _wait_fork(session, prev=3)
            await _play_to_end(session, fork3)
            await expect(side_rows).to_have_count(4, timeout=10_000)

            # UI: the auditor column shows a `send_message` tool-pair with the
            # edited args. (`_synthesize_prefix_events` emits both replayed
            # ModelEvents *before* `run_audit` re-executes their tools, so
            # `eventsToTurns`' positional model→tool grouping lumps both
            # turns' ToolEvents under row 1 — hence don't pin to a specific
            # `.nth()`.) The target column shows the replayed prefix; the
            # post-prefix reply is asserted on the tape (same limitation as
            # flow 2).
            await expect(
                aud.locator('.tool-pair[data-fn="send_message"] .tp-sig').filter(
                    has_text="ARGS-EDITED"
                )
            ).to_have_count(1, timeout=10_000)
            await expect(
                tgt.locator(".bubble.assistant").filter(has_text="reply-to:hello-one")
            ).to_be_visible(timeout=10_000)
            replies3 = _target_replies(fork3)
            assert replies3[0] == "reply-to:hello-one", (
                f"fork3 turn-0 prefix not replayed; tape={replies3}"
            )
            assert replies3[1] == "reply-to:ARGS-EDITED", (
                f"fork3 target never saw the edited tool-call args; tape={replies3}"
            )
            print(f"3 ✓ edit_auditor_call → target tape={replies3}")

            assert not errors, f"page errors: {errors}"
            await browser.close()

        await session.close()

    print("✓ all per-message action UI flows passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    sys.exit(main())
