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

Step 4 (agent registration): a real ``Orchestrator`` driven by mockllm
scripted to (t1) ``write_file("seeds.json", …)``, (t2) ``bash("inspect
eval …@demo -T n=3 --model mockllm/model --log-dir runs/r1")``, (t3)
``python("h = wb.attach('runs/r1'); await h.wait(); h.n_done")``. Asserts
the bash turn's outputs include a ``kind:"eval_run"`` card (folded from
``{"wb":"eval_start"}``), ``h.n_done == 3``, and every ``eval_run`` card in
``session.events`` came from the bash or attach turn — no in-process
``RunHandle.launch`` path.

Run:  ``uv run python -m workbench._smoke_m1_hybrid``
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
from inspect_ai.log._file import (  # noqa: PLC2701
    read_eval_log_sample_summaries_async,
)
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from workbench._smoke_util import FakeConn
from workbench.m1.attach import AttachedRun
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.orchestrator import ORCH_SOURCE, _prewarm  # noqa: PLC2701
from workbench.m1.wire import STREAM_MIME, WB_MIME, DisplayEvent
from workbench.m1.proposals import Gate
from workbench.m1.tools import _session_dir, make_tools  # noqa: PLC2701
from workbench.m1.wb import Workbench
from workbench.session import Session

N = 4
AUDIT_TASK = str(Path(__file__).parent / "m1" / "_audit_task.py")

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
    await _run_interrupt()
    await _run_e2e()
    print(
        f"\n✓ all M1-HYBRID smoke checks passed "
        f"(ctl discovery: {'found subprocess' if ctl_found else 'log-only'})"
    )


# -- step 2: bash / file / review tools --------------------------------------


