"""Chaos playwright: rapid / out-of-order clicks against a slow mockllm.

Drives the real UI (in-process backend + vite dev + headless chromium) on
free ports (via ``_smoke_fixtures._backend`` / ``_vite``). Each scenario
gets a fresh ``Session`` id, its own ``page.on("console")`` /
``page.on("pageerror")`` capture, and a shared ``logging`` handler that
records everything ≥ WARNING from the backend.

The mockllm auditor + target use an async ``custom_outputs`` callable that
``await asyncio.sleep(3)`` per *live* generate — slow enough for a click to
race the in-flight call, fast enough that a scenario settles in <30s. Turns
before ``slow_from`` return instantly so we can lay down clickable target
bubbles before the race window opens.

Scenarios (each is *observation-only* — assertions are minimal invariants;
a FAIL is a finding, not a test to be fixed here):

  1. double-pause         click ``.primary`` twice in <100ms mid-generate
  2. pause→play→pause     3× ``.primary`` in <300ms
  3. branch mid-generate  click a target row's branch action while the
                          auditor is mid-generate
  4. double-branch        same anchor, branch clicked twice in <200ms
  5. inject×2 around pause  send → pause → send → play; both messages must
                          reach the auditor's next generate ``input``
  6. resample × 2 anchors click resample on r2, then immediately on r1

Run:  uv run python -m workbench._e2e_m1_chaos
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
import traceback
from dataclasses import dataclass, field

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo
from playwright.async_api import Locator, Page, async_playwright

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

SLEEP_S = 3.0  # per slow generate — the race window

# ── slow mockllm ────────────────────────────────────────────────────────────

# 4-turn auditor script: two live target replies (r1, r2) before turn 2 goes
# slow, so the target column has clickable rows when the race window opens.
SCRIPT4: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(_tc("send_message", message="u3"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]
TARGET_TABLE = {"u1": "r1", "u2": "r2", "u3": "r3"}


@dataclass
class SlowModel:
    """Async ``custom_outputs`` callable: sleep ``SLEEP_S`` on turns
    ≥ ``slow_from``, return ``script[k]`` (auditor) or ``table[last_user]``
    (target). Records every ``input`` list it sees so tests can assert on
    what actually reached the model."""

    script: list[ModelOutput] | None = None  # auditor mode
    table: dict[str, str] | None = None  # target mode
    slow_from: int = 0
    entered: list[anyio.Event] = field(default_factory=list)
    inputs: list[list[ChatMessage]] = field(default_factory=list)

    def entered_at(self, k: int) -> anyio.Event:
        """Event that fires when the k-th assistant generate is entered."""
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
        self.inputs.append(list(input))
        self.entered_at(k).set()
        if k >= self.slow_from:
            await asyncio.sleep(SLEEP_S)
        if self.script is not None:
            return copy.deepcopy(self.script[min(k, len(self.script) - 1)])
        assert self.table is not None
        last = next(m for m in reversed(input) if m.role in ("user", "tool"))
        return _target(self.table.get(last.text, f"r?{last.text}"))


# ── capture harness ─────────────────────────────────────────────────────────


class BackendLogCapture(logging.Handler):
    """Collect every backend log record ≥ WARNING for the report."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        if record.exc_info:
            self.records.append(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )

    _NOISE = (
        # Page close mid-WS-receive → starlette raises after disconnect; not a
        # scenario finding, just teardown ordering.
        'Need to call "accept" first',
        "ASGI application",
    )

    def drain(self) -> list[str]:
        out, self.records = self.records, []
        return [r for r in out if not any(n in r for n in self._NOISE)]


@dataclass
class Capture:
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)


_CONSOLE_NOISE = ("Lit is in dev mode",)


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


# ── fixture ─────────────────────────────────────────────────────────────────


