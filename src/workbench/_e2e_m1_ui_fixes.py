"""Playwright e2e for the M1 live-use UI fixes (07f6a1d + f34b431).

Drives the real UI (in-process backend + vite dev + headless chromium) on
free ports — the user's ``wb-server``/``wb-vite`` on 8765/5173 are left
alone. Each case gets a fresh session id.

Cases
-----
1. **newAudit → fresh session** — populate ``?session=<t1>`` with a branch,
   click ``.side-new``, assert ``page.url`` swapped to a *different*
   ``?session=`` and the branches rail is gone (i.e. we didn't just append
   another root to the same session).
2. **unqueue** — paused M0 branch, type into the composer + Enter → ghost
   ``.bubble.ghost`` appears; hover it, click × in ``.queued-actions``,
   assert the ghost drops (1→0) and the backend ``queued['auditor']`` list
   drained.
3. **redacted reasoning** — mockllm target returns
   ``ContentReasoning(reasoning=<base64 blob>, redacted=True)``; assert the
   target column renders ``.reasoning-redacted`` with the placeholder text
   and NOT the raw blob.
4. **pause interrupts** — running branch with a slow (30s) mockllm auditor
   generate; click ``.primary-pause`` mid-generate, assert
   ``.runline.status-paused`` and backend ``b.status == 'paused'`` land in
   <2s (c2004c4: ``Branch.pause()`` cancels ``_gen_scope``). Skipped if the
   fix commit isn't in ``git log -20``.

Run:  uv run python -m workbench._e2e_m1_ui_fixes
"""

from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
import time
import traceback
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.tool import ToolChoice, ToolInfo
from playwright.async_api import Page, async_playwright, expect

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

# ── shared mockllm scripts ──────────────────────────────────────────────────

# Minimal 2-turn auditor: send one message, then end.
AUDITOR_2 = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="hello"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]

# Case 3 target: assistant reply carries a redacted reasoning block whose
# ``.reasoning`` is an opaque base64 blob — the UI must render the
# placeholder, never the blob.
BLOB = "QkFTRTY0QkxPQmRlYWRiZWVmY2FmZWJhYmU="