async def _run_tools(k: OrchestratorKernel, wire: list[DisplayEvent]) -> None:  # noqa: PLR0915
    # Duck-typed orch: ``make_tools`` reads ``.kernel``/``.gate``/``.span_id``,
    # and ``_turn`` calls ``.record_turn`` (rewind bookkeeping — no-op here).
    orch = SimpleNamespace(
        kernel=k, gate=Gate(), span_id="smoke-hybrid", record_turn=lambda tid: None
    )
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
    # eval_start folds to the ProgressCard shape (M1-HYBRID step 5: one
    # payload union), not a bare "start" pass-through.
    assert cards[0].bundle[WB_MIME]["kind"] == "eval_run"
    assert cards[0].bundle[WB_MIME]["task"] == "demo"
    assert cards[0].bundle[WB_MIME]["total"] == 3
    # model-facing: plain lines only + exit code (no JSON protocol line)
    assert "hello" in out and "world" in out and "[exit 0]" in out, out
    assert '{"wb":' not in out, out
    # bash allocated a real kernel turn (via ``_turn(orch)``) — its
    # DisplayEvents are stamped with it, not turn_id=-1.
    assert cards[0].turn_id == 1 and streams[0].turn_id == 1, (
        cards[0].turn_id, streams[0].turn_id,
    )
    assert any(ev.id == "t1" for ev in k.outputs.get(1, [])), (
        "bash card not in kernel.outputs[1]"
    )
    assert not k.outputs.get(-1), "bash outputs leaked to turn_id=-1"
    print("✓ bash: 2 stream + 1 eval_run card @ turn 1; model text = plain + [exit 0]")

    # ---- bash: second wb line for same eval_id → update=True --------------
    wire.clear()
    await bash(
        cmd='echo \'{"wb":"eval_start","eval_id":"e2","task":"x","total":2}\'; '
        'echo \'{"wb":"eval_progress","eval_id":"e2","done":1,"elapsed":0.1}\''
    )
    e2 = [ev for ev in wire if ev.id == "e2"]
    assert len(e2) == 2 and e2[0].update is False and e2[1].update is True, e2
    assert e2[1].bundle[WB_MIME]["kind"] == "eval_run"
    assert e2[1].bundle[WB_MIME]["done"] == 1
    print("✓ bash: eval_progress on same eval_id → update=True (folded)")

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
        if orch.gate.pending:
            break
    assert len(orch.gate.pending) == 1, "review_seeds gate not registered"
    (pid,) = orch.gate.pending
    assert orch.gate.resolve(pid, {"surviving": ["s0", "s1"]})
    result = json.loads(await task)  # ToolResult has no dict — encoded
    assert result["approved"] is True, result
    assert result["seeds"] == ["a", "b"], result
    assert result["reason"] is None, result
    assert not orch.gate.pending
    print("✓ review_seeds: {surviving:[s0,s1]} → approved, seeds=['a','b']")

    # ---- review_seeds: deny → approved=False -------------------------------
    task = asyncio.create_task(
        review_seeds(seeds=["x"], description="deny me", config={})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if orch.gate.pending:
            break
    (pid,) = orch.gate.pending
    orch.gate.resolve(pid, {"denied": True, "reason": "too broad"})
    result = json.loads(await task)
    assert result == {"approved": False, "seeds": ["x"], "reason": "too broad"}, result
    print("✓ review_seeds: denied → approved=False, reason carried")

    # ---- ask_human ---------------------------------------------------------
    task = asyncio.create_task(ask_human(question="proceed?", options=["y", "n"]))
    for _ in range(50):
        await asyncio.sleep(0)
        if orch.gate.pending:
            break
    (pid,) = orch.gate.pending
    orch.gate.resolve(pid, "y")
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
        if orch.gate.pending:
            break
    (pid,) = orch.gate.pending
    orch.gate.resolve(pid, {"signed": True, "by": "tester"})
    result = json.loads(await task)
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
    k.shell.user_ns["wb"] = Workbench(Gate(), session=None)

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


# -- M1-HYBRID §Import-to-auditor: per-sample ACP interrupt ------------------


async def _run_interrupt() -> None:  # noqa: PLR0915
    """``AttachedRun.interrupt_sample`` over a subprocess eval's ACP socket.

    Launch ``demo(n=3, turns=5, turn_sleep=2.0)`` under ``--acp-server``;
    attach; wait for ``running_ids``; ``interrupt_sample`` one; assert it
    flushes to ``.eval`` within 5s while its siblings keep running.
    """
    log_dir = tempfile.mkdtemp(prefix="wb-interrupt-")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "inspect_ai",
        "eval",
        f"{AUDIT_TASK}@demo",
        "-T", "n=3",
        "-T", "turns=5",
        "-T", "turn_sleep=2.0",
        "--model", "mockllm/model",
        "--log-dir", log_dir,
        "--log-buffer", "1",
        "--acp-server",
        "--display", "none",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(proc.stderr.read())  # type: ignore[union-attr]
    try:
        h = AttachedRun(log_dir=log_dir, description=log_dir)
        # Poll until ctl + samples surface (subprocess start ≈ 1-2s).
        for _ in range(100):
            await h._poll()  # noqa: SLF001
            if h.running_ids and h.location:
                break
            await asyncio.sleep(0.1)
        assert h.running_ids, (
            f"no running samples after 10s (ctl={h._ctl!r}, log={h.location})"  # noqa: SLF001
        )
        assert h.location and h.location.endswith(".eval")
        target = h.running_ids[0]
        siblings = [i for i in h.running_ids if i != target]
        assert len(siblings) == 2, h.running_ids

        ok = await h.interrupt_sample(target)
        assert ok, (
            f"interrupt_sample({target!r}) → False "
            f"(acp={h._acp!r}, ctl={h._ctl!r})"  # noqa: SLF001
        )

        # Interrupted sample flushes to the ``.eval`` under --log-buffer 1.
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            try:
                summaries = await read_eval_log_sample_summaries_async(h.location)
            except (zipfile.BadZipFile, ValueError):
                summaries = []
            if any(str(s.id) == target for s in summaries):
                break
            assert asyncio.get_running_loop().time() < deadline, (
                f"{target!r} did not flush within 5s"
            )
            await asyncio.sleep(0.1)

        # Siblings still running — the interrupt was per-sample.
        await h._poll()  # noqa: SLF001
        assert f"{target}#1" in h.rows and target not in h.running_ids, (
            h.rows.keys(), h.running_ids,
        )
        assert set(siblings) <= set(h.running_ids), (
            f"siblings dropped: running={h.running_ids}, expected⊇{siblings}"
        )
        print(
            f"✓ interrupt_sample: {target!r} flushed via ACP, "
            f"siblings {siblings} still running"
        )
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        stderr = (await stderr_task).decode()
        if proc.returncode not in (0, -15) and stderr:
            print(f"[interrupt smoke] subprocess stderr:\n{stderr}", file=sys.stderr)
        shutil.rmtree(log_dir, ignore_errors=True)


# -- step 4: agent registration (Orchestrator end-to-end) --------------------


def _tool_call(fn: str, **args: Any) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content=f"→ {fn}")
    out.choices[0].message.tool_calls = [
        ToolCall(id="c", function=fn, type="function", arguments=args)
    ]
    return out


