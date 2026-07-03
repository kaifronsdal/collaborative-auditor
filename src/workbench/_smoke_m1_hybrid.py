"""M1-HYBRID smoke — steps 2+3: ``m1/tools.py`` + ``wb.attach(log_dir)``.

Step 2 (``make_tools``): exercises the ``bash`` / file / review tools
directly (no agent, no model) against a real ``OrchestratorKernel``:

- ``bash`` — plain stdout lines emit as ``STREAM_MIME`` events, a
  ``{"wb":…}`` line emits as a WB_MIME card (stable id = ``eval_id``,
  second line for the same eval → ``update=True``); the model-facing
  return is plain-lines-only + ``[exit N]``.
- ``review_seeds`` — spawns a gate, human strikes seeds via
  ``{"surviving":[…]}``, tool returns the trimmed list + ``approved``.
- ``write_file`` / ``read_file`` / ``edit_file`` round-trip in the
  per-orchestrator session dir.

Step 3 (``AttachedRun``): the read-only half of ``RunHandle``:

- subprocess-launch ``inspect eval`` (mockllm, 4 slow samples) to a fresh
  ``log_dir``; inside a kernel cell ``h = wb.attach(log_dir); await h.wait()``.
  Assert ``h.finished``, ``h.n_done == 4``, card ticked ≥2×, and the WB_MIME
  payload is the same ``kind:"eval_run"`` shape ``ProgressCard`` expects.
- attach to an *already-completed* ``log_dir`` (no ctl server) →
  ``running`` empty, ``done`` populated, ``finished`` immediately.

Steps 1/4+ (``wb_display``, agent registration, frontend) land here as
they're implemented. Run:  ``uv run python -m workbench._smoke_m1_hybrid``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

import anyio

from workbench.m1.attach import AttachedRun
from workbench.m1.kernel import (
    STREAM_MIME,
    WB_MIME,
    DisplayEvent,
    OrchestratorKernel,
)
from workbench.m1.orchestrator import _prewarm  # noqa: PLC2701
from workbench.m1.tools import _session_dir, make_tools  # noqa: PLC2701
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
    wire: list[DisplayEvent] = []
    with OrchestratorKernel(on_display=wire.append) as k:
        await _run_tools(k, wire)
        ctl_found = await _run_attach(k)
    print(
        f"\n✓ all M1-HYBRID smoke checks passed "
        f"(ctl discovery: {'found subprocess' if ctl_found else 'log-only'})"
    )


# -- step 2: bash / file / review tools --------------------------------------


async def _run_tools(k: OrchestratorKernel, wire: list[DisplayEvent]) -> None:  # noqa: PLR0915
    # Duck-typed orch: ``make_tools`` only reads ``.kernel`` and ``.span_id``.
    orch = SimpleNamespace(kernel=k, span_id="smoke-hybrid")
    session_dir = _session_dir(orch)
    (bash, read_file, write_file, edit_file, ask_human, review_seeds, review_finding) = (
        make_tools(orch)
    )

    # ---- bash: plain lines → stream, {"wb":…} → WB_MIME card ---------------
    wire.clear()
    cmd = (
        "echo hello; "
        'echo \'{"wb":"eval_start","eval_id":"t1","task":"demo","total":3}\'; '
        "echo world"
    )
    out = await bash(cmd=cmd)
    streams = [ev for ev in wire if STREAM_MIME in ev.bundle]
    cards = [ev for ev in wire if WB_MIME in ev.bundle]
    assert len(streams) == 2, f"expected 2 stream events, got {len(streams)}"
    assert streams[0].bundle[STREAM_MIME]["text"].strip() == "hello"
    assert streams[1].bundle[STREAM_MIME]["text"].strip() == "world"
    assert len(cards) == 1, f"expected 1 WB_MIME card, got {len(cards)}"
    assert cards[0].id == "t1" and cards[0].stable and not cards[0].update
    assert cards[0].bundle[WB_MIME]["kind"] == "start"
    assert cards[0].bundle[WB_MIME]["task"] == "demo"
    # model-facing: plain lines only + exit code (no JSON protocol line)
    assert "hello" in out and "world" in out and "[exit 0]" in out, out
    assert '{"wb":' not in out, out
    # events landed in kernel.outputs (turn_id=-1 — outside any cell)
    assert any(ev.id == "t1" for ev in k.outputs.get(-1, [])), (
        "bash card not in kernel.outputs"
    )
    print("✓ bash: 2 stream + 1 WB_MIME card; model text = plain + [exit 0]")

    # ---- bash: second wb line for same eval_id → update=True --------------
    wire.clear()
    await bash(
        cmd='echo \'{"wb":"eval_start","eval_id":"e2","task":"x","total":2}\'; '
        'echo \'{"wb":"eval_progress","eval_id":"e2","done":1}\''
    )
    e2 = [ev for ev in wire if ev.id == "e2"]
    assert len(e2) == 2 and e2[0].update is False and e2[1].update is True, e2
    assert e2[1].bundle[WB_MIME]["kind"] == "progress"
    print("✓ bash: eval_progress on same eval_id → update=True")

    # ---- bash: timeout kills the process -----------------------------------
    out = await bash(cmd="sleep 5", timeout=1)
    assert "timeout after 1s" in out, out
    print("✓ bash: timeout → [exit timeout after Ns]")

    # ---- review_seeds: gate → strike seeds → approved return ---------------
    task = asyncio.create_task(
        review_seeds(seeds=["a", "b"] * 6, description="test", config={})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    assert len(k.gate.pending) == 1, "review_seeds gate not registered"
    (pid,) = k.gate.pending
    assert k.gate.resolve(pid, {"surviving": ["s0", "s1"]})
    result = await task
    assert result["approved"] is True, result
    assert result["seeds"] == ["a", "b"], result
    assert result["reason"] is None, result
    assert not k.gate.pending
    print("✓ review_seeds: {surviving:[s0,s1]} → approved, seeds=['a','b']")

    # ---- review_seeds: deny → approved=False -------------------------------
    task = asyncio.create_task(
        review_seeds(seeds=["x"], description="deny me", config={})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    (pid,) = k.gate.pending
    k.gate.resolve(pid, {"denied": True, "reason": "too broad"})
    result = await task
    assert result == {"approved": False, "seeds": ["x"], "reason": "too broad"}, result
    print("✓ review_seeds: denied → approved=False, reason carried")

    # ---- ask_human ---------------------------------------------------------
    task = asyncio.create_task(ask_human(question="proceed?", options=["y", "n"]))
    for _ in range(50):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    (pid,) = k.gate.pending
    k.gate.resolve(pid, "y")
    assert await task == "y"
    print("✓ ask_human: gate → 'y'")

    # ---- review_finding ----------------------------------------------------
    task = asyncio.create_task(
        review_finding(
            claim="model leaks system prompt",
            quotes=[{"sample_id": "s0", "at": 3, "role": "target", "text": "…"}],
            description="see turn 3",
        )
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if k.gate.pending:
            break
    (pid,) = k.gate.pending
    k.gate.resolve(pid, {"signed": True, "by": "tester"})
    result = await task
    assert result["signed"] is True and result["quotes"][0]["sample_id"] == "s0", result
    print("✓ review_finding: gate → signed")

    # ---- write / read / edit round-trip ------------------------------------
    w = await write_file(path="t.txt", content="hi")
    assert "wrote 2 bytes" in w, w
    r = await read_file(path="t.txt")
    assert r.strip() == "1\thi", repr(r)
    d = await edit_file(path="t.txt", old="hi", new="bye")
    assert "-hi" in d and "+bye" in d, d
    r2 = await read_file(path="t.txt")
    assert "bye" in r2, r2
    # edit_file guards
    assert "not found" in await edit_file(path="t.txt", old="nope", new="x")
    await write_file(path="dup.txt", content="ab ab")
    assert "appears 2 times" in await edit_file(path="dup.txt", old="ab", new="X")
    assert "not found" in await read_file(path="missing.txt")
    assert (session_dir / "t.txt").read_text() == "bye"
    print("✓ write_file / read_file / edit_file round-trip in session_dir")

    shutil.rmtree(Path.home() / ".workbench" / "sessions" / "smoke-hybrid", ignore_errors=True)


# -- step 3: wb.attach -------------------------------------------------------


async def _run_attach(k: OrchestratorKernel) -> bool:  # noqa: PLR0915
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
