"""M1.2 smoke: ``wb.run_eval`` / ``wb.run_audits`` / ``steer`` / ``stop`` in-cell.

Exercises the shared ``_launch`` path against mockllm inside a real
``OrchestratorKernel`` cell:

- ``run_eval(task)`` → ``RunHandle`` displays, ``.eval`` written, ``_watch``
  ticks ``dh.update`` to final counters, model text collapses.
- ``steer`` / ``stop`` via ``CONTROL`` reach a cooperating solver.
- Two concurrent ``run_eval`` in one cell (guard lifted @ inspect fa94cd82).
- ``cancel()`` mid-run → handle reports ``cancelled``.

``run_audits`` with the real petri task needs role models mockllm can't
satisfy; that path is exercised only up to task construction here — the
launcher is the same ``_launch``, so ``run_eval`` covers the mechanics.

Run:  ``uv run python -m workbench._smoke_m1_run``
"""

from __future__ import annotations

import asyncio

import anyio
from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, TaskState, solver

from workbench.m1.kernel import STREAM_MIME, WB_MIME, OrchestratorKernel
from workbench.m1.run import CONTROL, RunHandle, drain_control, prewarm
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


async def _amain() -> None:  # noqa: PLR0915
    prewarm()
    k = OrchestratorKernel()
    k.shell.user_ns["wb"] = Workbench(k, session=None)
    k.shell.user_ns.update(make_task=make_task, RunHandle=RunHandle)

    # ---- 1. run_eval in-cell: card ticks, .eval written, text collapses ----
    r = await k.run_turn(
        "h = await wb.run_eval(make_task('a'), model='mockllm/model')\n"
        "await h.wait()\n"
        "h"
    )
    assert r.success, r.error
    h = k.shell.user_ns["h"]
    assert isinstance(h, RunHandle)
    assert h.finished and h.done == 4 and h.total == 4, (h.finished, h.done, h.total)
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
        f"✓ run_eval: {h.done}/{h.total}, {len(stable_evs)} ticks, .eval @ {h.location.split('/')[-1][:30]}"
    )

    # ---- 2. steer + stop via CONTROL reach the solver ---------------------
    r = await k.run_turn(
        "h2 = await wb.run_eval(make_task('b'), model='mockllm/model')\n"
        "await asyncio.sleep(0.05)\n"
        "wb.steer(['b-1'], 'STEERED')\n"
        "wb.stop(['b-2'])\n"
        "logs = await h2._task\n"
        "smp = {s.id: s for s in logs[0].samples}\n"
        "(any('STEERED' in str(m.content) for m in smp['b-1'].messages),\n"
        " smp['b-2'].metadata.get('stopped_at') is not None,\n"
        " len(smp['b-0'].messages))"
    )
    assert r.success, r.text
    # ``_`` is disabled under concurrent cells; read from user_ns directly.
    steered_ok = any(
        "STEERED" in str(m.content) for m in k.shell.user_ns["smp"]["b-1"].messages
    )
    stopped_ok = k.shell.user_ns["smp"]["b-2"].metadata.get("stopped_at") is not None
    assert steered_ok, "steer message not in b-1's conversation"
    assert stopped_ok, "stop flag not honoured by b-2"
    assert len(k.shell.user_ns["smp"]["b-0"].messages) >= 4, "control sample ran short"
    CONTROL.clear()
    print("✓ steer/stop via CONTROL reach cooperating solver")

    # ---- 3. two concurrent run_eval (guard lifted) ------------------------
    r = await k.run_turn(
        "hx, hy = await asyncio.gather(\n"
        "    wb.run_eval(make_task('x', 3), model='mockllm/model'),\n"
        "    wb.run_eval(make_task('y', 3), model='mockllm/model'),\n"
        ")\n"
        "await asyncio.gather(hx.wait(), hy.wait())\n"
        "(hx.done, hy.done)"
    )
    assert r.success, r.text
    hx, hy = k.shell.user_ns["hx"], k.shell.user_ns["hy"]
    assert hx.done == 3 and hy.done == 3, (hx.done, hy.done)
    assert hx.log_dir != hy.log_dir
    # both cards ticked independently under the same turn
    assert any(ev.id == hx.id and ev.update for ev in r.outputs)
    assert any(ev.id == hy.id and ev.update for ev in r.outputs)
    print(f"✓ concurrent run_eval: hx {hx.done}/3, hy {hy.done}/3, distinct log_dirs")

    # ---- 4. cancel mid-run ------------------------------------------------
    r = await k.run_turn(
        "hc = await wb.run_eval(make_task('c', 6), model='mockllm/model')\n"
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
        f"✓ cancel: handle finished with error='cancelled', {hc.done}/{hc.total} landed"
    )

    # ---- 5. RunProposal gate → deny raises Denied -------------------------
    turn = asyncio.create_task(
        k.run_turn(
            "await wb.run_audits(['s']*12, {}, description='big', model='mockllm/model')"
        )
    )
    for _ in range(200):
        await asyncio.sleep(0)
        if k.pending:
            break
    assert k.pending, "run_audits didn't gate at n=12"
    (pid,) = k.pending
    prop_ev = next(ev for ev in k.outputs[k._turn_counter] if ev.id == pid)
    assert prop_ev.bundle[WB_MIME]["kind"] == "run_proposal"
    assert prop_ev.bundle[WB_MIME]["n"] == 12
    k.resolve(pid, {"denied": True, "reason": "too many"})
    r = await turn
    assert not r.success and "Denied" in r.text, r.text
    print("✓ run_audits gates at n>8; deny → Denied raised")

    k.restore_streams()
    print("\n✓ all M1.2 run smoke checks passed")


if __name__ == "__main__":
    anyio.run(_amain)