def _e2e_script(span_id: str) -> Any:
    """mockllm ``custom_outputs``: write_file → bash(inspect eval) → python(attach)."""
    bash_cmd = (
        f"{sys.executable} -m inspect_ai eval {AUDIT_TASK}@demo -T n=3 "
        f"--model mockllm/model --log-dir runs/r1 --log-buffer 1"
    )
    turns = [
        _tool_call("write_file", path="seeds.json", content='["a","b","c"]'),
        _tool_call("bash", cmd=bash_cmd, timeout=120),
        _tool_call(
            "python",
            code="h = wb.attach('runs/r1')\nawait h.wait()\nh.n_done",
        ),
        ModelOutput.from_content(model="mockllm", content="done."),
    ]

    def outputs(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        n = sum(1 for m in input if m.role == "assistant")
        # First call: verify all 8 tools are registered on the agent.
        if n == 0:
            names = {t.name for t in tools}
            expected = {
                "python", "bash", "read_file", "write_file", "edit_file",
                "ask_human", "review_seeds", "review_finding",
            }
            assert expected <= names, f"missing tools: {expected - names}"
        return turns[min(n, len(turns) - 1)]

    return outputs


async def _wait_tool(session: Session, fn: str, *, timeout: float = 60) -> dict[str, Any]:
    """Poll ``session.events`` until a ``ToolEvent(function=fn)`` has settled."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for e in session.events.values():
            if e["event"] == "tool" and e.get("function") == fn and not e.get("pending"):
                return e
        await asyncio.sleep(0.05)
    raise AssertionError(f"tool {fn!r} did not settle within {timeout}s")


def _wb_events(session: Session) -> list[dict[str, Any]]:
    """All orchestrator ``InfoEvent.data`` payloads that carry a WB_MIME bundle."""
    out = []
    for e in session.events.values():
        if e["event"] == "info" and e.get("source") == ORCH_SOURCE:
            b = e["data"].get("bundle") or {}
            if WB_MIME in b:
                out.append(e["data"])
    return out


async def _run_e2e() -> None:  # noqa: PLR0915
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)

    span_id = "smoke-hybrid-e2e"
    sdir = Path.home() / ".workbench" / "sessions" / span_id
    shutil.rmtree(sdir, ignore_errors=True)

    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": _e2e_script(span_id)},
        span_id=span_id,
        max_turns=6,
    )
    orch = session.orchestrator
    assert orch is not None
    assert str(orch.session_dir) == str(sdir), (orch.session_dir, sdir)

    # ---- t1: write_file --------------------------------------------------
    orch.step()
    ev = await _wait_tool(session, "write_file")
    assert "wrote" in str(ev.get("result")), ev.get("result")
    assert (sdir / "seeds.json").read_text() == '["a","b","c"]'
    print("✓ e2e t1: write_file → seeds.json in session_dir")

    # ---- t2: bash("inspect eval …@demo") --------------------------------
    orch.step()
    ev = await _wait_tool(session, "bash", timeout=120)
    result = str(ev.get("result"))
    assert "[exit 0]" in result, f"subprocess eval failed:\n{result}"
    assert '{"wb":' not in result, "protocol lines leaked to model text"
    # bash allocated kernel turn 1; its {"wb":"eval_start"} folded to a
    # kind:"eval_run" ProgressCard on the wire.
    bash_cards = [
        d for d in _wb_events(session)
        if d["turn"] == 1 and d["bundle"][WB_MIME].get("kind") == "eval_run"
    ]
    assert bash_cards, "no eval_run card from bash turn"
    assert bash_cards[0]["stable"] is True
    p = bash_cards[0]["bundle"][WB_MIME]
    # Stable display_id → session.events holds the *latest* fold: eval_done.
    assert p["task"] == "demo" and p["total"] == 3, p
    assert p["finished"] is True and p["done"] == 3, p
    assert len(p["rows"]["done"]) == 3 and p["rows"]["running"] == [], p["rows"]
    assert (sdir / "runs" / "r1").is_dir(), "log_dir not under session_dir"
    print(
        "✓ e2e t2: bash → eval_run card (turn 1, folded to done=3/3), "
        "log @ runs/r1"
    )

    # ---- t3: python("wb.attach('runs/r1')") ------------------------------
    orch.step()
    ev = await _wait_tool(session, "python")
    result = str(ev.get("result"))
    assert "\n3\n" in result or result.strip().startswith("3"), (
        f"h.n_done != 3 in tool result:\n{result}"
    )
    h = orch.kernel.shell.user_ns["h"]
    assert isinstance(h, AttachedRun) and h.finished and h.n_done == 3, (
        type(h), h.finished, h.n_done,
    )
    # relative "runs/r1" resolved against session_dir (via Workbench threading).
    assert h.log_dir == str(sdir / "runs" / "r1"), h.log_dir
    print("✓ e2e t3: wb.attach('runs/r1') → session_dir-resolved, n_done=3")

    # ---- no in-process eval_async: every eval_run card is from the bash
    # turn (subprocess wb_display) or the attach turn (AttachedRun poll).
    # A RunHandle.launch would emit kind:"eval_run" from some other turn.
    run_cards = [
        d for d in _wb_events(session)
        if d["bundle"][WB_MIME].get("kind") == "eval_run"
    ]
    turns_seen = {d["turn"] for d in run_cards}
    assert turns_seen <= {1, 2}, (
        f"eval_run cards at unexpected turns {turns_seen - {1, 2}} — "
        f"in-process RunHandle.launch?"
    )
    attach_ids = {d["bundle"][WB_MIME]["id"] for d in run_cards if d["turn"] == 2}
    assert attach_ids == {h.id}, (
        f"attach-turn eval_run cards not all from AttachedRun: {attach_ids}"
    )
    # bash turn registered in _turn_msg (rewind bookkeeping covers it).
    assert 1 in orch._turn_msg and 2 in orch._turn_msg, orch._turn_msg
    print(
        f"✓ e2e: {len(run_cards)} eval_run card(s) at turns {sorted(turns_seen)} "
        f"— no in-process RunHandle.launch"
    )

    # ---- t4: no tool call → parks ---------------------------------------
    orch.step()
    for _ in range(200):
        await asyncio.sleep(0)
        if orch.status == "paused":
            break
    assert orch.status == "paused"

    await session.close()
    shutil.rmtree(sdir, ignore_errors=True)


if __name__ == "__main__":
    anyio.run(_amain)
