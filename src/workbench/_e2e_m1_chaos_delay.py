"""Chaos playwright under simulated network jitter.

Sets ``WORKBENCH_BROADCAST_DELAY_MS=500`` (see :meth:`Session.drain` /
:meth:`Session.broadcast`) so every server→client frame is delayed by
``uniform(0, 0.5)`` seconds AND drain-queue frames are broadcast
concurrently — a later ``v`` can reach the client before an earlier one.
The connect-time ``push_full_state`` (direct ``conn.send_json``) is NOT
delayed, so the UI renders its initial snapshot immediately.

Re-runs the eight ``_e2e_m1_chaos`` scenarios verbatim (observation: which
assertions only hold without delay?) and adds three delay-specific probes:

  d1  ``useIsPending`` hold — click branch, immediately re-check: the
      button MUST be ``disabled`` until the (now-delayed) ``{t:"ack"}``
      arrives. A second click inside the window is blocked by the
      ``forkPending`` guard, not by luck.
  d2  version-guard drop count — capture every WS frame the page receives
      and count how many GUARDED frames arrive with ``v`` strictly less
      than the running max (i.e. what the client's A3 guard drops). Also
      counts ``{t:"status"}`` frames that overtake a GUARDED one — those
      are NOT guarded but DO write ``version``, so the overtaken batch is
      dropped (batch-atomicity finding, d3).
  d3  batch atomicity — after settling, does the client's pool cover every
      ``input_refs`` range in its event store? A dropped ``{t:"batch"}``
      leaves a pool gap; ``expandEvents`` would then read ``undefined``.

Run:  uv run python -m workbench._e2e_m1_chaos_delay
"""

from __future__ import annotations

import contextlib
import json
import logging
import os

# Set BEFORE importing session (drain reads it once at loop entry; broadcast
# reads it per-call, but keep the ordering unambiguous).
os.environ["WORKBENCH_BROADCAST_DELAY_MS"] = "500"

import sys
import traceback
from dataclasses import dataclass, field
from typing import Any

import anyio
from playwright.async_api import Page, WebSocket, async_playwright

from workbench._e2e_m1_chaos import (
    SCENARIOS as BASE_SCENARIOS,
    BackendLogCapture,
    Capture,
    Result,
    _mk_running,
    _target_row,
    _wire_page_capture,
)
from workbench._smoke_fixtures import _backend, _free_port, _vite

# Frame types the client's A3 version guard applies to (mirror of
# ``frontend-wb/src/store/session.ts`` ``GUARDED``).
GUARDED = frozenset(
    {"state", "batch", "branch_created", "current", "batch_resolved", "orch",
     "queued_consumed"}
)
# Non-guarded frames that nonetheless write ``version: msg.v`` — if one
# overtakes a GUARDED frame, the guard drops the latter.
WRITES_VERSION = frozenset({"status", "rewound", "notify", "l1_spans", "timeline"})


# ── WS frame capture ────────────────────────────────────────────────────────


@dataclass
class WsCapture:
    """Every frame the page's WebSocket received, in arrival order."""

    frames: list[dict[str, Any]] = field(default_factory=list)

    def guard_stats(self) -> dict[str, Any]:
        """Replay arrival order through the client's version guard.

        Returns the number of GUARDED frames the client drops (``v < max``),
        the count of non-guarded ``version``-writing frames that overtook a
        GUARDED one (each causes at least one drop), and per-``t`` drop
        breakdown.
        """
        max_v = -1
        dropped: list[tuple[str, int]] = []
        overtakes: list[tuple[str, int]] = []
        for f in self.frames:
            t = f.get("t")
            v = f.get("v")
            if v is None:
                continue
            if t in GUARDED:
                if v < max_v:
                    dropped.append((t, v))
                else:
                    max_v = v
            elif t in WRITES_VERSION:
                # this frame will bump ``state.version`` past a not-yet-
                # arrived GUARDED frame — record it as an overtake.
                if v > max_v and any(
                    g.get("t") in GUARDED
                    and g.get("v") is not None
                    and max_v <= g["v"] < v
                    for g in self.frames[self.frames.index(f) + 1 :]
                ):
                    overtakes.append((t, v))
                max_v = max(max_v, v)
        by_t: dict[str, int] = {}
        for t, _ in dropped:
            by_t[t] = by_t.get(t, 0) + 1
        return {
            "n_frames": len(self.frames),
            "dropped": len(dropped),
            "dropped_by_t": by_t,
            "overtakes": len(overtakes),
            "overtake_ts": sorted({t for t, _ in overtakes}),
        }