def _target_redacted() -> ModelOutput:
    return ModelOutput(
        model="mockllm",
        choices=[
            {
                "message": ChatMessageAssistant(
                    content=[
                        ContentReasoning(reasoning=BLOB, redacted=True),
                        ContentText(text="visible answer"),
                    ]
                ),
                "stop_reason": "stop",
            }
        ],
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _session_param(url: str) -> str | None:
    q = parse_qs(urlparse(url).query)
    return q.get("session", [None])[0]


async def _mk_branch(
    sid: str,
    bid: str,
    *,
    auditor: Any,
    target: Any,
    play: bool,
    max_turns: int = 2,
) -> tuple[Session, Branch]:
    """Register a mockllm branch on a fresh session under ``sessions[sid]``.

    ``play=True`` runs it to completion before returning; ``play=False``
    spawns the ``run()`` task but leaves the gate closed so the branch parks
    at ``status='paused'`` with an empty column. ``auditor``/``target`` may
    be a ``list[ModelOutput]`` or a mockllm ``custom_outputs`` callable.
    """
    session = Session()
    await session.start()
    sessions[sid] = session
    b = Branch(
        session,
        bid,
        seed=f"e2e-{sid}",
        auditor_model="mockllm/model",
        target_model="mockllm/model",
        max_turns=len(auditor) if isinstance(auditor, list) else max_turns,
        auditor_model_args={
            "custom_outputs": list(auditor) if isinstance(auditor, list) else auditor
        },
        target_model_args={
            "custom_outputs": list(target) if isinstance(target, list) else target
        },
    )
    session.branches[bid] = b
    session.current = bid
    task = asyncio.create_task(b.run())
    session.branch_tasks[bid] = task
    if play:
        b.play()
        # Callable custom_outputs (blocking mockllm) → don't await completion;
        # the caller manages release/cleanup. List → deterministic, run to end.
        if isinstance(auditor, list):
            await task
            assert b.error is None, f"branch {bid} failed: {b.error}"
    else:
        # Let run() flip idle→paused and broadcast before the page connects.
        for _ in range(100):
            if b.status == "paused":
                break
            await anyio.sleep(0.02)
    return session, b


def _pause_fix_landed() -> bool:
    try:
        out = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.lower()
    except Exception:
        return False
    return "interrupt" in out or "honest pause" in out


# ── cases ───────────────────────────────────────────────────────────────────


async def case_new_audit(page: Page, ui_port: int) -> None:
    sid = "e2e-na"
    session, _ = await _mk_branch(
        sid, "b0", auditor=AUDITOR_2, target=[_target("hi")], play=True
    )
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        # Populated session → sidebar shows the branch rail.
        await page.wait_for_selector(".side-new", timeout=15_000)
        await expect(page.locator(".side-branches .side-row")).to_have_count(
            1, timeout=15_000
        )
        before = _session_param(page.url)
        assert before == sid, f"expected ?session={sid}, got {before!r}"

        # `newAudit()` → `location.assign('?session=<rand>')` → full reload.
        await page.click(".side-new")
        await page.wait_for_url(
            lambda u: _session_param(u) not in (None, sid), timeout=10_000
        )
        after = _session_param(page.url)
        assert after and after != before, (
            f"?session did not change: before={before!r} after={after!r}"
        )
        # Fresh session: no current branch → StartView, and the Branches
        # section (guarded on `roots.length > 0`) does not mount.
        await page.wait_for_selector(".start-view", timeout=15_000)
        await expect(page.locator(".side-branches")).to_have_count(0)
        print(f"    ?session {before!r} → {after!r}; .side-branches gone")
    finally:
        await session.close()


async def case_unqueue(page: Page, ui_port: int) -> None:
    """Realistic unqueue scenario post-R4: branch is *running* with a
    blocking generate; inject via composer (no auto-step, since
    ``status=="running"``) → ghost queues; unqueue before ``pre_turn`` of
    the *next* turn drains it. Pre-R4 this used a paused branch, but
    ``sendFeedback`` now auto-steps when paused (gap #9), which would
    consume the message before the test can unqueue it.
    """
    sid = "e2e-uq"

    hold = anyio.Event()

    async def slow_aud(*_a: Any, **_k: Any) -> ModelOutput:
        await hold.wait()
        return AUDITOR_2[0]

    session, b = await _mk_branch(
        sid, "b0", auditor=slow_aud, target=[_target("hi")], play=True
    )
    try:
        # Wait until the auditor's first generate is in flight — status is
        # ``running`` and ``_gen_scope`` is set, so the composer's
        # ``sendFeedback`` won't auto-step and the inject genuinely queues.
        for _ in range(200):
            if b._gen_scope is not None:
                break
            await anyio.sleep(0.02)
        assert b._gen_scope is not None, "generate never started"

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".columns .col-wrap", timeout=15_000)
        aud = page.locator(".columns .col-wrap").first
        ghost = aud.locator(".bubble.ghost")
        await expect(ghost).to_have_count(0)

        await aud.locator(".composer-input").fill("please try a different tack")
        await aud.locator(".composer-input").press("Enter")
        await expect(ghost).to_have_count(1, timeout=5_000)
        for _ in range(100):
            if b.queued["auditor"]:
                break
            await anyio.sleep(0.02)
        assert len(b.queued["auditor"]) == 1, f"backend queued={b.queued['auditor']!r}"

        # Hover the queued bubble's wrap → × in `.queued-actions`.
        wrap = aud.locator(".lead-wrap:has(.bubble.ghost)")
        await wrap.hover()
        await wrap.locator(".queued-actions button[title='remove from queue']").click()
        await expect(ghost).to_have_count(0, timeout=5_000)
        for _ in range(100):
            if not b.queued["auditor"]:
                break
            await anyio.sleep(0.02)
        assert b.queued["auditor"] == [], (
            f"backend still queued after unqueue: {b.queued['auditor']!r}"
        )
        print("    ghost 0→1→0; backend queued drained")
    finally:
        hold.set()  # release the blocking generate
        b.pause()  # cancel _gen_scope so it doesn't wait on hold
        session.branch_tasks["b0"].cancel()
        await session.close()


async def case_redacted_reasoning(page: Page, ui_port: int) -> None:
    sid = "e2e-rr"
    session, _ = await _mk_branch(
        sid, "b0", auditor=AUDITOR_2, target=[_target_redacted()], play=True
    )
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        tgt = page.locator(".columns .col-wrap").last
        red = tgt.locator(".reasoning-redacted")
        await expect(red).to_have_count(1, timeout=15_000)
        text = (await red.text_content()) or ""
        assert "[extended reasoning — redacted by provider]" in text, (
            f"placeholder missing: {text!r}"
        )
        assert BLOB not in text, f"raw base64 blob leaked into UI: {text!r}"
        # And the blob doesn't leak anywhere else in the target column.
        col_text = (await tgt.text_content()) or ""
        assert BLOB not in col_text, "blob leaked outside .reasoning-redacted"
        print(f"    .reasoning-redacted = {text!r}")
    finally:
        await session.close()


