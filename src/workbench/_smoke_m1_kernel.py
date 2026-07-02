"""Smoke test for the M1 orchestrator kernel spike (``workbench.m1.kernel``).

Exercises, against a real in-process ``InteractiveShell``:

- basic cell execution + last-expr → ``DisplayEvent`` via the displayhook
- explicit ``display()`` and ``_repr_mimebundle_`` dispatch (vendor MIME lands)
- stdout capture as stream events, correctly attributed under concurrency
- ``display_id`` + ``dh.update()`` → ``update=True`` on the same id
- ``_gate()`` end to end: pending card → ``resolve()`` → resolved card
- background cell: ``run_turn(background=True)`` returns immediately, cell
  keeps running, ``[done · bound: …]`` notification lands on completion
- foreground detach mid-await: ``detach()`` returns a partial result, cell
  finishes in the background
- two concurrent bg cells share ``user_ns`` and both bind
- error path: ``error_in_exec`` surfaces, traceback appended to ``text``

No network, no models. Run:  ``uv run python -m workbench._smoke_m1_kernel``
"""

from __future__ import annotations

import asyncio
import time

import anyio

from workbench.m1.kernel import (
    STREAM_MIME,
    WB_MIME,
    DisplayEvent,
    OrchestratorKernel,
)
from workbench.m1.wb import Workbench


async def _amain() -> None:
    wire: list[DisplayEvent] = []
    with OrchestratorKernel(on_display=wire.append) as k:
        await _run(k, wire)
    print("\n✓ all M1 kernel smoke checks passed")


