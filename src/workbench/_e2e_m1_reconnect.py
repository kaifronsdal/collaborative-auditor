"""Reconnect-storm stress test: WS drops + reconnects rapidly during a run.

Drives the real UI (in-process backend + vite dev + headless chromium) on
free ports. A 5-turn, 2s-per-generate mockllm branch runs while the test
force-closes the client's WebSocket every 1-3s and immediately reconnects
(via ``useSession.getState().connect(sid)`` — there is no auto-reconnect in
the store, so the storm exercises the manual path a "reconnect" button /
network flap would take).

Scenarios (observation-first — a FAIL is a finding, not a bug to fix here):

  s1  reconnect storm during a running audit
        · every 1-3s: ``ws.close()`` → ``connect(sid)`` → ``push_full_state``
        · assert on each reconnect: ``pending: []`` (F3+A2 — ``connect()``
          resets it) → ``useIsPending`` never sticks a button disabled
        · after all 5 turns: frontend ``byRole["b0"]["auditor"]`` has 5
          non-rewound ``ModelEvent``s (nothing lost across ``push_full_state``)
        · target column renders all 5 rows (session-wide timeline survives)
        · no console errors, no ``.error-banner``, no backend ``{t:"error"}``

  s2  reconnect DURING a ``_pendingChild`` fork
        · click branch on target row 0 → immediately ``ws.close()``
        · reconnect
        · either the fork completed server-side (child in ``session.branches``
          → appears on reconnect via ``push_full_state``), OR the send raced
          the close and the cmd was lost (OK — user re-clicks). Either way:
          ``pending: []`` and the branch button re-enables (``forkPending``
          derives from ``pending[]``, which ``connect()`` cleared).

Run:  uv run python -m workbench._e2e_m1_reconnect
"""

from __future__ import annotations

import asyncio
import copy
import logging
import random
import sys
import time
import traceback
from dataclasses import dataclass, field

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo
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

SLEEP_S = 2.0  # per generate (task spec)

# ── slow mockllm ────────────────────────────────────────────────────────────

# 5-turn auditor script: every turn sends + resumes → 5 target replies.
# ``max_turns=5`` ends the loop after turn 4, so no ``end_conversation``.
SCRIPT5: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="u3"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="u4"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="u5"), _tc("resume")),
]
TARGET_TABLE = {"u1": "r1", "u2": "r2", "u3": "r3", "u4": "r4", "u5": "r5"}


@dataclass
class SlowModel:
    """Async ``custom_outputs``: sleep ``SLEEP_S`` on turns ≥ ``slow_from``."""

    script: list[ModelOutput] | None = None
    table: dict[str, str] | None = None
    slow_from: int = 0
    entered: list[anyio.Event] = field(default_factory=list)

    def entered_at(self, k: int) -> anyio.Event:
        while len(self.entered) <= k:
            self.entered.append(anyio.Event())
        return self.entered[k]

    async def __call__(
        self,
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        self.entered_at(k).set()
        if k >= self.slow_from:
            await asyncio.sleep(SLEEP_S)
        if self.script is not None:
            return copy.deepcopy(self.script[min(k, len(self.script) - 1)])
        assert self.table is not None
        last = next(m for m in reversed(input) if m.role in ("user", "tool"))
        return _target(self.table.get(last.text, f"r?{last.text}"))


# ── capture harness (from _e2e_m1_chaos) ────────────────────────────────────


class BackendLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        if record.exc_info:
            self.records.append(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )

    # WS-close-mid-receive noise from starlette/uvicorn — teardown ordering,
    # not a finding. The storm produces one per drop.
    _NOISE = (
        'Need to call "accept" first',
        "ASGI application",
        "WebSocket is not connected",
        "close message has been sent",
    )

    def drain(self) -> list[str]:
        out, self.records = self.records, []
        return [r for r in out if not any(n in r for n in self._NOISE)]


@dataclass
class Capture:
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)


_CONSOLE_NOISE = (
    "Lit is in dev mode",
    # ``ws.close()`` mid-flight → the server may have a frame queued that
    # never arrives; the browser logs a generic "WebSocket is already in
    # CLOSING or CLOSED state" if a `send()` raced the close. Not a finding.
    "CLOSING or CLOSED state",
    # Vite HMR socket also closes when the page's beforeunload fires on
    # teardown; irrelevant to the app WS.
    "[vite]",
)


def _wire_page_capture(page: Page) -> Capture:
    cap = Capture()
    page.on("pageerror", lambda e: cap.page_errors.append(str(e)))
    page.on(
        "console",
        lambda m: (
            cap.console.append(f"[{m.type}] {m.text}")
            if m.type in ("error", "warning")
            and not any(n in m.text for n in _CONSOLE_NOISE)
            else None
        ),
    )
    return cap