async def case_pause_interrupts(page: Page, ui_port: int) -> None:
    sid = "e2e-pi"
    # Async mockllm callable: turn 0 announces entry then sleeps 30s — long
    # enough that a non-interrupting pause would fail the <2s budget by a
    # wide margin. mockllm ``await``s the coroutine, and ``anyio.sleep`` is
    # cancellable via ``_gen_scope``.
    entered = anyio.Event()

    async def slow_auditor(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        if k == 0:
            entered.set()
            await anyio.sleep(30)
        return copy.deepcopy(AUDITOR_2[min(k, 1)])

    session = Session()
    await session.start()
    sessions[sid] = session
    b = Branch(
        session,
        "b0",
        seed=f"e2e-{sid}",
        auditor_model="mockllm/model",
        target_model="mockllm/model",
        max_turns=4,
        auditor_model_args={"custom_outputs": slow_auditor},
        target_model_args={"custom_outputs": [_target("hi")]},
    )
    session.branches["b0"] = b
    session.current = "b0"
    b.play()
    task = asyncio.create_task(b.run())
    session.branch_tasks["b0"] = task
    try:
        # Block until the slow generate is actually in flight (so `.primary-
        # pause` has something to interrupt) before loading the page.
        with anyio.fail_after(5.0):
            await entered.wait()
        assert b._gen_scope is not None, "loop never exposed _gen_scope"

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        await page.wait_for_selector(".primary-pause", timeout=5_000)

        t0 = time.monotonic()
        await page.click(".primary-pause")
        # Backend: status flips + generate is cancelled well under 2s.
        for _ in range(200):
            if b.status == "paused" and b._gen_scope is None:
                break
            await anyio.sleep(0.01)
        dt_backend = time.monotonic() - t0
        assert b.status == "paused", (
            f"backend status={b.status!r} after {dt_backend:.2f}s (want 'paused')"
        )
        assert b._gen_scope is None, "_gen_scope not cleared after interrupt"
        assert dt_backend < 2.0, (
            f"pause took {dt_backend:.2f}s to interrupt — expected <2s "
            f"(30s sleep should have been cancelled, not awaited)"
        )
        assert not task.done(), "branch task exited — interrupt should park, not end"
        # UI: `.runline` reflects the broadcast status within the same budget.
        await page.wait_for_selector(
            ".runline.status-paused",
            timeout=int(max(100, (2.0 - dt_backend) * 1000)),
        )
        dt_ui = time.monotonic() - t0
        print(
            f"    interrupted in {dt_backend:.3f}s (backend) / "
            f"{dt_ui:.3f}s (UI); task alive, _gen_scope cleared"
        )
    finally:
        task.cancel()
        await session.close()


# ── driver ──────────────────────────────────────────────────────────────────


async def _amain() -> int:
    ws_port = _free_port()
    ui_port = _free_port()

    cases = [
        ("newAudit → fresh session", case_new_audit),
        ("unqueue ghost bubble", case_unqueue),
        ("redacted reasoning placeholder", case_redacted_reasoning),
    ]
    skipped: list[str] = []
    if _pause_fix_landed():
        cases.append(("pause interrupts mid-generate", case_pause_interrupts))
    else:
        skipped.append(
            "pause interrupts mid-generate (fix not landed — no "
            "'interrupt'/'honest pause' commit in git log -20)"
        )

    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    async with _backend(ws_port), _vite(ws_port, ui_port), async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "console",
            lambda m: (
                errors.append(f"console: {m.text}") if m.type == "error" else None
            ),
        )

        for name, fn in cases:
            print(f"\n── {name} ──")
            try:
                await fn(page, ui_port)
                results.append((name, "PASS", ""))
            except AssertionError as e:
                results.append((name, "FAIL", str(e)))
            except Exception as e:
                results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
                traceback.print_exc()

        await browser.close()

        if errors:
            print("\npage errors:")
            for e in errors:
                print(f"  ! {e}")

    print("\n" + "=" * 60)
    for name, status, detail in results:
        line = f"{status:5}  {name}"
        if detail:
            line += f"  — {detail}"
        print(line)
    for s in skipped:
        print(f"SKIP   {s}")
    n_pass = sum(1 for _, st, _ in results if st == "PASS")
    print(f"\n{n_pass}/{len(results)} passed, {len(skipped)} skipped")
    return 0 if n_pass == len(results) else 1


def main() -> None:
    sys.exit(anyio.run(_amain))


if __name__ == "__main__":
    main()
