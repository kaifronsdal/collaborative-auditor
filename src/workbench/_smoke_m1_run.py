"""M1.2 smoke: ``wb.run_eval`` / ``wb.run_audits`` / ``steer`` / ``stop`` in-cell.

Exercises the shared ``RunHandle.launch`` path against mockllm inside a real
``OrchestratorKernel`` cell:

- ``run_eval(task)`` → ``RunHandle`` displays, ``.eval`` written, ``_watch``
  ticks ``dh.update`` to final counters, model text collapses.
- ``steer`` / ``stop`` via ``CONTROL`` reach a cooperating solver.
- Two concurrent ``run_eval`` in one cell (guard lifted @ inspect fa94cd82).
- ``cancel()`` mid-run → handle reports ``cancelled``.

``run_audits`` with the real petri task needs role models mockllm can't
satisfy; that path is exercised only up to task construction here — the
launcher is the same ``RunHandle.launch``, so ``run_eval`` covers the
mechanics.

Run:  ``uv run python -m workbench._smoke_m1_run``
"""

from __future__ import annotations

import asyncio

import anyio
from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, TaskState, solver

from workbench.m1.kernel import STREAM_MIME, WB_MIME, OrchestratorKernel
from workbench.m1.orchestrator import _prewarm  # noqa: PLC2701
from workbench.m1.run import CONTROL, RunHandle, drain_control
from workbench.m1.wb import Workbench