async def _run(k: OrchestratorKernel, wire: list[DisplayEvent]) -> None:  # noqa: PLR0915
    k.shell.user_ns["wb"] = Workbench(k.gate, session=None)

    # ---- 1. last-expr auto-display via displayhook -------------------------
    r = await k.run_turn("x = 41\nx + 1")
    assert r.success and not r.detached
    assert r.new_names == ["x"], r.new_names
    # exactly one output: the last-expr bundle, text/plain "42"
    outs = [ev for ev in r.outputs if not ev.meta.get("sys")]
    assert len(outs) == 1, outs
    assert outs[0].bundle["text/plain"] == "42"
    assert "42" in r.text
    print("✓ last-expr → DisplayEvent")

    # ---- 2. explicit display() + _repr_mimebundle_ -------------------------
    k.shell.user_ns["Thing"] = _Thing
    r = await k.run_turn("t = Thing(7)\ndisplay(t)\nprint('after')\nt")
    kinds = [_kind(ev) for ev in r.outputs]
    # display(t) → wb-mime; print → ≥1 stream (text + '\n'); last-expr t → wb-mime
    assert kinds[0] == "thing" and kinds[-1] == "thing", kinds
    assert all(k_ == "stream" for k_ in kinds[1:-1]), kinds
    assert r.outputs[0].bundle[WB_MIME] == {"kind": "thing", "n": 7}
    assert "after" in "".join(ev.bundle[STREAM_MIME]["text"] for ev in r.outputs[1:-1])
    # emission order preserved in the model-facing text
    assert r.text.index("<Thing 7>") < r.text.index("after")
    print("✓ display() / _repr_mimebundle_ / stdout order")

    # ---- 3. display_id + update -------------------------------------------
    r = await k.run_turn(
        "dh = display(Thing(1), display_id='job-1')\n"
        "dh.update(Thing(2))\n"
        "dh.update(Thing(3))"
    )
    ids = [ev.id for ev in r.outputs]
    assert ids == ["job-1", "job-1", "job-1"], ids
    assert [ev.update for ev in r.outputs] == [False, True, True]
    assert [ev.bundle[WB_MIME]["n"] for ev in r.outputs] == [1, 2, 3]
    # model-facing text collapses updates: only the final <Thing 3> survives
    assert "<Thing 3>" in r.text and "<Thing 1>" not in r.text, r.text
    print("✓ display_id / dh.update() → update=True, model text collapses")

    # ---- 4. gate: display + await Future + update -------------------------
    turn = asyncio.create_task(
        k.run_turn("ans = await wb.ask_human('proceed?', ['y', 'n'])\nans")
    )
    # let the cell reach the await
    for _ in range(50):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    assert len(k.gate.pending) == 1, "gate future not registered"
    (pid,) = k.gate.pending
    pending_evs = [ev for ev in k.outputs[k._turn_counter] if ev.id == pid]
    assert len(pending_evs) == 1 and not pending_evs[0].update
    assert pending_evs[0].bundle[WB_MIME]["pending"] is True
    # WS handler resolves it
    assert k.gate.resolve(pid, "y")
    r = await turn
    assert r.success
    assert k.shell.user_ns["ans"] == "y"
    prompt_evs = [ev for ev in r.outputs if ev.id == pid]
    assert len(prompt_evs) == 2
    assert prompt_evs[1].update and prompt_evs[1].bundle[WB_MIME]["pending"] is False
    assert prompt_evs[1].bundle[WB_MIME]["answer"] == "y"
    assert not k.gate.pending, "gate future not cleaned up"
    print("✓ _gate: pending → resolve() → update(resolved)")

    # ---- 5. background cell + [done] notification -------------------------
    k.drain_notifications()
    r = await k.run_turn(
        "await asyncio.sleep(0.05)\nbgv = 'landed'\nprint('bg-print')\n'bg-result'",
        background=True,
    )
    tid = r.turn_id
    assert r.detached and r.text == f"<cell-{tid} backgrounded>"
    assert tid in k.bg
    await asyncio.wait_for(asyncio.gather(*k.bg.values()), timeout=1.0)
    assert k.shell.user_ns["bgv"] == "landed"
    notes = k.drain_notifications()
    assert any(f"cell-{tid} done" in n and "bgv" in n for n in notes), notes
    # outputs streamed to the right turn even though we never awaited it
    bg_outs = k.outputs[tid]
    assert any(_kind(ev) == "stream" for ev in bg_outs)
    assert any(ev.bundle.get("text/plain") == "'bg-result'" for ev in bg_outs)
    print("✓ background cell runs, notifies, outputs attributed")

    # ---- 6. foreground detach mid-run -------------------------------------
    turn = asyncio.create_task(
        k.run_turn("print('early')\nawait asyncio.sleep(0.1)\ndv = 99\ndv")
    )
    await asyncio.sleep(0.02)  # let 'early' print land
    k.detach()
    r = await turn
    assert r.detached
    assert "early" in r.text and "detached" in r.text
    assert "dv" not in k.shell.user_ns  # not bound yet
    await asyncio.wait_for(asyncio.gather(*k.bg.values()), timeout=1.0)
    assert k.shell.user_ns["dv"] == 99
    notes = k.drain_notifications()
    assert any("dv" in n for n in notes), notes
    print("✓ detach mid-run returns partial; cell completes in bg")

    # ---- 7. two concurrent bg cells, shared user_ns, print attribution ----
    t0 = time.monotonic()
    ra = await k.run_turn(
        "await asyncio.sleep(0.1)\nprint('from-a')\nca = 1", background=True
    )
    rb = await k.run_turn(
        "await asyncio.sleep(0.1)\nprint('from-b')\ncb = 2", background=True
    )
    await asyncio.wait_for(asyncio.gather(*k.bg.values()), timeout=1.0)
    dt = time.monotonic() - t0
    assert dt < 0.18, f"concurrent bg cells serialised? dt={dt:.3f}"
    assert k.shell.user_ns["ca"] == 1 and k.shell.user_ns["cb"] == 2
    a_streams = [ev for ev in k.outputs[ra.turn_id] if _kind(ev) == "stream"]
    b_streams = [ev for ev in k.outputs[rb.turn_id] if _kind(ev) == "stream"]
    assert any("from-a" in ev.text for ev in a_streams), a_streams
    assert any("from-b" in ev.text for ev in b_streams), b_streams
    assert not any("from-b" in ev.text for ev in a_streams), (
        "stdout mis-attributed across concurrent cells"
    )
    print(f"✓ concurrent bg cells overlap (dt={dt:.3f}s), prints attributed")

    # ---- 8. error path ----------------------------------------------------
    r = await k.run_turn("raise ValueError('nope')")
    assert not r.success
    assert isinstance(r.error, ValueError)
    assert "ValueError" in r.text and "nope" in r.text
    print("✓ error_in_exec surfaces in TurnResult")

    # ---- 9. multi-gate via gather (Scenario C t1) -------------------------
    turn = asyncio.create_task(
        k.run_turn(
            "a, b = await asyncio.gather("
            "wb.ask_human('one?'), wb.ask_human('two?'))\n(a, b)"
        )
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if len(k.gate.pending) == 2:
            break
    assert len(k.gate.pending) == 2, (
        f"both gates should publish before either blocks: {k.gate.pending}"
    )
    for pid in list(k.gate.pending):
        q = next(
            ev.bundle[WB_MIME]["question"]
            for ev in k.outputs[k._turn_counter]
            if ev.id == pid and not ev.update
        )
        k.gate.resolve(pid, q[:-1])  # answer with the question text sans '?'
    r = await turn
    assert r.success and k.shell.user_ns["a"] == "one" and k.shell.user_ns["b"] == "two"
    print("✓ asyncio.gather: both gates pending simultaneously, resolve independently")

    # ---- 10. shadow_warning + cancel --------------------------------------
    await k.run_turn("await asyncio.sleep(0.5)\nsh = 1", background=True)
    tid = k._turn_counter
    assert k.shadow_warning("sh = 2\nother = 3") == ["sh"]
    assert k.shadow_warning("unrelated = 1") == []
    assert k.cancel(tid)
    await asyncio.sleep(0.01)
    assert tid not in k.bg
    assert any("cancelled" in n for n in k.drain_notifications())
    print("✓ shadow_warning flags pending target; cancel() kills bg cell")

    # ---- 11. syntax error → error_before_exec -----------------------------
    r = await k.run_turn("def broken(:\n    pass")
    assert not r.success and isinstance(r.error, SyntaxError)
    print("✓ error_before_exec surfaces")

    # ---- 12. rich formatter dispatch (DataFrame _repr_html_) --------------
    try:
        import pandas as pd  # noqa: PLC0415

        k.shell.user_ns["pd"] = pd
        r = await k.run_turn("pd.DataFrame({'a': [1, 2]})")
        assert r.success
        (out,) = [ev for ev in r.outputs if "text/html" in ev.bundle]
        assert "<table" in out.bundle["text/html"]
        assert "text/plain" in out.bundle  # model still gets a text repr
        print("✓ DataFrame → text/html + text/plain via display_formatter")
    except ImportError:
        print("· pandas not installed, skipping DataFrame check")

    # ---- 13. resolve unknown id is a no-op --------------------------------
    assert k.gate.resolve("nope", "x") is False

    # ---- 14. quiet() race: later cell's ';' must not drop earlier's expr --
    ra = await k.run_turn("await asyncio.sleep(0.05)\n'survived'", background=True)
    await k.run_turn("await asyncio.sleep(0.01)\n99;", background=True)
    await asyncio.wait_for(asyncio.gather(*k.bg.values()), timeout=1.0)
    assert any(ev.text == "'survived'" for ev in k.outputs[ra.turn_id]), (
        "concurrent ';' cell suppressed another cell's last-expr"
    )
    print("✓ quiet() race disarmed")

    # ---- 15. input transforms: %magic works -------------------------------
    r = await k.run_turn("%time _v = sum(range(100))")
    assert r.success, r.error
    assert "Wall time" in r.text or "CPU times" in r.text, r.text
    print("✓ %magic via transform_cell")

    # ---- 16. Markdown → model sees the markdown, not the object repr ------
    r = await k.run_turn("display(Markdown('**rate:** 7/24 (29%)'))")
    assert "**rate:** 7/24 (29%)" in r.text, r.text
    assert "IPython.core.display.Markdown" not in r.text
    print("✓ display(Markdown) → text/markdown preferred")

    # ---- 17. single traceback; no ANSI; fg cell doesn't notify -----------
    k.drain_notifications()
    r = await k.run_turn("raise RuntimeError('once')")
    assert r.text.count("RuntimeError: once") == 1 and "\x1b[" not in r.text, r.text
    assert k.drain_notifications() == [], "fg cell enqueued a [done] chip"
    print("✓ traceback rendered once, no ANSI, fg cell silent")

    # ---- 18. assign-only cell → <ok · bound: …> ---------------------------
    r = await k.run_turn("cfg = {'n': 3}\nseeds = [1, 2, 3]")
    assert r.text == "<ok · bound: cfg, seeds>", r.text
    print("✓ assign-only cell reports bindings")

    # ---- 19. clear_output truncates model text ----------------------------
    r = await k.run_turn(
        "print('gone')\n"
        "from IPython.display import clear_output\n"
        "clear_output()\n"
        "print('kept')"
    )
    assert "gone" not in r.text and "kept" in r.text, r.text
    print("✓ clear_output honoured in model-facing render")

    # ---- 20. interrupt (M1-FEATURES §11) ----------------------------------
    turn = asyncio.create_task(k.run_turn("print('partial')\nawait asyncio.sleep(10)"))
    await asyncio.sleep(0.05)
    tid = k._turn_counter
    assert k.interrupt(tid)
    r = await turn
    assert not r.success and r.error is None, (r.success, r.error)
    assert "[interrupted by user after " in r.text and "partial" in r.text, r.text
    assert not any(
        ev.bundle.get(WB_MIME, {}).get("kind") == "traceback" for ev in r.outputs
    ), "interrupt emitted a traceback card"
    assert r.duration >= 0.04, r.duration
    # cell_done went to on_display (not r.outputs) with interrupted=True
    done_ev = next(
        ev
        for ev in reversed(wire)
        if ev.bundle.get(WB_MIME, {}).get("kind") == "cell_done"
    )
    assert done_ev.bundle[WB_MIME]["interrupted"] is True
    assert done_ev.bundle[WB_MIME]["turn"] == tid
    print("✓ interrupt: [interrupted by user …] + partial, no traceback, cell_done")

    # ---- 21. cell_done + ns_summary (M1-FEATURES §5/§7) --------------------
    r = await k.run_turn("summary_var = [1, 2, 3]")
    done_ev = next(
        ev
        for ev in reversed(wire)
        if ev.bundle.get(WB_MIME, {}).get("kind") == "cell_done"
    )
    cd = done_ev.bundle[WB_MIME]
    assert cd["turn"] == r.turn_id and cd["duration"] == r.duration
    assert cd["new_names"] == ["summary_var"]
    assert cd["ns"]["summary_var"] == "list · len 3", cd["ns"]["summary_var"]
    assert "KERNEL" not in cd["ns"] and "asyncio" not in cd["ns"]
    print("✓ cell_done: duration + new_names + ns_summary (seeded names excluded)")

    # ---- 22. on_display forwarded everything ------------------------------
    assert len(wire) >= sum(len(v) for v in k.outputs.values())
    assert all(ev.turn_id != -1 for ev in wire if not ev.meta.get("sys")), (
        "some in-cell output emitted with turn_id=-1"
    )
    print(f"✓ on_display saw {len(wire)} events across {len(k.outputs)} turns")


class _Thing:
    def __init__(self, n: int) -> None:
        self.n = n

    def _repr_mimebundle_(self, include=None, exclude=None):  # noqa: ANN001, ANN202
        return {
            "text/plain": f"<Thing {self.n}>",
            WB_MIME: {"kind": "thing", "n": self.n},
        }


def _kind(ev: DisplayEvent) -> str:
    if STREAM_MIME in ev.bundle:
        return "stream"
    if WB_MIME in ev.bundle:
        return ev.bundle[WB_MIME]["kind"]
    return "plain"


if __name__ == "__main__":
    anyio.run(_amain)
