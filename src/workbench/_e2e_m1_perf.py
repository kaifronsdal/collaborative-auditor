"""OVERNIGHT-SWEEP P1 perf verification (P12/P13/P16/P17/P23).

Drives the real UI (in-process backend + vite dev + headless chromium, same
harness as ``_e2e_m1_chaos``) through a 20-turn mockllm branch whose target
emits ~10 streaming ``{t:"update", pending:true}`` frames per generate, and
reads the dev-only ``window.__renderCount`` probe stamped in
``SwimlaneColumn`` / ``ModelEventRow``.

Claims under test
-----------------
- **P16** — ``useSwimlanes`` keys off ``eventsRev`` (bumped only on
  structural events, not per-chunk), so a ``SwimlaneColumn`` re-render per
  streaming frame does no expensive recompute.
- **P23** — ``ModelEventRow`` is memoized on ``turn.ev`` identity; a settled
  row must not re-render when a *later* row streams. Total row renders
  should be O(turns), not O(turns²) or O(turns × chunks).
- **P13** — mutable ``events`` Map: streaming updates don't clone the whole
  event store per frame (measured indirectly via wall-clock).
- **H5a** — the drain queue never overflows (``_desync`` stays 0) at 10
  chunks × 0.1s per turn.

The auditor script carries a short prose prefix so *both* columns stream.
Render counts are 2× logical due to React ``StrictMode`` in dev.

Run:  uv run python -m workbench._e2e_m1_perf
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field

import anyio
from inspect_ai.log._samples import update_active_model_event_output
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo
from playwright.async_api import async_playwright

from workbench._e2e_m1_chaos import BackendLogCapture, _wire_page_capture
from workbench._smoke_fixtures import _backend, _free_port, _vite
from workbench.run import Branch
from workbench.server import sessions
from workbench.session import Session

TURNS = 20
CHUNKS = 10
DELAY = 0.1
# React 18 StrictMode double-invokes render functions in dev.
STRICT = 2


# ── streaming mockllm ───────────────────────────────────────────────────────


def _auditor_turn(content: str, *calls: ToolCall) -> ModelOutput:
    """Auditor ModelOutput with prose (so it streams) plus tool calls."""
    out = ModelOutput.from_content(model="mockllm", content=content)
    out.choices[0].message.tool_calls = list(calls)
    return out


def _tc(cid: str, function: str, **arguments: object) -> ToolCall:
    return ToolCall(id=cid, function=function, type="function", arguments=arguments)


AUDITOR_SCRIPT: list[ModelOutput] = [
    _auditor_turn(
        "auditor turn 0 — establishing system prompt and opening",
        _tc("a0s", "set_system_message", system_message="sys"),
        _tc("a0m", "send_message", message="u0"),
        _tc("a0r", "resume"),
    ),
    *[
        _auditor_turn(
            f"auditor turn {k} — sending follow-up probe number {k}",
            _tc(f"a{k}m", "send_message", message=f"u{k}"),
            _tc(f"a{k}r", "resume"),
        )
        for k in range(1, TURNS)
    ],
    _auditor_turn(
        f"auditor turn {TURNS} — wrapping up",
        _tc(f"a{TURNS}e", "end_conversation"),
    ),
]


@dataclass
class StreamModel:
    """Async ``custom_outputs`` callable that emits ``chunks`` partial-output
    flushes (via :func:`update_active_model_event_output`) with ``delay``
    seconds between each, then returns the full output.

    ``script`` mode indexes by assistant-count (auditor); ``prefix`` mode
    replies with ``f"{prefix}{last_user}: …"`` (target).
    """

    delay: float
    chunks: int
    script: list[ModelOutput] | None = None  # auditor
    prefix: str | None = None  # target
    calls: int = 0
    updates: int = 0
    done: anyio.Event = field(default_factory=anyio.Event)

    async def __call__(
        self,
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        self.calls += 1
        k = sum(1 for m in input if m.role == "assistant")
        if self.script is not None:
            out = self.script[min(k, len(self.script) - 1)].model_copy(deep=True)
        else:
            assert self.prefix is not None
            last = next(m for m in reversed(input) if m.role in ("user", "tool"))
            body = f"{self.prefix}{last.text}: " + " ".join(
                f"tok{i}" for i in range(self.chunks * 3)
            )
            out = ModelOutput.from_content(model="mockllm", content=body)
        text = out.completion
        n = min(self.chunks, len(text)) or 1
        step = -(-len(text) // n)
        for i in range(n):
            await anyio.sleep(self.delay)
            update_active_model_event_output(
                ModelOutput.from_content(
                    model="mockllm", content=text[: (i + 1) * step]
                )
            )
            self.updates += 1
        return out


# ── driver ──────────────────────────────────────────────────────────────────


async def _amain() -> int:
    ws_port = _free_port()
    ui_port = _free_port()

    log_cap = BackendLogCapture()
    logging.getLogger().addHandler(log_cap)
    logging.getLogger().setLevel(logging.WARNING)

    async with _backend(ws_port), _vite(ws_port, ui_port), async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1600, "height": 1200})
        cap = _wire_page_capture(page)

        # -- session with a paused branch, so the page can load + settle
        #    before streaming begins and the probe is zeroed at t0.
        sid = "perf"
        aud = StreamModel(delay=DELAY, chunks=CHUNKS, script=AUDITOR_SCRIPT)
        tgt = StreamModel(delay=DELAY, chunks=CHUNKS, prefix="reply-to-")
        session = Session()
        await session.start()
        sessions[sid] = session
        b = Branch(
            session,
            "b0",
            seed="perf-seed",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=TURNS + 1,
            auditor_model_args={"custom_outputs": aud},
            target_model_args={"custom_outputs": tgt},
        )
        session.branches["b0"] = b
        session.current = "b0"
        # Leave paused: page loads, WS connects, initial `state` renders idle.
        session.branch_tasks["b0"] = asyncio.create_task(b.run())

        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline", timeout=15_000)
        await anyio.sleep(0.5)  # let connect-time renders settle
        await page.evaluate("window.__renderCount = {}")

        # -- track drain-queue peak while the branch runs.
        peak_q = 0
        stop = anyio.Event()

        async def _watch_q() -> None:
            nonlocal peak_q
            while not stop.is_set():
                peak_q = max(peak_q, session._send.statistics().current_buffer_used)
                await anyio.sleep(0.02)

        t0 = await page.evaluate("performance.now()")
        b.play()
        await session.broadcast_status()
        watch = asyncio.create_task(_watch_q())

        with anyio.fail_after(TURNS * CHUNKS * DELAY * 4 + 30):
            while b.status not in ("ended", "error"):
                await anyio.sleep(0.1)
        stop.set()
        await watch
        assert b.error is None, f"branch errored: {b.error}"

        # Let final frames drain + React commit.
        await anyio.sleep(1.0)
        t1 = await page.evaluate("performance.now()")
        rc: dict[str, int] = await page.evaluate("window.__renderCount ?? {}")

        await page.close()
        await browser.close()

        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()

    # ── analyse ─────────────────────────────────────────────────────────────
    backend_log = log_cap.drain()
    desyncs = sum(1 for r in backend_log if "wire buffer full" in r)

    sc_total = rc.get("SwimlaneColumn", 0)
    sc_aud = rc.get("SwimlaneColumn:auditor", 0)
    sc_tgt = rc.get("SwimlaneColumn:target", 0)
    mer_total = rc.get("ModelEventRow", 0)
    memo_miss = {
        k.removeprefix("memoMiss:"): v
        for k, v in rc.items()
        if k.startswith("memoMiss:")
    }
    per_row = {
        k.removeprefix("ModelEventRow:"): v
        for k, v in rc.items()
        if k.startswith("ModelEventRow:") and k != "ModelEventRow"
    }
    aud_rows = {k: v for k, v in per_row.items() if k.startswith("auditor:")}
    tgt_rows = {k: v for k, v in per_row.items() if k.startswith("target:")}
    max_row = max(per_row.values(), default=0)
    med_row = sorted(per_row.values())[len(per_row) // 2] if per_row else 0

    stream_frames = aud.updates + tgt.updates
    wall_ms = t1 - t0

    # Budgets (×STRICT for StrictMode double-render). These are the O()
    # expectations from OVERNIGHT-SWEEP P1; a miss is a *finding*, not a
    # harness failure — reported below and reflected in the exit code.
    n_rows = TURNS * 2 + 1  # auditor turns 0..TURNS + target turns 0..TURNS-1
    sc_budget = n_rows * 4 * STRICT  # O(turns): a few structural bumps/turn
    sc_bad = stream_frames * STRICT // 2  # O(turns × chunks) failure floor
    row_budget = (CHUNKS + 4) * STRICT  # streaming row: ~chunks; settled: ~few
    mer_budget = n_rows * row_budget  # O(turns) total (each row bounded)
    mer_bad = n_rows * n_rows * STRICT // 2  # O(turns²) failure floor

    print("\n" + "=" * 72)
    print(
        f"turns={TURNS}  chunks/gen={CHUNKS}  delay={DELAY}s  "
        f"generates: aud={aud.calls} tgt={tgt.calls}  "
        f"stream-updates={stream_frames}"
    )
    print(
        f"wall-clock: {wall_ms:.0f} ms  "
        f"(floor ≈ {stream_frames * DELAY * 1000:.0f} ms of sleep)"
    )
    print(f"drain-queue peak: {peak_q}  _desync fires: {desyncs}")
    print()
    print(f"SwimlaneColumn renders (total, ×{STRICT} StrictMode):")
    print(f"  total   = {sc_total:5}   auditor = {sc_aud:5}   target = {sc_tgt:5}")
    print(f"  budget  ≤ {sc_budget:5}  (O(turns))")
    print(f"  fail-if ≥ {sc_bad:5}  (O(turns × chunks))")
    print()
    print(f"ModelEventRow renders (×{STRICT} StrictMode):")
    print(
        f"  total   = {mer_total:5}   over {len(per_row)} distinct rows "
        f"(aud={len(aud_rows)} tgt={len(tgt_rows)})"
    )
    print(f"  budget  ≤ {mer_budget:5}  (O(turns), each row ≤ {row_budget})")
    print(f"  fail-if ≥ {mer_bad:5}  (O(turns²))")
    print(f"  per-row: max={max_row}  median={med_row}")
    if memo_miss:
        print(f"  arePropsEqual misses by first-failing check: {memo_miss}")
    if per_row:
        print("  distribution (renders → #rows):")
        hist: dict[int, int] = {}
        for v in per_row.values():
            hist[v] = hist.get(v, 0) + 1
        for v in sorted(hist):
            print(f"    {v:4} → {hist[v]:3}")
    print()
    if cap.page_errors:
        print("page errors:")
        for e in cap.page_errors:
            print(f"  ! {e}")
    if cap.console:
        print("console (error/warning):")
        for c in cap.console:
            print(f"  · {c}")
    if backend_log:
        print("backend log (≥WARNING):")
        for line in backend_log:
            print(f"  │ {line}")

    # ── verdict ─────────────────────────────────────────────────────────────
    findings: list[str] = []
    if desyncs > 0:
        findings.append(f"drain overflow: {desyncs} `_desync` fires")
    if sc_total >= sc_bad:
        findings.append(
            f"SwimlaneColumn is O(turns × chunks): {sc_total} renders for "
            f"{stream_frames} stream frames — `useStagedForTarget` subscribes "
            f"to `byRole[target]` (churns per chunk) in both columns"
        )
    if mer_total >= mer_bad:
        findings.append(
            f"ModelEventRow total is O(turns²): {mer_total} renders. "
            f"{memo_miss.get('ev', 0)} `arePropsEqual` misses on `turn.ev` "
            f"identity — `splice()`→`stripSuffix()` clones every event on each "
            f"`eventsRev` bump, so the P23 memo's stable-store-ref assumption "
            f"fails on the swimlane path (holds on `LinearColumn`/`byRole` only)"
        )
    elif max_row > row_budget:
        findings.append(
            f"at least one row re-rendered {max_row}× (budget {row_budget}) — "
            f"P23 memo not holding"
        )
    if cap.page_errors:
        findings.append(f"{len(cap.page_errors)} page error(s)")

    print("=" * 72)
    if findings:
        print("FINDINGS:")
        for f in findings:
            print(f"  ✗ {f}")
        return 1
    print("PASS — render counts within O() budgets; no desync; no page errors")
    return 0


def main() -> None:
    sys.exit(anyio.run(_amain))


if __name__ == "__main__":
    main()
