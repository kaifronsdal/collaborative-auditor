"""M1-HYBRID smoke — step 3: ``wb.attach(log_dir)`` on a subprocess eval.

Exercises the read-only ``AttachedRun`` half of ``RunHandle``:

- subprocess-launch ``inspect eval`` (mockllm, 4 slow samples) to a fresh
  ``log_dir``; inside a kernel cell ``h = wb.attach(log_dir); await h.wait()``.
  Assert ``h.finished``, ``h.n_done == 4``, card ticked ≥2×, and the WB_MIME
  payload is the same ``kind:"eval_run"`` shape ``ProgressCard`` expects.
- attach to an *already-completed* ``log_dir`` (no ctl server) →
  ``running`` empty, ``done`` populated, ``finished`` immediately.

Steps 1/2/4+ (``wb_display``, ``bash`` tool, frontend) land here as they're
implemented. Run:  ``uv run python -m workbench._smoke_m1_hybrid``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap

import anyio

from workbench.m1.attach import AttachedRun
from workbench.m1.kernel import WB_MIME, OrchestratorKernel
from workbench.m1.orchestrator import _prewarm  # noqa: PLC2701
from workbench.m1.wb import Workbench

N = 4

#: A tiny mockllm task with a per-sample sleep so ``AttachedRun._poll`` sees
#: at least one tick with ``status == "started"`` before the log settles.
TASK_SRC = textwrap.dedent(
    """
    import anyio
    from inspect_ai import Task, task
    from inspect_ai.dataset import Sample
    from inspect_ai.solver import Generate, TaskState, solver


    @solver
    def slow():
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            for _ in range(2):
                await anyio.sleep(0.4)
                state = await generate(state)
            return state
        return solve


    @task
    def smoke(n: int = 4):
        return Task(
            dataset=[Sample(input=f"s{i}", id=f"s{i}") for i in range(n)],
            solver=slow(),
            name="smoke-hybrid",
        )
    """
)


async def _amain() -> None:
    _prewarm()
    with OrchestratorKernel() as k:
        ctl_found = await _run(k)
    print(
        f"\n✓ all M1-HYBRID attach smoke checks passed "
        f"(ctl discovery: {'found subprocess' if ctl_found else 'log-only'})"
    )


async def _run(k: OrchestratorKernel) -> bool:  # noqa: PLR0915
    k.shell.user_ns["wb"] = Workbench(k.gate, session=None)

    task_file = os.path.join(tempfile.gettempdir(), "wb_smoke_hybrid_task.py")
    with open(task_file, "w") as f:
        f.write(TASK_SRC)

    log_dir = "/tmp/wb-attach-test"
    shutil.rmtree(log_dir, ignore_errors=True)

    # ---- 1. subprocess `inspect eval` + wb.attach in a kernel cell --------
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "inspect_ai",
        "eval",
        f"{task_file}@smoke",
        "-T",
        f"n={N}",
        "--model",
        "mockllm/model",
        "--log-dir",
        log_dir,
        "--log-buffer",
        "1",
        "--display",
        "none",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    # Give the subprocess a moment to write its ctl discovery file + first
    # log flush before we attach (otherwise the first poll finds nothing).
    await asyncio.sleep(0.5)

    k.shell.user_ns["_LOG_DIR"] = log_dir
    r = await k.run_turn(
        "h = wb.attach(_LOG_DIR)\n"
        "await h.wait()\n"
        "h"
    )
    stderr = (await proc.stderr.read()).decode() if proc.stderr else ""
    rc = await proc.wait()
    assert rc == 0, f"subprocess eval failed (rc={rc}):\n{stderr}"
    assert r.success, r.text

    h = k.shell.user_ns["h"]
    assert isinstance(h, AttachedRun)
    assert h.finished and h.n_done == N and h.total == N, (
        h.finished, h.n_done, h.total, h._status,
    )
    assert h.location and h.location.endswith(".eval")
    assert h.error is None, h.error
    assert h._running == [] and h.running_ids == []
    # dh.update fired ≥2× (initial + ≥1 tick + final)
    stable_evs = [ev for ev in r.outputs if ev.stable and ev.id == h.id]
    assert len(stable_evs) >= 2 and stable_evs[-1].update, (
        f"card only ticked {len(stable_evs)}×"
    )
    # same WB_MIME shape as RunHandle → ProgressCard renders unchanged
    final_wb = stable_evs[-1].bundle[WB_MIME]
    assert final_wb["kind"] == "eval_run"
    assert final_wb["done"] == N and final_wb["total"] == N
    assert final_wb["log_dir"] == log_dir and final_wb["log"] == h.location
    assert len(final_wb["rows"]["done"]) == N and final_wb["rows"]["running"] == []
    assert final_wb["scores"] == [None] * N
    assert f"{N}/{N}" in r.text, r.text
    ctl_found = h._ctl not in (None, False)
    print(
        f"✓ wb.attach: {h.n_done}/{h.total}, {len(stable_evs)} ticks, "
        f".eval @ {h.location.split('/')[-1][:30]}, ctl={'yes' if ctl_found else 'no'}"
    )

    # ---- 2. attach to a *completed* log_dir (no ctl server) ---------------
    r = await k.run_turn(
        "h2 = wb.attach(_LOG_DIR)\n"
        "await h2.wait()\n"
        "h2"
    )
    assert r.success, r.text
    h2 = k.shell.user_ns["h2"]
    assert h2.finished and h2.n_done == N and h2.total == N, (
        h2.finished, h2.n_done, h2.total,
    )
    assert h2._ctl is False, f"ctl should be absent post-completion, got {h2._ctl!r}"
    assert h2._running == []
    stable_evs2 = [ev for ev in r.outputs if ev.stable and ev.id == h2.id]
    assert len(stable_evs2) >= 2, f"card only ticked {len(stable_evs2)}×"
    wb2 = stable_evs2[-1].bundle[WB_MIME]
    assert wb2["rows"]["running"] == [] and len(wb2["rows"]["done"]) == N
    assert wb2["finished"] is True
    print(
        f"✓ wb.attach on completed log_dir: log-only mode, "
        f"{h2.n_done}/{h2.total}, {len(stable_evs2)} ticks"
    )

    return ctl_found


if __name__ == "__main__":
    anyio.run(_amain)
