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
4. **pause interrupts** — SKIPPED unless a commit mentioning "interrupt" or
   "honest pause" has landed (checked at import time via ``git log``).

Run:  uv run python -m workbench._e2e_m1_ui_fixes
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import traceback
from urllib.parse import parse_qs, urlparse

import anyio
from inspect_ai.model import (
    ChatMessageAssistant,
    ContentReasoning,
    ContentText,
    ModelOutput,
)
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
    auditor: list[ModelOutput],
    target: list[ModelOutput],
    play: bool,
) -> tuple[Session, Branch]:
    """Register a mockllm branch on a fresh session under ``sessions[sid]``.

    ``play=True`` runs it to completion before returning; ``play=False``
    spawns the ``run()`` task but leaves the gate closed so the branch parks
    at ``status='paused'`` with an empty column.
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
        max_turns=len(auditor),
        auditor_model_args={"custom_outputs": list(auditor)},
        target_model_args={"custom_outputs": list(target)},
    )
    session.branches[bid] = b
    session.current = bid
    task = asyncio.create_task(b.run())
    session.branch_tasks[bid] = task
    if play:
        b.play()
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
    sid = "e2e-uq"
    session, b = await _mk_branch(
        sid, "b0", auditor=AUDITOR_2, target=[_target("hi")], play=False
    )
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".columns .col-wrap", timeout=15_000)
        aud = page.locator(".columns .col-wrap").first
        ghost = aud.locator(".bubble.ghost")
        await expect(ghost).to_have_count(0)

        # Composer → Enter injects into queued[auditor]; branch is paused so
        # the message stays queued (ghost bubble persists).
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
        # Branch is parked on the gate — cancel its run() task.
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
        # Placeholder — wire up once the pause-interrupt commit lands.
        skipped.append("pause interrupts (fix landed but case not wired)")
    else:
        skipped.append(
            "pause interrupts (fix not landed — no 'interrupt'/'honest pause' "
            "commit in git log -20)"
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