# ── store bridge ────────────────────────────────────────────────────────────
#
# The zustand store isn't on ``window``. In vite dev mode modules are served
# as ESM at their source path, so a dynamic ``import('/src/store/session.ts')``
# resolves to the SAME module instance the app uses (vite's module graph is a
# singleton). Bind ``useSession`` to ``window.__store`` once, then poke it
# from ``page.evaluate``.

_INSTALL_STORE = """
async () => {
  if (window.__store) return true;
  const m = await import('/src/store/session.ts');
  window.__store = m.useSession;
  return true;
}
"""


async def _install_store(page: Page) -> None:
    await page.wait_for_function("() => !!document.querySelector('.app')")
    ok = await page.evaluate(_INSTALL_STORE)
    assert ok, "failed to bind useSession to window.__store"


async def _store_state(page: Page, expr: str):
    """``page.evaluate`` a JS expression against ``useSession.getState()``."""
    return await page.evaluate(
        f"() => {{ const s = window.__store.getState(); return ({expr}); }}"
    )


async def _drop_and_reconnect(page: Page, sid: str) -> None:
    """Close the app WS, then reconnect. Waits for the fresh socket to OPEN
    and the ``push_full_state`` snapshot to land (``version > 0``)."""
    await page.evaluate(
        """(sid) => {
          const st = window.__store.getState();
          try { st.ws?.close(); } catch (e) {}
          // connect() is idempotent on (ws, sessionId) — but ws.close() is
          // async; the onclose handler hasn't cleared them yet. Force the
          // guard open so the reconnect isn't a no-op.
          window.__store.setState({ ws: null, sessionId: null });
          st.connect(sid);
        }""",
        sid,
    )
    # Wait for OPEN (readyState 1) — the storm interval must not outrun the
    # handshake or we'd be measuring TCP, not the reducer.
    await page.wait_for_function(
        "() => window.__store.getState().ws?.readyState === 1", timeout=10_000
    )
    await page.wait_for_function(
        "() => window.__store.getState().version > 0", timeout=10_000
    )


# ── fixture ─────────────────────────────────────────────────────────────────


async def _mk_running(
    sid: str,
    *,
    aud_slow_from: int = 0,
    tgt_slow_from: int = 0,
    max_turns: int = 5,
) -> tuple[Session, Branch, SlowModel]:
    aud = SlowModel(script=SCRIPT5, slow_from=aud_slow_from)
    tgt = SlowModel(table=TARGET_TABLE, slow_from=tgt_slow_from)
    session = Session()
    await session.start()
    sessions[sid] = session
    b = Branch(
        session,
        "b0",
        seed=f"reconnect-{sid}",
        auditor_model="mockllm/model",
        target_model="mockllm/model",
        max_turns=max_turns,
        auditor_model_args={"custom_outputs": aud},
        target_model_args={"custom_outputs": tgt},
    )
    session.branches["b0"] = b
    session.current = "b0"
    b.play()
    session.branch_tasks["b0"] = asyncio.create_task(b.run())
    return session, b, aud


def _model_events(session: Session, key: tuple[str, str]) -> list[dict]:
    """Non-rewound, non-pending ``ModelEvent``s in ``by_role[key]`` order."""
    return [
        session.events[u]
        for u in session.by_role.get(key, [])
        if session.events[u].get("event") == "model"
        and not session.events[u].get("pending")
        and not session.events[u].get("rewound")
    ]


# ── scenarios ───────────────────────────────────────────────────────────────