async def _mk_running(
    sid: str,
    *,
    aud_slow_from: int,
    tgt_slow_from: int = 99,
    max_turns: int = 4,
) -> tuple[Session, Branch, SlowModel, SlowModel]:
    """Register a fresh session with one running branch ``b0`` on slow mockllm.

    Returns once the auditor has *entered* its first slow generate
    (``aud_slow_from``), so the caller lands mid-race-window.
    """
    aud = SlowModel(script=SCRIPT4, slow_from=aud_slow_from)
    tgt = SlowModel(table=TARGET_TABLE, slow_from=tgt_slow_from)
    session = Session()
    await session.start()
    sessions[sid] = session
    b = Branch(
        session,
        "b0",
        seed=f"chaos-{sid}",
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
    with anyio.fail_after(10.0):
        await aud.entered_at(aud_slow_from).wait()
    return session, b, aud, tgt


def _tape_len(b: Branch) -> int:
    return len(b.audit_tape.log)


async def _target_row(page: Page, nth: int) -> Locator:
    tgt_col = page.locator(".columns .col-wrap").last
    row = tgt_col.locator(".model-event-row").nth(nth)
    await row.wait_for(state="attached", timeout=15_000)
    await row.hover()
    return row


# ── scenarios ───────────────────────────────────────────────────────────────


async def s1_double_pause(page: Page, ui_port: int) -> str:
    sid = "chaos-s1"
    session, b, *_ = await _mk_running(sid, aud_slow_from=0)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        btn = page.locator(".composer-lower .primary")
        tape_before = _tape_len(b)

        # Two clicks on the primary button in <100ms — second click may land
        # on ``.primary-play`` (optimistic flip) or a stale ``.primary-pause``.
        await btn.click(force=True)
        await anyio.sleep(0.05)
        await btn.click(force=True)

        # Let the backend settle (dispatch lock + cancel + broadcast).
        await anyio.sleep(1.5)
        assert b.error is None, f"branch errored: {b.error}"
        # Expect: no exception, status paused, tape unchanged (the interrupted
        # generate produced no output).
        assert b.status == "paused", f"status={b.status!r} (want 'paused')"
        assert _tape_len(b) == tape_before, (
            f"tape grew {tape_before}→{_tape_len(b)} during double-pause"
        )
        await page.wait_for_selector(".runline.status-paused", timeout=5_000)
        return f"status={b.status!r} tape_len={_tape_len(b)} (unchanged)"
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s2_pause_play_pause(page: Page, ui_port: int) -> str:
    sid = "chaos-s2"
    session, b, *_ = await _mk_running(sid, aud_slow_from=0)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        btn = page.locator(".composer-lower .primary")

        for _ in range(3):
            await btn.click(force=True)
            await anyio.sleep(0.08)

        await anyio.sleep(2.0)
        assert b.error is None, f"branch errored: {b.error}"
        assert b.status in ("paused", "running"), (
            f"status={b.status!r} not a definite state"
        )
        # No orphan tasks: every task in branch_tasks is either done or the
        # one owning a live Branch (i.e. its branch_id resolves).
        orphans = [
            bid
            for bid, t in session.branch_tasks.items()
            if not t.done() and bid not in session.branches
        ]
        assert not orphans, f"orphan tasks: {orphans}"
        cls = await page.locator(".runline").get_attribute("class")
        return f"status={b.status!r} runline={cls!r} tasks={len(session.branch_tasks)}"
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s3_branch_mid_generate(page: Page, ui_port: int) -> str:
    sid = "chaos-s3"
    # Turns 0-1 fast → r1, r2 on screen; turn 2 slow → mid-generate window.
    session, b0, *_ = await _mk_running(sid, aud_slow_from=2)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        row = await _target_row(page, 0)  # r1
        assert b0.generating == "auditor", f"not mid-generate: {b0.generating!r}"

        await row.locator(".actions button[title='branch at this turn']").click(
            force=True
        )

        # Backend: parent stopped, one child spawned. ``_stop_running_branches``
        # deletes the parent's entry from ``branch_tasks`` on cancel.
        for _ in range(200):
            if len(session.branches) >= 2:
                break
            await anyio.sleep(0.05)
        assert len(session.branches) >= 2, "no child spawned"
        for _ in range(200):
            t = session.branch_tasks.get("b0")
            if t is None or t.done():
                break
            await anyio.sleep(0.05)
        t = session.branch_tasks.get("b0")
        assert t is None or t.done(), "parent task not stopped"
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        # UI: no error banner (would carry "turn_index out of range" etc).
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner shown: "
            f"{await page.locator('.error-banner').text_content()!r}"
        )
        child_id = next(bid for bid in session.branches if bid != "b0")
        parent_stopped = (
            "b0" not in session.branch_tasks or session.branch_tasks["b0"].done()
        )
        return (
            f"children={len(session.branches) - 1} parent_stopped={parent_stopped} "
            f"child_status={session.branches[child_id].status!r}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s4_double_branch(page: Page, ui_port: int) -> str:
    sid = "chaos-s4"
    session, *_ = await _mk_running(sid, aud_slow_from=2)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        row = await _target_row(page, 0)
        # One live locator, clicked twice — after the first click the frontend
        # installs ``PENDING_BRANCH`` (truncated view keeps r1), and shortly
        # after the backend's ``state`` swaps ``current`` to the real child.
        # ``force=True`` + short timeout: if the row/button has vanished by
        # the second click that's an *observation* (target column empty on the
        # forked child), not a harness failure.
        branch_btn = row.locator(".actions button[title='branch at this turn']")

        await branch_btn.click(force=True, no_wait_after=True)
        await anyio.sleep(0.03)
        second_click_landed = True
        try:
            await branch_btn.click(force=True, no_wait_after=True, timeout=1_000)
        except Exception as e:
            second_click_landed = False
            print(f"    [obs] second branch click did not land: {type(e).__name__}")

        await anyio.sleep(2.0)
        # Observation: does the child's target column render the replayed
        # prefix (r1)? If empty, a rapid re-click has nothing to hit — which
        # de facto prevents double-branch, but is its own UX note.
        tgt_rows = (
            await page.locator(".columns .col-wrap")
            .last.locator(".model-event-row")
            .count()
        )
        n_children = len(session.branches) - 1
        assert n_children in (1, 2), f"unexpected child count: {n_children}"
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        # No PENDING_BRANCH stuck: sidebar row count == backend branch count.
        rows = await page.locator(".side-branches .side-row").count()
        assert rows == len(session.branches), (
            f"sidebar rows={rows} vs backend branches={len(session.branches)} "
            f"— PENDING_BRANCH likely stuck"
        )
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        return (
            f"children={n_children} sidebar_rows={rows} "
            f"second_click_landed={second_click_landed} "
            f"child_target_rows={tgt_rows}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s5_inject_pause_inject_play(page: Page, ui_port: int) -> str:
    sid = "chaos-s5"
    session, b, aud, _ = await _mk_running(sid, aud_slow_from=0, max_turns=6)
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        composer = page.locator(".columns .col-wrap").first.locator(".composer-input")
        btn = page.locator(".composer-lower .primary")

        # inject "steer-1" → pause → inject "steer-2" → play, all in rapid
        # succession while the first auditor generate is in flight.
        await composer.fill("steer-1")
        await composer.press("Enter")
        await anyio.sleep(0.05)
        await btn.click(force=True)  # pause (interrupt)
        await anyio.sleep(0.05)
        await composer.fill("steer-2")
        await composer.press("Enter")
        await anyio.sleep(0.05)
        await btn.click(force=True)  # play (or pause-again, depending on race)

        # Wait for the *next* auditor generate to fire and inspect its input.
        with anyio.move_on_after(SLEEP_S * 3):
            while len(aud.inputs) < 2:
                await anyio.sleep(0.05)
        assert b.error is None, f"branch errored: {b.error}"
        seen: set[str] = set()
        for inp in aud.inputs:
            for m in inp:
                if m.role == "user":
                    seen.add(m.text)
        missing = {"steer-1", "steer-2"} - seen
        assert not missing, (
            f"injected messages never reached auditor input: missing={sorted(missing)} "
            f"(queued now={[m.text for m in b.queued['auditor']]}, "
            f"status={b.status!r}, generates={len(aud.inputs)})"
        )
        return (
            f"generates={len(aud.inputs)} "
            f"user_msgs_seen={sorted(seen)} status={b.status!r}"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


async def s6_resample_two_anchors(page: Page, ui_port: int) -> str:
    sid = "chaos-s6"
    # Need r1 + r2 on screen; slow target so the resample child's regenerate
    # is in flight when the second click lands.
    session, *_ = await _mk_running(sid, aud_slow_from=2, tgt_slow_from=0)
    # aud_slow_from=2 with tgt_slow_from=0 means turns 0/1 each spend 3s in
    # the target — _mk_running waited for auditor turn 2 entry, so r1+r2 are
    # already on the tape.
    try:
        await page.goto(f"http://127.0.0.1:{ui_port}/?session={sid}")
        await page.wait_for_selector(".runline.status-running", timeout=15_000)
        # Click resample on r2 first (so r1 survives the optimistic truncate),
        # then immediately on r1.
        row_r2 = await _target_row(page, 1)
        await row_r2.locator(
            ".actions button[title='resample — regenerate this response']"
        ).click(force=True)
        await anyio.sleep(0.05)
        row_r1 = await _target_row(page, 0)
        await row_r1.locator(
            ".actions button[title='resample — regenerate this response']"
        ).click(force=True)

        # Each ``resample`` dispatch holds ``_dispatch_lock`` while awaiting
        # ``_replayed`` — for a target-exclusive fork the child's live target
        # regenerate (``SLEEP_S``) runs *before* ``pre_turn`` sets
        # ``_replayed``, so budget ≈ 2×SLEEP_S + slack.
        await anyio.sleep(SLEEP_S * 2 + 2.0)
        errs = [(bid, br.error) for bid, br in session.branches.items() if br.error]
        assert not errs, f"branch errors: {errs}"
        banner = await page.locator(".error-banner").count()
        assert banner == 0, (
            f".error-banner: {await page.locator('.error-banner').text_content()!r}"
        )
        n_children = len(session.branches) - 1
        return (
            f"children={n_children} current={session.current!r} "
            f"statuses={ {bid: br.status for bid, br in session.branches.items()} }"
        )
    finally:
        for t in session.branch_tasks.values():
            t.cancel()
        await session.close()


# ── driver ──────────────────────────────────────────────────────────────────

SCENARIOS = [
    ("s1 double-pause", s1_double_pause),
    ("s2 pause→play→pause rapid", s2_pause_play_pause),
    ("s3 branch during generate", s3_branch_mid_generate),
    ("s4 double-branch same anchor", s4_double_branch),
    ("s5 inject→pause→inject→play", s5_inject_pause_inject_play),
    ("s6 resample ×2 anchors", s6_resample_two_anchors),
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
                log_cap.drain()  # clear inter-scenario noise
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