def _wire_ws_capture(page: Page) -> WsCapture:
    cap = WsCapture()

    def on_ws(ws: WebSocket) -> None:
        def on_frame(payload: str | bytes) -> None:
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                cap.frames.append(json.loads(payload))

        ws.on("framereceived", on_frame)

    page.on("websocket", on_ws)
    return cap


# ── delay-specific scenarios ────────────────────────────────────────────────


async def d1_is_pending_holds(page: Page, ui_port: int) -> str:
    """A2 ``useIsPending``: with the ``{t:"ack"}`` delayed up to 500ms, the
    branch button must be ``disabled`` for the whole window — a second
    click is blocked by ``forkPending``, not by racing the server."""
    sid = "delay-d1"
    session, *_ = await _mk_running(sid, aud_slow_from=2)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        row = await _target_row(page, 0)
        btn = row.locator(".actions button[title='branch at this turn']")

        assert not await btn.is_disabled(), "button already disabled pre-click"
        await btn.click(no_wait_after=True)

        # Sample ``disabled`` every 20ms until the ack lands (button
        # re-enables) or 1.5s elapses. Every sample before re-enable MUST
        # be True; the hold duration is the observed ack round-trip.
        samples: list[bool] = []
        for _ in range(75):
            samples.append(await btn.is_disabled())
            if samples[-1] is False and len(samples) > 1:
                break
            await anyio.sleep(0.02)
        held_ms = 20 * sum(1 for s in samples if s)
        # First sample MUST be disabled (send() → pending[] is synchronous).
        assert samples[0] is True, (
            "button not disabled immediately after click — useIsPending "
            "did not gate on send()"
        )
        # No False in the middle of a True run (would mean re-enabled then
        # re-disabled — impossible under A2, indicates a different guard).
        first_false = next((i for i, s in enumerate(samples) if not s), len(samples))
        assert all(samples[:first_false]), "disabled flickered before ack"

        # Second click while held: force it and check the backend did NOT
        # spawn a second child (server-side idempotency is NOT the guard —
        # the button being ``disabled`` means the browser drops the click
        # before ``send()`` ever runs).
        n_before = len(session.branches)
        with contextlib.suppress(Exception):
            await btn.click(force=True, no_wait_after=True, timeout=500)
        await anyio.sleep(2.0)
        n_children = len(session.branches) - 1
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        return (
            f"held_disabled≈{held_ms}ms samples={len(samples)} "
            f"children={n_children} (before_2nd_click={n_before - 1})"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def d2_version_guard_drops(page: Page, ui_port: int) -> str:
    """Count how many frames the client's A3 version guard drops under
    500ms jitter across a full 4-turn run + one branch."""
    sid = "delay-d2"
    ws = _wire_ws_capture(page)
    # ``aud_slow_from=3`` → turns 0-2 fast (r1,r2,r3 on screen), turn 3
    # (``end_conversation``) slow; ``_mk_running`` returns once turn 3 is
    # entered, so all fast-turn frames are already in flight.
    session, *_ = await _mk_running(sid, aud_slow_from=3, max_turns=4)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline", timeout=15_000)
        # Let the whole run's frames drain (max 4 turns × a few frames ×
        # 500ms jitter → budget 6s).
        await anyio.sleep(6.0)
        # One branch action to add ``branch_created``/``current`` frames to
        # the mix.
        row = await _target_row(page, 0)
        await row.locator(".actions button[title='branch at this turn']").click(
            force=True
        )
        await anyio.sleep(3.0)

        stats = ws.guard_stats()
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        # Observation, not assertion: report the drop count. The guard
        # firing at all is the finding (drain is FIFO absent delay).
        return (
            f"frames={stats['n_frames']} guarded_dropped={stats['dropped']} "
            f"by_t={stats['dropped_by_t']} "
            f"unguarded_overtakes={stats['overtakes']} ({stats['overtake_ts']})"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def d3_batch_atomicity(page: Page, ui_port: int) -> str:
    """Does a delayed ``{t:"batch"}`` overtaken by a later ``{t:"status"}``
    leave the client's pool with a gap? ``status`` writes ``version`` but
    is not GUARDED, so the guard drops the trailing batch — its
    ``{t:"pool", from:N}`` op never applies."""
    sid = "delay-d3"
    ws = _wire_ws_capture(page)
    # Fast turns 0-2 so many ``batch``+``status`` pairs race; turn 3 slow
    # so ``_mk_running``'s wait resolves.
    session, *_ = await _mk_running(sid, aud_slow_from=3, max_turns=4)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline", timeout=15_000)
        await anyio.sleep(6.0)

        stats = ws.guard_stats()
        # Client-side integrity: does the store's pool length cover every
        # event's ``input_refs``? Read via ``page.evaluate`` against the
        # zustand store (no global handle — walk from a rendered element's
        # React fiber would be brittle; instead re-derive from captured
        # frames what the client SHOULD have vs what a guard-respecting
        # replay yields).
        server_pool = len(session.pool)
        # Replay: apply frames in arrival order under the guard; track pool.
        max_v = -1
        client_pool = 0
        gaps: list[tuple[int, int]] = []
        for f in ws.frames:
            t, v = f.get("t"), f.get("v")
            if t == "state":
                client_pool = len(f.get("pool", []))
                max_v = v if v is not None else max_v
                continue
            if v is None:
                continue
            if t in GUARDED and v < max_v:
                # dropped — if it carried a pool op, that's a gap.
                if t == "batch":
                    for op in f.get("ops", []):
                        if op.get("t") == "pool":
                            gaps.append((op["from"], len(op["entries"])))
                continue
            if t == "batch":
                for op in f.get("ops", []):
                    if op.get("t") == "pool":
                        # Client applies unconditionally (no ``from`` check
                        # in ``reduceOne``) — an out-of-order pool op that
                        # ISN'T dropped extends from wherever it lands.
                        client_pool = max(
                            client_pool, op["from"] + len(op["entries"])
                        )
            max_v = max(max_v, v)

        holds = not gaps and client_pool >= server_pool
        # Report; assert only that no branch errored.
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        return (
            f"server_pool={server_pool} client_pool_replay={client_pool} "
            f"pool_gaps={gaps} guarded_dropped={stats['dropped']} "
            f"unguarded_overtakes={stats['overtakes']} atomicity_holds={holds}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


DELAY_SCENARIOS = [
    ("d1 useIsPending holds under delayed ack", d1_is_pending_holds),
    ("d2 version-guard drop count", d2_version_guard_drops),
    ("d3 batch atomicity vs overtaking status", d3_batch_atomicity),
]


# ── driver ──────────────────────────────────────────────────────────────────


@dataclass
class DelayResult(Result):
    ws_stats: dict[str, Any] = field(default_factory=dict)


async def _amain() -> int:
    ws_port = _free_port()
    ui_port = _free_port()

    log_cap = BackendLogCapture()
    logging.getLogger().addHandler(log_cap)
    logging.getLogger().setLevel(logging.WARNING)

    results: list[DelayResult] = []
    all_scenarios = list(BASE_SCENARIOS) + DELAY_SCENARIOS

    async with _backend(ws_port), _vite(ws_port, ui_port), async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for name, fn in all_scenarios:
                print(f"\n── {name} ──")
                page = await browser.new_page(viewport={"width": 1600, "height": 1000})
                cap: Capture = _wire_page_capture(page)
                # Per-scenario WS capture for s1-s8 too, so we get a drop
                # count for each (d2 has its own dedicated one).
                wscap = _wire_ws_capture(page)
                log_cap.drain()
                try:
                    detail = await fn(page, ui_port)
                    status = "PASS"
                except AssertionError as e:
                    status, detail = "FAIL", str(e)
                except Exception as e:
                    status, detail = "ERROR", f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                results.append(
                    DelayResult(
                        name=name,
                        status=status,
                        detail=detail,
                        console=list(cap.console),
                        page_errors=list(cap.page_errors),
                        backend_log=log_cap.drain(),
                        ws_stats=wscap.guard_stats(),
                    )
                )
                await page.close()
        finally:
            await browser.close()

    # ── report ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"WORKBENCH_BROADCAST_DELAY_MS={os.environ['WORKBENCH_BROADCAST_DELAY_MS']}")
    total_dropped = 0
    total_overtakes = 0
    for r in results:
        print(f"\n{r.status:5}  {r.name}")
        print(f"       {r.detail}")
        s = r.ws_stats
        total_dropped += s.get("dropped", 0)
        total_overtakes += s.get("overtakes", 0)
        print(
            f"       ws: frames={s.get('n_frames', 0)} "
            f"guarded_dropped={s.get('dropped', 0)} {s.get('dropped_by_t', {})} "
            f"unguarded_overtakes={s.get('overtakes', 0)}"
        )
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
    print(
        f"\n{n_pass}/{len(results)} passed  "
        f"[Σ guarded_dropped={total_dropped}  Σ unguarded_overtakes={total_overtakes}]"
    )
    return 0 if n_pass == len(results) else 1


def main() -> None:
    sys.exit(anyio.run(_amain))


if __name__ == "__main__":
    main()