async def s1_reconnect_storm(page: Page, ui_port: int) -> str:
    sid = "reconn-s1"
    session, b, _aud = await _mk_running(sid, aud_slow_from=0, tgt_slow_from=0)
    rng = random.Random(0xC0FFEE)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await _install_store(page)
        await page.wait_for_selector(".runline.status-running", timeout=15_000)

        # First generate is now in flight (2s each, 5 auditor + 5 target ≈
        # 20s total). Storm the socket until the branch ends.
        n_reconnects = 0
        pending_after: list[int] = []
        deadline = time.monotonic() + 60.0
        while b.status != "ended" and time.monotonic() < deadline:
            await anyio.sleep(rng.uniform(1.0, 3.0))
            if b.status == "ended":
                break
            await _drop_and_reconnect(page, sid)
            n_reconnects += 1
            # F3 + A2: ``connect()`` sets ``pending: []`` synchronously; the
            # ``{t:"state"}`` snapshot's reducer also returns ``pending: []``.
            p = await _store_state(page, "s.pending.length")
            pending_after.append(p)
            # No stuck ``useIsPending`` — the fork-family predicate is what
            # gates every branch/resample button. Must be false immediately
            # post-reconnect regardless of what was in flight before the drop.
            fork_pending = await _store_state(
                page,
                "s.pending.some(c => "
                "['branch','resample','edit_target_message','resample_auditor',"
                "'branch_auditor','edit_auditor_call'].includes(c.t))",
            )
            assert p == 0, (
                f"reconnect #{n_reconnects}: pending[] not reset "
                f"(len={p}) — connect()/state should clear it"
            )
            assert fork_pending is False, (
                f"reconnect #{n_reconnects}: useIsPending(FORK_KINDS) stuck "
                f"true — button would stay disabled"
            )

        assert b.status == "ended", (
            f"branch never ended (status={b.status!r} after "
            f"{time.monotonic() - deadline + 60:.0f}s, {n_reconnects} reconnects)"
        )
        assert b.error is None, f"branch errored: {b.error}"
        assert n_reconnects >= 3, (
            f"only {n_reconnects} reconnect(s) — storm window too short to "
            f"stress anything"
        )

        # One final reconnect after ``ended`` so the frontend's snapshot is
        # the terminal state (a mid-storm drop may have raced the last event).
        await _drop_and_reconnect(page, sid)
        await page.wait_for_selector(".runline.status-ended", timeout=10_000)

        # ── backend ground truth ────────────────────────────────────────────
        be_aud = _model_events(session, ("b0", "auditor"))
        be_tgt = _model_events(session, ("b0", "target"))
        assert len(be_aud) == 5, (
            f"backend by_role[b0,auditor] has {len(be_aud)} ModelEvents "
            f"(want 5) — events lost server-side across reconnects"
        )
        assert len(be_tgt) == 5, (
            f"backend by_role[b0,target] has {len(be_tgt)} ModelEvents (want 5)"
        )

        # ── frontend byRole (via push_full_state) ───────────────────────────
        fe_aud = await _store_state(
            page,
            "(s.byRole['b0']?.auditor ?? [])"
            ".filter(e => e.event === 'model' && !e.pending && !e.rewound).length",
        )
        fe_tgt = await _store_state(
            page,
            "(s.byRole['b0']?.target ?? [])"
            ".filter(e => e.event === 'model' && !e.pending && !e.rewound).length",
        )
        assert fe_aud == 5, (
            f"frontend byRole['b0']['auditor'] has {fe_aud} ModelEvents "
            f"(want 5) — push_full_state dropped events across reconnect"
        )
        assert fe_tgt == 5, (
            f"frontend byRole['b0']['target'] has {fe_tgt} ModelEvents (want 5)"
        )

        # ── target column DOM (session-wide timeline survives reconnect) ────
        tgt_col = page.locator(".columns .col-wrap").last
        # give the timeline op a beat to render post-snapshot
        for _ in range(40):
            if await tgt_col.locator(".model-event-row").count() >= 5:
                break
            await anyio.sleep(0.1)
        tgt_rows = await tgt_col.locator(".model-event-row").count()
        assert tgt_rows == 5, (
            f"target column shows {tgt_rows} rows (want 5) — session-wide "
            f"timeline not fully reconstructed on reconnect"
        )

        # ── no errors ───────────────────────────────────────────────────────
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner shown: "
            f"{await page.locator('.error-banner').text_content()!r}"
        )
        fe_err = await _store_state(page, "s.error")
        assert fe_err is None, f"store.error set: {fe_err!r} — a {{t:'error'}} landed"

        return (
            f"reconnects={n_reconnects} "
            f"pending_after={pending_after} "
            f"backend_aud={len(be_aud)} backend_tgt={len(be_tgt)} "
            f"fe_aud={fe_aud} fe_tgt={fe_tgt} tgt_rows={tgt_rows}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s2_fork_mid_reconnect(page: Page, ui_port: int) -> str:
    sid = "reconn-s2"
    # Turns 0-1 fast (r1, r2 on screen), turn 2+ slow → click window.
    session, _b, aud = await _mk_running(
        sid, aud_slow_from=2, tgt_slow_from=99, max_turns=5
    )
    try:
        with anyio.fail_after(10.0):
            await aud.entered_at(2).wait()
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await _install_store(page)
        await page.wait_for_selector(".runline.status-running", timeout=15_000)

        tgt_col = page.locator(".columns .col-wrap").last
        row = tgt_col.locator(".model-event-row").nth(0)
        await row.wait_for(state="attached", timeout=15_000)
        await row.hover()
        branch_btn = row.locator(".actions button[title='branch at this turn']")

        n_branches_before = len(session.branches)

        # Click branch → immediately drop the socket. The click's ``send()``
        # → ``ws.send(JSON)`` runs synchronously in the click handler, so it
        # *should* reach the server before the close frame — but the ack
        # can't (socket's gone). ``connect()`` must clear ``pending`` so the
        # button re-enables regardless.
        await branch_btn.click(force=True)
        await page.evaluate(
            "() => { try { window.__store.getState().ws?.close(); } catch(e){} }"
        )
        # Small beat for the server's ``_dispatch`` to run (or not).
        await anyio.sleep(0.5)
        n_after_close = len(session.branches)

        # Reconnect.
        await page.evaluate(
            """(sid) => {
              window.__store.setState({ ws: null, sessionId: null });
              window.__store.getState().connect(sid);
            }""",
            sid,
        )
        await page.wait_for_function(
            "() => window.__store.getState().ws?.readyState === 1", timeout=10_000
        )
        await page.wait_for_function(
            "() => window.__store.getState().version > 0", timeout=10_000
        )
        await anyio.sleep(0.5)

        # ── pending cleared / button re-enabled ─────────────────────────────
        p = await _store_state(page, "s.pending.length")
        assert p == 0, (
            f"pending[] not cleared on reconnect (len={p}) — button stays "
            f"disabled forever"
        )
        fork_pending = await _store_state(
            page,
            "s.pending.some(c => "
            "['branch','resample','edit_target_message','resample_auditor',"
            "'branch_auditor','edit_auditor_call'].includes(c.t))",
        )
        assert fork_pending is False, "useIsPending(FORK_KINDS) stuck true"

        # ── did the fork land? ──────────────────────────────────────────────
        # Two OK outcomes:
        #   (a) send reached the server → child spawned → ``push_full_state``
        #       carries it → sidebar row count == backend branch count.
        #   (b) send raced the close and was lost → still 1 branch → user
        #       re-clicks. Also OK; the button being enabled is the assert.
        n_children = len(session.branches) - n_branches_before
        outcome = "child-spawned" if n_children >= 1 else "cmd-lost"

        fe_branches = await _store_state(page, "Object.keys(s.branches).length")
        assert fe_branches == len(session.branches), (
            f"frontend branches={fe_branches} vs backend={len(session.branches)} "
            f"— push_full_state didn't ship the child ({outcome})"
        )
        rows = await page.locator(".side-branches .side-row").count()
        assert rows == len(session.branches), (
            f"sidebar rows={rows} vs backend branches={len(session.branches)} "
            f"— PENDING_BRANCH ghost or missing child row"
        )

        if outcome == "child-spawned":
            child_id = next(bid for bid in session.branches if bid != "b0")
            # ``_register_and_spawn`` repointed ``session.current`` to the
            # child; ``push_full_state`` should reflect that.
            fe_current = await _store_state(page, "s.current")
            assert fe_current == session.current, (
                f"frontend current={fe_current!r} vs backend={session.current!r}"
            )
            errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
            assert not errs, f"branch errors after fork+reconnect: {errs}"
            print(f"    [obs] fork completed server-side → child {child_id!r}")
        else:
            # cmd lost — the branch button must be enabled so a re-click works.
            # Hover the row again (reconnect re-rendered the column).
            row = tgt_col.locator(".model-event-row").nth(0)
            await row.wait_for(state="attached", timeout=10_000)
            await row.hover()
            btn = row.locator(".actions button[title='branch at this turn']")
            disabled = await btn.get_attribute("disabled")
            assert disabled is None, (
                f"branch button disabled={disabled!r} after lost cmd + "
                f"reconnect — user cannot re-click"
            )
            print("    [obs] fork cmd lost across close — button re-enabled")

        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        return (
            f"outcome={outcome} n_children={n_children} "
            f"n_after_close={n_after_close} pending_len={p} "
            f"fe_branches={fe_branches} sidebar_rows={rows}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


# ── driver ──────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("s1 reconnect storm during running audit", s1_reconnect_storm),
    ("s2 reconnect during _pendingChild fork", s2_fork_mid_reconnect),
]


@dataclass
class Result:
    name: str
    status: str
    detail: str
    console: list[str]
    page_errors: list[str]
    backend_log: list[str]


async def _amain() -> int:
    ws_port = _free_port()
    ui_port = _free_port()

    log_cap = BackendLogCapture()
    logging.getLogger().addHandler(log_cap)
    logging.getLogger().setLevel(logging.WARNING)

    results: list[Result] = []

    async with _backend(ws_port), _vite(ws_port, ui_port), async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for name, fn in SCENARIOS:
                print(f"\n── {name} ──")
                page = await browser.new_page(viewport={"width": 1600, "height": 1000})
                cap = _wire_page_capture(page)
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

    # ── report ──────────────────────────────────────────────────────────────
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