@solver
def cooperating():
    """A 3-turn solver that drains ``CONTROL`` before each generate."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        for turn in range(3):
            await asyncio.sleep(0.08)
            injected, stop_now = drain_control(state.sample_id)
            state.messages.extend(injected)
            if stop_now:
                state.metadata["stopped_at"] = turn
                return state
            state = await generate(state)
        return state

    return solve


def make_task(tag: str, n: int = 4) -> Task:
    return Task(
        dataset=[Sample(input=f"{tag}-{i}", id=f"{tag}-{i}") for i in range(n)],
        solver=cooperating(),
        name=f"t-{tag}",
    )


async def _amain() -> None:
    _prewarm()
    with OrchestratorKernel() as k:
        await _run(k)
    print("\n✓ all M1.2 run smoke checks passed")


async def _run(k: OrchestratorKernel) -> None:  # noqa: PLR0915
    k.shell.user_ns["wb"] = Workbench(k.gate, session=None)
    k.shell.user_ns.update(make_task=make_task, RunHandle=RunHandle)

    # ---- 1. run_eval in-cell: card ticks, .eval written, text collapses ----
    r = await k.run_turn(
        "h = wb.run_eval(make_task('a'), model='mockllm/model')\n"
        "await h.wait()\n"
        "h"
    )
    assert r.success, r.error
    h = k.shell.user_ns["h"]
    assert isinstance(h, RunHandle)
    assert h.finished and h.n_done == 4 and h.total == 4, (h.finished, h.n_done, h.total)
    assert h.location and h.location.endswith(".eval")
    # dh.update fired ≥2× (initial + ≥1 tick + final); model text = final line
    stable_evs = [ev for ev in r.outputs if ev.stable and ev.id == h.id]
    assert len(stable_evs) >= 2 and stable_evs[-1].update
    assert "4/4" in r.text and "0/4" not in r.text, r.text
    # no inspect progress spew leaked into stream events
    streams = [ev for ev in r.outputs if STREAM_MIME in ev.bundle]
    assert not streams, (
        f"prewarm() didn't suppress display: {[e.text for e in streams]}"
    )
    print(
        f"✓ run_eval: {h.n_done}/{h.total}, {len(stable_evs)} ticks, .eval @ {h.location.split('/')[-1][:30]}"
    )

    # ---- 2. steer + stop via CONTROL reach the solver ---------------------
    r = await k.run_turn(
        "h2 = wb.run_eval(make_task('b'), model='mockllm/model')\n"
        "await asyncio.sleep(0.05)\n"
        "wb.steer(['b-1'], 'STEERED')\n"
        "wb.stop(['b-2'])\n"
        # running_ids depends on the watcher's _poll (~0.25s tick); poll it
        # separately so the steer above still lands before b-1's 3rd turn.
        "seen_running = []\n"
        "for _ in range(20):\n"
        "    if h2.running_ids:\n"
        "        seen_running = list(h2.running_ids)\n"
        "        break\n"
        "    await asyncio.sleep(0.05)\n"
        "logs = await h2._task\n"
        "smp = {s.id: s for s in logs[0].samples}\n"
        "(any('STEERED' in str(m.content) for m in smp['b-1'].messages),\n"
        " smp['b-2'].metadata.get('stopped_at') is not None,\n"
        " len(smp['b-0'].messages))"
    )
    assert r.success, r.text
    # running_ids populated while samples were in flight (M1-E2E-FINDINGS §1)
    seen_running = k.shell.user_ns["seen_running"]
    assert seen_running, "h2.running_ids never populated during run"
    assert set(seen_running) <= {"b-0", "b-1", "b-2", "b-3"}, seen_running
    # ``_`` is disabled under concurrent cells; read from user_ns directly.
    steered_ok = any(
        "STEERED" in str(m.content) for m in k.shell.user_ns["smp"]["b-1"].messages
    )
    stopped_ok = k.shell.user_ns["smp"]["b-2"].metadata.get("stopped_at") is not None
    assert steered_ok, "steer message not in b-1's conversation"
    assert stopped_ok, "stop flag not honoured by b-2"
    assert len(k.shell.user_ns["smp"]["b-0"].messages) >= 4, "control sample ran short"
    # wb.steer emits a visible receipt regardless of match (M1-E2E-FINDINGS §3)
    assert "→ steered" in r.text, f"no steer receipt in cell output: {r.text!r}"
    CONTROL.clear()
    print(
        f"✓ steer/stop via CONTROL reach cooperating solver "
        f"(running_ids={len(seen_running)}, receipt shown)"
    )

    # ---- 3. two concurrent run_eval (guard lifted) ------------------------
    r = await k.run_turn(
        "hx = wb.run_eval(make_task('x', 3), model='mockllm/model')\n"
        "hy = wb.run_eval(make_task('y', 3), model='mockllm/model')\n"
        "await asyncio.gather(hx.wait(), hy.wait())\n"
        "(hx.n_done, hy.n_done)"
    )
    assert r.success, r.text
    hx, hy = k.shell.user_ns["hx"], k.shell.user_ns["hy"]
    assert hx.n_done == 3 and hy.n_done == 3, (hx.n_done, hy.n_done)
    assert hx.log_dir != hy.log_dir
    # both cards ticked independently under the same turn
    assert any(ev.id == hx.id and ev.update for ev in r.outputs)
    assert any(ev.id == hy.id and ev.update for ev in r.outputs)
    print(f"✓ concurrent run_eval: hx {hx.n_done}/3, hy {hy.n_done}/3, distinct log_dirs")

    # ---- 4. cancel mid-run ------------------------------------------------
    r = await k.run_turn(
        "hc = wb.run_eval(make_task('c', 6), model='mockllm/model')\n"
        "await asyncio.sleep(0.1)\n"
        "hc.cancel()\n"
        "try:\n"
        "    await hc.wait()\n"
        "except asyncio.CancelledError:\n"
        "    pass\n"
        "await asyncio.sleep(0.3)  # let _watch settle\n"
        "hc"
    )
    assert r.success, r.text
    hc = k.shell.user_ns["hc"]
    assert hc.finished and hc.error == "cancelled", (hc.finished, hc.error)
    assert "cancelled" in r.text
    print(
        f"✓ cancel: handle finished with error='cancelled', {hc.n_done}/{hc.total} landed"
    )

    # ---- 5. RunProposal gate → deny returns settled handle ----------------
    turn = asyncio.create_task(
        k.run_turn(
            "await wb.run_audits(['s']*12, {}, description='big', model='mockllm/model')"
        )
    )
    for _ in range(200):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    assert k.gate.pending, "run_audits didn't gate at n=12"
    (pid,) = k.gate.pending
    prop_ev = next(ev for ev in k.outputs[k._turn_counter] if ev.id == pid)
    assert prop_ev.bundle[WB_MIME]["kind"] == "run_proposal"
    assert prop_ev.bundle[WB_MIME]["n"] == 12
    k.gate.resolve(pid, {"denied": True, "reason": "too many"})
    r = await turn
    assert r.success, r.text  # deny is control flow, not an exception
    assert "denied: too many" in r.text, r.text
    print("✓ run_audits gates at n>8; deny → settled handle (no traceback)")

    # ---- 6. wb.scan over a RunHandle's logs (scout grep_scanner) ----------
    try:
        from inspect_scout import grep_scanner  # noqa: PLC0415, F401
    except ImportError:
        print("- wb.scan: inspect_scout not installed, skipping")
    else:
        k.shell.user_ns["grep_scanner"] = grep_scanner
        r = await k.run_turn(
            "sh = await wb.scan(h, {'g': grep_scanner('a-')})\n"
            "await sh.wait()\n"
            "sh"
        )
        assert r.success, r.text
        sh = k.shell.user_ns["sh"]
        assert sh.finished and sh.error is None, (sh.finished, sh.error)
        assert sh.location and sh.location.startswith(sh.scans_dir)
        assert sh.per_scanner["g"]["scans"] == 4, sh.per_scanner
        assert sh.n_done == sh.total == 4
        assert "4/4" in r.text and "done" in r.text, r.text
        # .df is the ScanResultsDF.scanners mapping — one frame per scanner
        df = sh.df["g"]
        assert len(df) == 4, len(df)
        # card ticked at least once (initial + final)
        scan_evs = [ev for ev in r.outputs if ev.stable and ev.id == sh.id]
        assert len(scan_evs) >= 2 and scan_evs[-1].update
        assert scan_evs[-1].bundle[WB_MIME]["kind"] == "scan"
        # scout progress didn't leak into stream events
        assert not [ev for ev in r.outputs if STREAM_MIME in ev.bundle]
        print(
            f"✓ wb.scan: {sh.n_done}/{sh.total} via grep_scanner, "
            f"df['g'] {len(df)} rows, {len(scan_evs)} ticks"
        )


if __name__ == "__main__":
    anyio.run(_amain)
