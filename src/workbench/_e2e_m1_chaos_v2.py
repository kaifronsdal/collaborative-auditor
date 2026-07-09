"""Chaos playwright v2 — post-A1-A4 adversarial UI stress (s9-s13).

Same harness as ``_e2e_m1_chaos`` (in-process backend + vite dev on free
ports, headless chromium, per-scenario console/pageerror/backend-log
capture) but exercises the M1-orchestrator + session-wide-timeline paths
that A1-A4 touched:

  s9   rapid switchBranch across 3 branches — sidebar rows clicked
       b0→b1→b2→b0 in <100ms; assert `session.current` settles on b0,
       no `{t:"error"}`, all statuses correct, no orphan running task.
  s10  session-fork during running orchestrator — turn-1 settles, turn-2's
       generate is mid-3s-sleep; click turn-1's ``bi-signpost-split`` fork
       button. Assert `{t:"forked"}` navigates, parent closed, child
       registered + reachable.
  s11  orchestrator + M0 branch concurrently running — pause M0 → orch
       unaffected; pause orch → M0 unaffected. If the server rejects the
       coexistence, assert clean rejection instead.
  s12  click every gantt lane fast — base with an L1 rollback (2 lanes) +
       2 L2 forks → ≥4 target swimlane rows; click each in <200ms; assert
       `switchBranch` fires per foreign-lane click, no exception, selection
       settles.
  s13  approve-all during opening gates — one python cell opens 3
       ``wb.review_seeds`` gates staggered by 300ms; hover the header pill
       and click ``Approve all`` while the 3rd is registering. Assert all
       three resolve, no double-approve error.

Observation-only: a FAIL is a finding, not a test to fix here.

Run:  uv run python -m workbench._e2e_m1_chaos_v2
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
import traceback
from typing import Any

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo
from playwright.async_api import Page, async_playwright

from workbench._e2e_m1_chaos import (
    SLEEP_S,
    BackendLogCapture,
    Result,
    SlowModel,
    _wire_page_capture,
)
from workbench._smoke_fixtures import (
    SCRIPT3,
    _auditor_turn,
    _backend,
    _free_port,
    _nth_target_anchor,
    _tc,
    _vite,
    auditor_by_turn,
    make_base,
    run_child,
    target_by_last_user,
)
from workbench.m1._fixtures import tool_call, wait_for
from workbench.server import _dispatch, sessions
from workbench.session import Session

# ── slow orchestrator model (s10/s11/s13) ───────────────────────────────────


def _slow_orch(turns: list[ModelOutput], *, slow_from: int, entered: list[anyio.Event]):
    """mockllm ``custom_outputs``: ``turns[k]`` for k = #assistant in input;
    sleep ``SLEEP_S`` on turns ≥ ``slow_from``. Fires ``entered[k]`` on entry
    so the caller can synchronise with the mid-generate window."""

    async def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        while len(entered) <= k:
            entered.append(anyio.Event())
        entered[k].set()
        if k >= slow_from:
            await asyncio.sleep(SLEEP_S)
        return copy.deepcopy(turns[min(k, len(turns) - 1)])

    return _out


async def _mk_orch_session(
    sid: str,
    turns: list[ModelOutput],
    *,
    slow_from: int,
    play: bool = False,
) -> tuple[Session, Any, list[anyio.Event]]:
    """Fresh session with a mockllm-driven orchestrator on ``turns``.

    Registers under ``sessions[sid]`` with a ``session_id`` (so
    ``fork_orchestrator``'s eviction works) but NO ``store_dir`` —
    ``save_session`` no-ops, avoiding ``asdict(branch.meta)`` deep-copying
    the mockllm callable when a scenario also carries an M0 branch.
    Returns ``(session, orch, entered)`` where ``entered[k]`` fires when
    generate ``k`` is entered.
    """
    session = Session(sid, None)
    await session.start()
    sessions[sid] = session
    entered: list[anyio.Event] = []
    await session.start_orchestrator(
        model="mockllm/model",
        model_args={
            "custom_outputs": _slow_orch(turns, slow_from=slow_from, entered=entered)
        },
        max_turns=len(turns) + 4,
    )
    orch = session.orchestrator
    assert orch is not None
    if play:
        orch.play()
    return session, orch, entered


async def _teardown_orch(session: Session) -> None:
    """Close ``session`` and any fork it spawned; clear the kernel singleton.

    ``Session.close`` is not re-entrant (``_send.aclose()`` on an already-
    closed stream raises), so guard on ``_closed``. Force-clear
    ``OrchestratorKernel._instance`` so a stuck ``__exit__`` in one scenario
    doesn't cascade a ``singleton`` RuntimeError into the next.
    """
    from workbench import server as srv
    from workbench.m1.kernel import OrchestratorKernel

    for sid, s in list(srv.sessions.items()):
        if s is session or (s.orchestrator is not None and s is not session):
            if not s._closed.is_set():
                await s.close()
            srv.sessions.pop(sid, None)
    OrchestratorKernel._instance = None  # type: ignore[attr-defined]


# ── s9: rapid switchBranch across 3 branches ────────────────────────────────


async def s9_rapid_switch(page: Page, ui_port: int) -> str:
    sid = "chaos2-s9"
    session = Session()
    await session.start()
    sessions[sid] = session
    try:
        # Base + two L2 forks, each run to completion → 3 ended branches.
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
            branch_id="b0",
        )
        a1 = _nth_target_anchor(base.audit_tape.log, 0)
        a2 = _nth_target_anchor(base.audit_tape.log, 1)
        await _dispatch(session, {"t": "branch", "branch": "b0", "at": a1})
        _b1, _ = await run_child(session)
        await _dispatch(session, {"t": "branch", "branch": "b0", "at": a2})
        _b2, _ = await run_child(session)
        assert len(session.branches) == 3, f"branches={list(session.branches)}"
        # Repoint to b0 so the click sequence starts from a known state.
        await _dispatch(session, {"t": "switch", "branch": "b0"})

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        rows = page.locator(".side-branches .side-row")
        await rows.first.wait_for(state="visible", timeout=15_000)
        n_rows = await rows.count()
        assert n_rows == 3, f"sidebar rows={n_rows} (want 3)"

        # BranchNode renders depth-first (root then children in
        # ``branches`` dict-insertion order): b0, b1, b2. Click sequence
        # b0→b1→b2→b0 with <100ms between.
        seq = [0, 1, 2, 0]
        for i in seq:
            await rows.nth(i).click(force=True)
            await anyio.sleep(0.06)

        await anyio.sleep(1.5)
        # ``switch`` is LOCKED — 4 clicks serialise; last one wins.
        assert session.current == "b0", (
            f"session.current={session.current!r} (want 'b0'); "
            f"branches={list(session.branches)}"
        )
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        # No orphan tasks; every branch's status is a definite value.
        orphans = [
            bid
            for bid, t in session.branch_tasks.items()
            if not t.done() and bid not in session.branches
        ]
        assert not orphans, f"orphan tasks: {orphans}"
        running = [
            bid for bid, br in session.branches.items() if br.status == "running"
        ]
        assert not running, f"branch left running after switch storm: {running}"
        statuses = {bid: br.status for bid, br in session.branches.items()}
        assert all(s in ("ended", "paused") for s in statuses.values()), statuses
        # UI settled: b0's row is `.active`.
        active = await page.locator(".side-branches .side-row.active").count()
        assert active == 1, f"{active} active rows (want 1)"
        return (
            f"current={session.current!r} rows={n_rows} statuses={statuses} "
            f"orphans={len(orphans)}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()
        sessions.pop(sid, None)


# ── s10: session-fork during running orchestrator ──────────────────────────


async def s10_session_fork_mid_turn(page: Page, ui_port: int) -> str:
    sid = "chaos2-s10"
    orig_cwd = os.getcwd()
    # Turn 1 fast (prose + trivial python), turn 2+ slow → fork window.
    turns = [
        tool_call("Loading.", [("python", {"code": "1 + 1"})]),
        tool_call("Thinking.", [("python", {"code": "2 + 2"})]),
        tool_call("done.", []),
    ]
    session, orch, entered = await _mk_orch_session(sid, turns, slow_from=1, play=True)
    try:
        # Wait until turn-2's generate is mid-sleep (turn-1 is settled →
        # its `.asst-prose` is `!pending` so the fork button mounts).
        with anyio.fail_after(15.0):
            while len(entered) < 2 or not entered[1].is_set():
                await anyio.sleep(0.05)
        assert orch.status == "running", f"orch.status={orch.status!r}"

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        # Auto-accept the ``confirm()`` the fork button opens.
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        await page.wait_for_selector(
            ".orch-col .turn[data-turn='1'] .asst-prose", timeout=15_000
        )
        prose = page.locator(".orch-col .turn[data-turn='1'] .ba-host").first
        await prose.hover()
        fork_btn = prose.locator(".block-actions button:has(i.bi-signpost-split)")
        await fork_btn.wait_for(state="visible", timeout=5_000)

        await fork_btn.click(force=True)

        # `{t:"forked"}` → `location.assign(?session=<new>)` → reload.
        await page.wait_for_url(
            lambda u: f"session={sid}" not in u and "session=" in u,
            timeout=15_000,
        )
        from urllib.parse import parse_qs, urlparse

        new_sid = parse_qs(urlparse(page.url).query)["session"][0]
        assert new_sid != sid, f"?session did not change: {page.url}"
        assert new_sid in sessions, (
            f"forked session {new_sid!r} not registered in server.sessions"
        )
        # Parent: closed + evicted; task done; kernel released.
        assert sid not in sessions, f"parent {sid!r} still in server.sessions"
        assert session._closed.is_set(), "parent session not closed"
        assert orch.task is not None and orch.task.done(), (
            "parent orch task still running"
        )
        # Child reachable: page reloads onto it; orch column mounts.
        await page.wait_for_selector(".orch-col-wrap", timeout=15_000)
        child = sessions[new_sid]
        assert child.orchestrator is not None, "child has no orchestrator"
        # No error banner on either end of the navigation.
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        return (
            f"parent_closed={session._closed.is_set()} "
            f"new_sid={new_sid[:8]}… child_orch_status={child.orchestrator.status!r}"
        )
    finally:
        await _teardown_orch(session)
        os.chdir(orig_cwd)


# ── s11: orchestrator + M0 branch running simultaneously ────────────────────


async def s11_orch_and_m0_concurrent(page: Page, ui_port: int) -> str:
    sid = "chaos2-s11"
    orig_cwd = os.getcwd()
    # Orchestrator: every turn slow so it's ``running`` for the whole test.
    turns = [
        tool_call("Working.", [("python", {"code": "None"})]),
        tool_call("Still working.", [("python", {"code": "None"})]),
        tool_call("done.", []),
    ]
    session, orch, entered = await _mk_orch_session(sid, turns, slow_from=0, play=True)
    try:
        with anyio.fail_after(10.0):
            while not (entered and entered[0].is_set()):
                await anyio.sleep(0.05)
        # M0 branch on the SAME session (mimics ``{t:"start"}`` without the
        # WS round-trip so ``custom_outputs`` reaches the branch).
        aud = SlowModel(script=[*SCRIPT3, SCRIPT3[-1]], slow_from=0)
        tgt = SlowModel(table={"u1": "r1", "u2": "r2"}, slow_from=99)
        from workbench.run import Branch

        b = Branch(
            session,
            "m0",
            seed=f"chaos-{sid}",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=4,
            auditor_model_args={"custom_outputs": aud},
            target_model_args={"custom_outputs": tgt},
        )
        session.branches["m0"] = b
        session.current = "m0"
        b.play()
        session.branch_tasks["m0"] = asyncio.create_task(b.run())
        with anyio.fail_after(10.0):
            await aud.entered_at(0).wait()
        assert b.status == "running" and orch.status == "running", (
            f"pre: m0={b.status!r} orch={orch.status!r}"
        )

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        await page.wait_for_selector(".orch-col-wrap", timeout=15_000)
        # Both columns present: M0 auditor composer + orch composer.
        m0_primary = page.locator(".columns > .col-wrap").first.locator(
            ".composer-lower .primary"
        )
        # Orch play/pause is the ``composer-controls`` button, not ``.primary``.
        orch_toggle = page.locator(
            ".orch-col-wrap .composer-controls button[title*='pause']"
        )
        await m0_primary.wait_for(state="visible", timeout=10_000)
        await orch_toggle.wait_for(state="visible", timeout=10_000)

        # Pause M0 → orch unaffected.
        await m0_primary.click(force=True)
        await anyio.sleep(1.0)
        assert b.status == "paused", f"m0 status={b.status!r} after M0-pause"
        assert orch.status == "running", (
            f"orch status={orch.status!r} after M0-pause (want 'running')"
        )
        assert b.error is None, f"m0 errored: {b.error}"

        # Pause orch → M0 unaffected. ``{t:"pause", target:"orch"}`` → orch
        # ``pause()`` clears the gate; the in-flight generate finishes then
        # the loop parks (StepGated semantics — no mid-generate cancel).
        await orch_toggle.click(force=True)
        with anyio.move_on_after(SLEEP_S + 2.0):
            while orch.status == "running":
                await anyio.sleep(0.05)
        assert orch.status in ("paused", "waiting"), (
            f"orch status={orch.status!r} after orch-pause"
        )
        assert b.status == "paused", (
            f"m0 status={b.status!r} after orch-pause (want unchanged 'paused')"
        )
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        return (
            f"coexist=yes m0={b.status!r} orch={orch.status!r} branch_error={b.error!r}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await _teardown_orch(session)
        os.chdir(orig_cwd)


# ── s12: click every gantt lane fast ────────────────────────────────────────

# Base with two L1 rollbacks (auditor rolls back to M3 == after r1 twice →
# 3 target trajectories in one L2 branch → wrapper row + 3 L1 lanes = 4
# gantt rows), then 2 L2 forks off the base. Whether the A1-b-wide session-
# wide target timeline grafts the L2 forks as extra lanes is *observed*,
# not assumed.
S12_AUDITOR = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u3"),
        _tc("resume"),
    ),
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u4"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]


async def s12_gantt_lane_storm(page: Page, ui_port: int) -> str:
    sid = "chaos2-s12"
    session = Session()
    await session.start()
    sessions[sid] = session
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(S12_AUDITOR),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u3": "r3", "u4": "r4"}
            ),
            max_turns=len(S12_AUDITOR),
            branch_id="b0",
        )
        # Two L2 forks at r1 / r2 (inclusive branch → replay to end). Whether
        # these surface as extra target-gantt lanes under A1-b-wide is part
        # of the observation (first run showed they do not — replayed anchors
        # graft onto the parent's lane).
        for n in (0, 1):
            anchor = _nth_target_anchor(base.audit_tape.log, n)
            await _dispatch(session, {"t": "branch", "branch": "b0", "at": anchor})
            await run_child(session)
        assert len(session.branches) == 3, f"branches={list(session.branches)}"
        await _dispatch(session, {"t": "switch", "branch": "b0"})

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        aud_col = page.locator(".columns > .col-wrap").first
        target_col = page.locator(".columns > .col-wrap").nth(1)
        gantt = target_col.locator(".lane-gantt-host")
        await gantt.wait_for(state="visible", timeout=15_000)
        lanes = gantt.locator('[role="row"]')
        # Poll for the timeline delta to settle.
        for _ in range(50):
            if await lanes.count() >= 4:
                break
            await anyio.sleep(0.1)
        n_lanes = await lanes.count()
        n_aud_lanes = await aud_col.locator('.lane-gantt-host [role="row"]').count()
        # ≥2 is the hard floor (rollback ⇒ multi-lane); <4 is reported, not a
        # harness failure — the click-storm still exercises whatever's there.
        assert n_lanes >= 2, f"only {n_lanes} gantt rows (need ≥2 for a storm)"
        note = "" if n_lanes >= 4 else f" [obs: {n_lanes} lanes for 3 branches]"

        current_before = session.current
        # Click every lane's label cell (row 0 is the b-wide wrapper — its
        # `selectLane` still fires; observe whether it errors). <200ms apart.
        for i in range(n_lanes):
            await lanes.nth(i).locator("> div").first.click(force=True)
            await anyio.sleep(0.1)

        await anyio.sleep(1.5)
        # No exception surfaced; ``session.current`` settled to *some*
        # branch (target lanes owned by foreign branches trigger
        # ``switchBranch``; b0's own lanes leave ``current`` alone).
        assert session.current in session.branches, (
            f"current={session.current!r} not a known branch"
        )
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        # ``defaultKey`` follows ``branch`` → the column's selected lane
        # belongs to ``session.current`` (or its ``l1_spans``). Just assert
        # the column still renders content (no blank / crashed swimlane).
        content = await target_col.locator(".model-event-row").count()
        assert content > 0, "target column empty after lane storm"
        return (
            f"tgt_lanes={n_lanes} aud_lanes={n_aud_lanes} clicked={n_lanes} "
            f"current {current_before!r}→{session.current!r} "
            f"rows_shown={content}{note}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()
        sessions.pop(sid, None)


# ── s13: approve-all during opening gates ───────────────────────────────────


async def s13_approve_all_race(page: Page, ui_port: int) -> str:
    sid = "chaos2-s13"
    orig_cwd = os.getcwd()
    # One turn: 3 concurrent ``wb.review_seeds`` gates, staggered so gate 3
    # opens ~600ms after gate 1 — the click lands in that window.
    cell = (
        "async def _g(i, d):\n"
        "    await asyncio.sleep(d)\n"
        "    return await wb.review_seeds([f's{i}'], f'batch {i}', {})\n"
        "await asyncio.gather(_g(0, 0.0), _g(1, 0.3), _g(2, 0.6))"
    )
    turns = [
        tool_call("Proposing three batches.", [("python", {"code": cell})]),
        tool_call("done.", []),
    ]
    session, orch, _ = await _mk_orch_session(sid, turns, slow_from=99)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".orch-col-wrap", timeout=15_000)
        orch.step()

        # Wait for gate 1+2 to open (≥2 pending → ``Approve all`` mounts on
        # hover). Gate 3 (``d=0.6``) is still ``asyncio.sleep``'ing.
        await wait_for(lambda: len(orch.gate.pending) >= 2, timeout=5.0)

        pill = page.locator(".orch-head .head-gate-wrap")
        await pill.wait_for(state="visible", timeout=10_000)
        await pill.hover()
        approve_all = page.locator(".head-gate-pop .hgp-all")
        await approve_all.wait_for(state="visible", timeout=5_000)
        # STRESS-V2 P1 s13: the fix's guarantee is "server-side snapshot at
        # resolve time" — wait until gate 3 is pending *on the server* (the
        # client's button may still read "(2)" if the broadcast hasn't
        # rendered), then click. Pre-fix this still fails: the client
        # iterates its own ``approvable`` and sends ≤2 ``{t:"approve"}``.
        await wait_for(lambda: len(orch.gate.pending) == 3, timeout=5.0)
        n_at_click = len(orch.gate.pending)
        await approve_all.click(force=True)

        # Let gate 3 open + observe. If it wasn't caught by the click, it's
        # still pending → resolve it now so the cell settles (observation,
        # not a fix).
        await anyio.sleep(1.5)
        leftover = list(orch.gate.pending)
        for gid in leftover:
            orch.gate.resolve(gid, {})
        with anyio.move_on_after(5.0):
            while orch.gate.pending:
                await anyio.sleep(0.05)

        # All 3 gates ultimately resolved; the cell's ``asyncio.gather``
        # returns → tool result lands (no hung ``waiting`` status).
        await wait_for(lambda: orch.status != "waiting", timeout=5.0)
        # No `{t:"error"}` from a double-approve (server-side is idempotent;
        # a False from ``gate.resolve`` only logs a WARNING — captured below).
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        # The observation the scenario is after: did approve-all catch all
        # three, or did gate 3 slip past?
        assert not leftover, (
            f"approve-all missed {len(leftover)} gate(s) opening during the "
            f"click ({n_at_click} pending at click time; leftover ids="
            f"{[g[:6] for g in leftover]})"
        )
        return (
            f"pending_at_click={n_at_click} leftover={len(leftover)} "
            f"final_status={orch.status!r}"
        )
    finally:
        await _teardown_orch(session)
        os.chdir(orig_cwd)


# ── driver ──────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("s9 rapid switchBranch ×3 branches", s9_rapid_switch),
    ("s10 session-fork mid-orch-turn", s10_session_fork_mid_turn),
    ("s11 orch + M0 concurrent pause independence", s11_orch_and_m0_concurrent),
    ("s12 gantt lane click storm", s12_gantt_lane_storm),
    ("s13 approve-all during opening gates", s13_approve_all_race),
]


async def _amain() -> int:
    ws_port = _free_port()
    ui_port = _free_port()

    log_cap = BackendLogCapture()
    logging.getLogger().addHandler(log_cap)
    logging.getLogger().setLevel(logging.WARNING)

    orig_cwd = os.getcwd()
    results: list[Result] = []

    async with _backend(ws_port), _vite(ws_port, ui_port), async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for name, fn in SCENARIOS:
                print(f"\n── {name} ──")
                page = await browser.new_page(viewport={"width": 1800, "height": 1000})
                cap = _wire_page_capture(page)
                log_cap.drain()
                try:
                    detail = await fn(page, ui_port)
                    status = "PASS"
                except AssertionError as e:
                    status, detail = "FAIL", str(e) or repr(e)
                except Exception as e:
                    status, detail = "ERROR", f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                finally:
                    # ``Orchestrator.__init__`` chdirs into ``session_dir``;
                    # a scenario that raised before its own ``finally`` ran
                    # would leave subsequent ``_backend`` teardown / vite
                    # subprocess pathing on a deleted cwd.
                    os.chdir(orig_cwd)
                results.append(
                    Result(
                        name=name,
                        status=status,
                        detail=detail,
                        console=list(cap.console),
                        page_errors=list(cap.page_errors),
                        backend_log=log_cap.drain(),
                    )
                )
                await page.close()
        finally:
            await browser.close()

    print("\n" + "=" * 72)
    for r in results:
        print(f"\n{r.status:5}  {r.name}")
        print(f"       {r.detail}")
        if r.page_errors:
            print("       page errors:")
            for e in r.page_errors:
                print(f"         ! {e}")
        if r.console:
            print("       console:")
            for c in r.console:
                print(f"         · {c}")
        if r.backend_log:
            print("       backend log (≥WARNING):")
            for line in r.backend_log:
                for sub in line.splitlines():
                    print(f"         │ {sub}")
    n_pass = sum(1 for r in results if r.status == "PASS")
    print(f"\n{n_pass}/{len(results)} passed")
    return 0 if n_pass == len(results) else 1


def main() -> None:
    sys.exit(anyio.run(_amain))


if __name__ == "__main__":
    main()
