"""Deterministic UI e2e: scripted mockllm auditor + target, rollback mid-run,
then Playwright asserts on what's actually rendered.

The auditor script (no model nondeterminism):
  T1  set_system_message · send_message("u1") · resume    → target replies "r1"
  T2  send_message("u2") · resume                         → target replies "r2"
  T3  send_message("u3") · resume                         → target replies "r3"
  T4  rollback_conversation(M3) · send_message("u4") · resume   ← rollback to after r1
                                                          → target replies "r4"
  T5  send_message("u5") · resume                         → target replies "r5"
  T6  end_conversation

Expected target trajectory tree:
  branch 1: r1, r2, r3
    └ branch 2 (forked at r1): r4, r5

UI assertions (Playwright against the real frontend):
  - `.lane-rail` is present with exactly 2 `.lane-row` (branch 1, branch 2)
  - branch 2 row has `.branch` class and is selected by default (latest)
  - selected lane shows the spliced lineage: r1 (prefix) then r4, r5
  - clicking branch 1 shows r1, r2, r3
  - auditor column shows the `rollback_conversation` tool call

Run:  uv run python -m workbench._smoke_ui_rollback
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path

import anyio
import uvicorn
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolCall
from playwright.async_api import async_playwright, expect

from workbench.run import Branch
from workbench.server import app, sessions
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]

# ── scripted model outputs ──────────────────────────────────────────────────

_n = 0


def _tc(function: str, **arguments) -> ToolCall:
    global _n
    _n += 1
    return ToolCall(id=f"c{_n}", function=function, type="function", arguments=arguments)


def _auditor_turn(*calls: ToolCall) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content="")
    out.choices[0].message.tool_calls = list(calls)
    return out


def _target(content: str) -> ModelOutput:
    return ModelOutput.from_content(model="mockllm", content=content)


AUDITOR_SCRIPT = [
    _auditor_turn(
        _tc("set_system_message", system_message="be helpful"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="u3"), _tc("resume")),
    # rollback to M3 (= first target reply r1); then continue on the new trajectory
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u4"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u5"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]

TARGET_SCRIPT = [_target(f"r{i}") for i in range(1, 6)]


# ── infra ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _backend(port: int):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(50):
            if srv.started:
                break
            await anyio.sleep(0.1)
        assert srv.started, "uvicorn failed to start"
        yield
    finally:
        srv.should_exit = True
        await task


@asynccontextmanager
async def _vite(ws_port: int, ui_port: int):
    """Start the Vite dev server pointing its WS at our in-process backend."""
    env = {**os.environ, "VITE_WS_URL": f"ws://127.0.0.1:{ws_port}"}
    proc = subprocess.Popen(  # noqa: S603
        [
            "npx", "--yes", "pnpm@10.29.3", "dev",
            "--host", "127.0.0.1", "--port", str(ui_port), "--strictPort",
        ],
        cwd=REPO / "frontend-wb",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                with closing(socket.create_connection(("127.0.0.1", ui_port), 0.2)):
                    break
            except OSError:
                await anyio.sleep(0.2)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"vite failed to start on :{ui_port}\n{out}")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── the test ────────────────────────────────────────────────────────────────


async def _amain() -> None:
    ws_port = _free_port()
    ui_port = _free_port()
    sid = "ui-rollback"

    async with _backend(ws_port), _vite(ws_port, ui_port):
        # Register the session and the scripted branch directly (no WS `start`
        # — `custom_outputs` can't cross the wire). The frontend connects to
        # this session_id and receives the full state.
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "rb",
            seed="scripted rollback",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=len(AUDITOR_SCRIPT),
            auditor_model_args={"custom_outputs": list(AUDITOR_SCRIPT)},
            target_model_args={"custom_outputs": list(TARGET_SCRIPT)},
        )
        session.branches["rb"] = b
        session.current = "rb"
        b.play()
        task = asyncio.create_task(b.run())
        session.branch_tasks.append(task)
        await task

        # Backend-side sanity: 2 trajectories, 5 target ModelEvents.
        from workbench._smoke_util import resolve_role  # noqa: PLC0415

        n_traj = sum(1 for _ in _walk(b.history.root))
        assert n_traj == 2, f"expected 2 target trajectories, got {n_traj}"
        target_models = [
            e for e in session.events.values()
            if e["event"] == "model"
            and resolve_role(e.get("span_id"), session) == ("rb", "target")
        ]
        assert len(target_models) == 5, f"expected 5 target ModelEvents, got {len(target_models)}"
        print(f"backend ✓ trajectories={n_traj} target_model_events={len(target_models)}")

        # ── Playwright: drive the real frontend ──────────────────────────────
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")

            # Both columns now render via `SwimlaneColumn`; select by layout
            # slot (first/last `.col-wrap`) so the assertions are role-stable.
            auditor_col = page.locator(".columns .col-wrap").first
            target_col = page.locator(".columns .col-wrap").last

            # The frontend should connect to /ws/{sid}, receive the completed
            # branch's full state (incl. timelines), and render inspect-view's
            # gantt swimlane (TimelineSwimLanes — CSS-module classes, so we
            # select via role/host instead of class names).
            gantt = target_col.locator(".lane-gantt-host")
            await expect(gantt).to_be_visible(timeout=15_000)
            lanes = gantt.locator('[role="row"]')
            await expect(lanes).to_have_count(2)
            print("UI ✓ target .lane-gantt-host visible with 2 swimlane rows")

            # Default selection = latest (branch 2); shows spliced lineage:
            # r1 (prefix) then r4, r5.
            target_bubbles = target_col.locator(".bubble.assistant")
            texts = [t.strip() for t in await target_bubbles.all_text_contents()]
            assert "r1" in texts and "r4" in texts and "r5" in texts, (
                f"branch-2 lineage missing expected replies; got {texts}"
            )
            assert "r2" not in texts and "r3" not in texts, (
                f"branch-2 lineage should not include rolled-back r2/r3; got {texts}"
            )
            print(f"UI ✓ branch-2 lineage shows {texts}")

            # Click branch 1 row label → r1, r2, r3 (no r4/r5).
            await lanes.first.locator("> div").first.click()
            texts1 = [
                t.strip()
                for t in await target_col.locator(".bubble.assistant").all_text_contents()
            ]
            assert texts1[:3] == ["r1", "r2", "r3"], (
                f"branch-1 should show r1/r2/r3, got {texts1}"
            )
            assert "r4" not in texts1 and "r5" not in texts1, (
                f"branch-1 should not include post-rollback r4/r5; got {texts1}"
            )
            print(f"UI ✓ branch-1 shows {texts1}")

            # Auditor column renders tool calls as .tool-pair cards (Proposal 2),
            # one per call, nested under their model turn — including the rollback.
            tool_fns = await auditor_col.locator(".tool-pair .tp-fn").all_text_contents()
            assert "rollback_conversation" in tool_fns, (
                f"auditor .tool-pair cards missing rollback_conversation; got {tool_fns}"
            )
            # Exactly one card per scripted call (3+2+2 send/resume + 1 set_sys
            # + 1 rollback + 1 end = 13 across 6 turns).
            assert len(tool_fns) == 13, f"expected 13 tool-pair cards, got {len(tool_fns)}: {tool_fns}"
            # No detached TOOL bylines anymore (results live inside the pair).
            tool_bylines = await auditor_col.locator(".bubble-by").all_text_contents()
            assert "tool" not in [b.strip().lower() for b in tool_bylines], (
                f"detached TOOL bylines still present: {tool_bylines}"
            )
            print(
                f"UI ✓ auditor column has {len(tool_fns)} .tool-pair cards "
                f"incl. rollback_conversation; no detached tool bylines"
            )

            # ── inline branch-point chip (‹ idx/total ›) ────────────────────
            # Currently viewing branch-1 (clicked above). r1 is the fork point
            # → exactly one .branch-nav in the target column, reading "1/2".
            chips = target_col.locator(".branch-nav")
            await expect(chips).to_have_count(1)
            pos = chips.first.locator(".branch-nav-pos")
            await expect(pos).to_have_text("1/2")
            # No workbench-branch siblings exist, so the auditor column has none.
            await expect(auditor_col.locator(".branch-nav")).to_have_count(0)
            print("UI ✓ .branch-nav chip at r1 shows 1/2 on branch-1")

            # Click › → switches selected lane to branch-2; lineage flips to
            # r1, r4, r5 and the chip updates to "2/2".
            await chips.first.get_by_role("button", name="Next branch").click()
            texts2 = [
                t.strip()
                for t in await target_col.locator(".bubble.assistant").all_text_contents()
            ]
            assert "r4" in texts2 and "r5" in texts2 and "r2" not in texts2, (
                f"after › expected branch-2 lineage (r1/r4/r5), got {texts2}"
            )
            await expect(
                target_col.locator(".branch-nav .branch-nav-pos")
            ).to_have_text("2/2")
            print(f"UI ✓ › switched to branch-2: {texts2}; chip now 2/2")

            assert not errors, f"page errors: {errors}"
            await browser.close()

        await session.close()

    print("✓ all UI rollback assertions passed")


def _walk(t):
    yield t
    for c in t.children:
        yield from _walk(c)


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    sys.exit(main())
