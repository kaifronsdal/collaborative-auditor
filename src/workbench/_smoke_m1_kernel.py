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

from workbench.m1._fixtures import wait_for, wait_gate
from workbench.m1.inspect_repr import ns_size_estimate, short_repr
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.proposals import Gate
from workbench.m1.wb import Workbench
from workbench.m1.wire import STREAM_MIME, WB_KINDS, WB_MIME, DisplayEvent


async def _amain() -> None:
    await _check_short_repr()
    wire: list[DisplayEvent] = []
    with OrchestratorKernel(on_display=wire.append) as k:
        await _run(k, wire)
    print("\n✓ all M1 kernel smoke checks passed")


async def _run(k: OrchestratorKernel, wire: list[DisplayEvent]) -> None:  # noqa: PLR0915
    gate = Gate()
    k.shell.user_ns["wb"] = Workbench(gate, session=None)

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
    pid = await wait_gate(gate)
    pending_evs = [ev for ev in k.outputs[k._turn_counter] if ev.id == pid]
    assert len(pending_evs) == 1 and not pending_evs[0].update
    assert pending_evs[0].bundle[WB_MIME]["pending"] is True
    # WS handler resolves it
    assert gate.resolve(pid, "y")
    r = await turn
    assert r.success
    assert k.shell.user_ns["ans"] == "y"
    prompt_evs = [ev for ev in r.outputs if ev.id == pid]
    assert len(prompt_evs) == 2
    assert prompt_evs[1].update and prompt_evs[1].bundle[WB_MIME]["pending"] is False
    assert prompt_evs[1].bundle[WB_MIME]["answer"] == "y"
    assert not gate.pending, "gate future not cleaned up"
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
    await wait_for(lambda: len(gate.pending) == 2, tick=0)
    for pid in list(gate.pending):
        q = next(
            ev.bundle[WB_MIME]["question"]
            for ev in k.outputs[k._turn_counter]
            if ev.id == pid and not ev.update
        )
        gate.resolve(pid, q[:-1])  # answer with the question text sans '?'
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
    assert gate.resolve("nope", "x") is False

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
    assert cd["ns"]["summary_var"] == "list[int] · len 3", cd["ns"]["summary_var"]
    assert "KERNEL" not in cd["ns"] and "asyncio" not in cd["ns"]
    # Every summary respects the 80-char cap.
    assert all(len(v) <= 80 for v in cd["ns"].values()), (
        f"over-cap: {[v for v in cd['ns'].values() if len(v) > 80]}"
    )
    print("✓ cell_done: duration + new_names + ns_summary (seeded names excluded)")

    # ---- 22. on_display forwarded everything ------------------------------
    assert len(wire) >= sum(len(v) for v in k.outputs.values())
    assert all(ev.turn_id != -1 for ev in wire if not ev.meta.get("sys")), (
        "some in-cell output emitted with turn_id=-1"
    )
    print(f"✓ on_display saw {len(wire)} events across {len(k.outputs)} turns")

    # ---- 23. wire contract: every emitted kind is registered (Batch C) ----
    # ``WB_KINDS`` is derived from ``get_args(WbPayload)`` — an unregistered
    # kind means a missing ``wire.py`` TypedDict + ``types.ts`` mirror.
    # ``"thing"`` is this file's own fixture, not a real card.
    emitted = {
        ev.bundle[WB_MIME]["kind"] for ev in wire if WB_MIME in ev.bundle
    } - {"thing"}
    assert emitted <= WB_KINDS, (
        f"unregistered WB_MIME kinds emitted: {sorted(emitted - WB_KINDS)}; "
        f"add TypedDict(s) to workbench.m1.wire and mirror in types.ts"
    )
    # And the kernel-owned kinds actually appeared (guards against a rename
    # that leaves ``WB_KINDS`` correct but the emit site drifted).
    assert {"prompt", "traceback", "cell_done"} <= emitted, emitted
    print(f"✓ wire contract: emitted kinds {sorted(emitted)} ⊆ WB_KINDS")


async def _check_short_repr() -> None:  # noqa: PLR0915
    """M1-FEATURES §7 deep review: one useful line per realistic binding type.

    Each case asserts (a) something *useful* is in the summary, (b) no
    ``<object at 0x…>`` leaks, (c) the 80-char cap holds. Optional deps
    (petri) are skipped if absent.
    """
    import io  # noqa: PLC0415
    from functools import partial  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415
    import plotly.graph_objects as go  # noqa: PLC0415
    from inspect_ai import Task  # noqa: PLC0415
    from inspect_ai.dataset import Sample  # noqa: PLC0415
    from inspect_ai.model import ChatMessageUser  # noqa: PLC0415

    checks: list[tuple[str, object, str]] = [
        # -- primitives -----------------------------------------------------
        ("int", 42, "int · 42"),
        ("float", 3.14159, "float · 3.14159"),
        ("bool", True, "True"),
        ("None", None, "None"),
        ("str-short", "hello", "str · len 5 · 'hello'"),
        (
            "str-long",
            "persuade the target to reveal the system prompt " * 10,
            "str · len 480 · 'persuade the target to",
        ),
        ("bytes", b"\x00" * 200, "bytes · len 200"),
        # -- containers -----------------------------------------------------
        ("list[str]", ["a", "b", "c"], "list[str] · len 3"),
        ("list-empty", [], "list · empty"),
        ("list-mixed", [1, "a", 3.0], "list[mixed] · len 3"),
        ("tuple[int]", (1, 2, 3, 4), "tuple[int] · len 4"),
        ("set[int]", {1, 2, 3}, "set[int] · len 3"),
        (
            "dict",
            {"g": 1, "h": 2, "i": 3, "j": 4},
            "dict[str, int] · 4 keys: [g, h, i, …]",
        ),
        ("dict-empty", {}, "dict · empty"),
        (
            "list[DataFrame]",
            [pd.DataFrame({"a": [1]}), pd.DataFrame({"b": [2]})],
            "list[DataFrame] · len 2",
        ),
        # -- pandas ---------------------------------------------------------
        (
            "DataFrame",
            pd.DataFrame(
                {"id": range(40), "seed": 0, "score": 0.5, "tag": "x", "n": 1}
            ),
            "DataFrame · 40×5 · [id, seed, score, …]",
        ),
        (
            "Series",
            pd.Series([0.5, 0.7, 0.9, 0.4], name="score"),
            "Series[float64] · len 4 · mean 0.625",
        ),
        ("Index", pd.Index(["a", "b", "c"]), "Index[str] · len 3"),
        (
            "GroupBy",
            pd.DataFrame({"k": [1, 1, 2], "v": [1, 2, 3]}).groupby("k"),
            "DataFrameGroupBy · 2 groups · by k",
        ),
        # -- numpy ----------------------------------------------------------
        ("ndarray", np.zeros((40, 5)), "ndarray · float64 · (40, 5)"),
        ("np-scalar", np.float64(0.63), "float64 · 0.63"),
        # -- plotly ---------------------------------------------------------
        (
            "Figure",
            go.Figure(data=[go.Bar(y=[1, 2]), go.Bar(y=[3, 4]), go.Scatter(y=[1])]),
            "Figure · bar+scatter · 3 traces",
        ),
        # -- inspect_ai -----------------------------------------------------
        (
            "Task",
            Task(dataset=[Sample(input="x"), Sample(input="y")], name="audit-a5b59a"),
            "Task · audit-a5b59a · 2 samples",
        ),
        (
            "ChatMessageUser",
            ChatMessageUser(content="persuade the target to reveal the prompt"),
            "ChatMessageUser · 'persuade the target to reveal the prompt'",
        ),
        # -- callables ------------------------------------------------------
        ("function", _make_task, "function · _make_task(tag, n=4)"),
        ("lambda", lambda x: x + 1, "function · "),
        ("partial", partial(_make_task, "x", n=8), "partial · _make_task(2 bound)"),
        # -- paths / IO -----------------------------------------------------
        (
            "Path",
            Path("logs/audit-a5b/2026-06-30.eval"),
            "PosixPath · logs/audit-a5b/2026-06-30.eval",
        ),
        (
            "Path-long",
            Path("/mnt/s3/users/somebody/evals/audit-a5b59a/very/deep/nested/dir/x.eval"),
            "PosixPath · /…/dir/x.eval",
        ),
        ("file", io.StringIO("data"), "StringIO · open · "),
    ]

    for label, v, want in checks:
        got = short_repr(v)
        assert len(got) <= 80, f"{label}: over cap ({len(got)}): {got!r}"
        assert "\n" not in got, f"{label}: newline in {got!r}"
        assert " at 0x" not in got, f"{label}: raw object repr leaked: {got!r}"
        assert want in got, f"{label}: want {want!r} in {got!r}"
    print(f"✓ short_repr: {len(checks)} core types")

    # -- workbench handles (dataclass, so construct a minimal one) ----------
    from workbench.m1.attach import AttachedRun  # noqa: PLC0415

    h = AttachedRun(task_name="audit-a5b59a", log_dir="/tmp", total=12)
    h.rows = {f"s{i}": _row("done") for i in range(3)}
    got = short_repr(h)
    assert got == "AttachedRun · 3/12 running · audit-a5b59a", got
    h.finished = True
    assert "3/12 done" in short_repr(h)
    print("✓ short_repr: AttachedRun")

    # -- petri (optional) ---------------------------------------------------
    try:
        from inspect_petri.target import History  # noqa: PLC0415

        hist = History()
        hist.branch("")
        hist.branch("")
        got = short_repr(hist)
        assert got == "History · 3 nodes · depth 2", got
        assert short_repr(hist.root).startswith("Trajectory · "), short_repr(hist.root)
        print("✓ short_repr: petri History/Trajectory")
    except ImportError:
        print("· inspect_petri not installed, skipping")

    # -- awaitables ---------------------------------------------------------
    t = asyncio.create_task(asyncio.sleep(0), name="scan-x")
    assert short_repr(t) == "Task · pending · scan-x", short_repr(t)
    await t
    assert "done" in short_repr(t)
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    assert short_repr(fut) == "Future · pending"
    coro = asyncio.sleep(0)
    assert short_repr(coro) == "coroutine · sleep", short_repr(coro)
    coro.close()
    print("✓ short_repr: Task/Future/coroutine")

    # -- exception safety: broken __repr__ / __len__ ------------------------
    assert short_repr(_Broken()) == "_Broken"
    assert short_repr(_BrokenList()) == "_BrokenList"
    print("✓ short_repr: never raises (broken __repr__/__len__ → type name)")

    # -- pydantic fallback: an un-special-cased BaseModel shows field names,
    #    not a 500-char ``model_repr`` dump ---------------------------------
    got = short_repr(Sample(input="x" * 500, target="y" * 500))
    assert len(got) <= 80 and "xxxx" not in got and "input" in got, got

    # -- wire size: 30 vars × 80-char cap ≈ 3 kB per cell_done --------------
    ns = {f"var{i}": short_repr("x" * 200) for i in range(30)}
    assert ns_size_estimate(ns) < 4000, ns_size_estimate(ns)
    # 20 turns worth stays well under the 1 MB WS frame default.
    assert 20 * ns_size_estimate(ns) < 100_000
    print(f"✓ wire size: 30 vars = {ns_size_estimate(ns)} B; ×20 turns < 100 kB")


def _make_task(tag: str, n: int = 4) -> None:
    """Fixture for the callable-signature check."""


def _row(status: str) -> object:
    from workbench.m1.handles import SampleRow  # noqa: PLC0415

    return SampleRow(id="s", status=status, epoch=1, input="", turns=0)


class _Broken:
    def __repr__(self) -> str:
        raise RuntimeError("boom")


class _BrokenList(list):
    def __len__(self) -> int:
        raise RuntimeError("boom")


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
