"""M1-REFACTOR Batch H — coverage smokes for the 7 HIGH-risk zero-coverage paths.

Each check is small and self-contained; a subprocess ``inspect eval …@demo``
is spawned once (via ``demo_eval_cmd``) and the resulting ``.eval`` reused
for ``read.py`` and the no-ACP ``interrupt_sample`` case.

Run:  ``uv run python -m workbench._smoke_m1_coverage``
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile

import anyio
import pandas as pd

from workbench.m1._fixtures import (
    AUDIT_TASK,
    TurnSpec,
    demo_eval_cmd,
    mock_orch_session,
    wait_for,
    wait_running,
)
from workbench.m1.attach import AttachedRun
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.orchestrator import _prewarm
from workbench.m1.proposals import Finding, Gate
from workbench.m1.tools import make_tools
from workbench.m1.wb import Workbench
from workbench.m1.wire import STREAM_MIME, WB_MIME
from workbench.server import _dispatch

# -- 1. bash(background=True) ------------------------------------------------


async def _check_bash_background() -> None:
    async with mock_orch_session([]) as (_session, orch, _conn):
        k = orch.kernel
        (bash, *_) = make_tools(orch)
        k.outputs.clear()
        k.drain_notifications()

        out = await bash(cmd="sleep 0.4 && echo bg-out", background=True)
        m = re.fullmatch(r"\[bg-([0-9a-f]{6}) started · pid (\d+)\]", out)
        assert m, f"bad immediate return: {out!r}"
        bg_id, pid = m.group(1), int(m.group(2))
        # nothing landed yet — the pump is detached
        evs = [ev for turn in k.outputs.values() for ev in turn]
        assert not any(WB_MIME in ev.bundle for ev in evs)

        await wait_for(lambda: k.notifications, timeout=3.0)
        notes = k.drain_notifications()
        assert any(f"bg-{bg_id} done · exit 0" in n for n in notes), notes
        evs = [ev for turn in k.outputs.values() for ev in turn]
        done_evs = [
            ev for ev in evs if ev.bundle.get(WB_MIME, {}).get("kind") == "bg_done"
        ]
        assert len(done_evs) == 1 and done_evs[0].bundle[WB_MIME]["exit"] == 0, done_evs
        assert done_evs[0].bundle[WB_MIME]["id"] == bg_id
        # stdout streamed under the bash turn (contextvar copied into _bg task)
        streams = [ev for ev in evs if STREAM_MIME in ev.bundle]
        assert any("bg-out" in ev.bundle[STREAM_MIME]["text"] for ev in streams), streams
        assert done_evs[0].turn_id == streams[0].turn_id != -1, (
            f"bg outputs not attributed to a real turn: {done_evs[0].turn_id}"
        )
        # no orphan
        try:
            os.kill(pid, 0)
            raise AssertionError(f"pid {pid} still alive")
        except ProcessLookupError:
            pass
    print(f"✓ bash(background=True): bg-{bg_id} → bg_done card + notify, no orphan")


# -- 2. read.py: wb.transcript / excerpt / read_transcript -------------------


async def _check_read(k: OrchestratorKernel, log_file: str) -> None:
    k.shell.user_ns["_LOG"] = log_file
    r = await k.run_turn(
        "tr = wb.transcript(_LOG, 's0')\n"
        "ex = await wb.excerpt(_LOG, 's0', at=1)\n"
        "txt = await wb.read_transcript(_LOG, 's0')\n"
        "(tr, ex, txt)"
    )
    assert r.success, r.text
    tr = k.shell.user_ns["tr"]
    ex = k.shell.user_ns["ex"]
    txt = k.shell.user_ns["txt"]
    assert tr._repr_mimebundle_()[WB_MIME]["kind"] == "transcript"
    assert tr._repr_mimebundle_()[WB_MIME]["n_messages"] == len(tr) >= 2
    assert ex._repr_mimebundle_()[WB_MIME]["kind"] == "excerpt"
    assert ex._repr_mimebundle_()[WB_MIME]["at"] == 1
    assert isinstance(txt, str) and "seed 0" in txt, txt[:200]
    assert "seed 0" in ex.text, ex.text[:200]
    print(f"✓ read.py: transcript({len(tr)} msgs) / excerpt / read_transcript")


# -- 3. wb_display: erroring sample → eval_sample_done{error} + eval_done{errors} --


async def _check_wb_display_error() -> None:
    log_dir = tempfile.mkdtemp(prefix="wb-cov-err-")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "inspect_ai", "eval", f"{AUDIT_TASK}@demo",
        "-T", "n=2", "-T", "fail_on=s1",
        "--model", "mockllm/model", "--log-dir", log_dir,
        "--display", "none", "--no-fail-on-error",
        env={**os.environ, "WORKBENCH_DISPLAY": "1", "NO_COLOR": "1"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    lines = [json.loads(ln) for ln in out.decode().splitlines() if ln.startswith('{"wb":')]
    s1 = next(w for w in lines if w["wb"] == "eval_sample_done" and w["id"] == "s1")
    assert s1["error"] and "deliberate failure" in s1["error"], s1
    done = next(w for w in lines if w["wb"] == "eval_done")
    assert done["errors"] == 1, done
    s0 = next(w for w in lines if w["wb"] == "eval_sample_done" and w["id"] == "s0")
    assert s0["error"] is None, s0
    shutil.rmtree(log_dir, ignore_errors=True)
    print("✓ wb_display: erroring sample → eval_sample_done{error} + eval_done{errors:1}")


# -- 4. attach.py: interrupt_sample error paths ------------------------------


async def _check_attach_empty_dir() -> None:
    """e2e-v3 secondary finding: subprocess crashed before writing any
    ``.eval``; ``.wait()`` sat the full timeout on an empty ``log_dir``.
    Now bails after ``_grace`` seconds with a diagnostic error."""
    empty = tempfile.mkdtemp(prefix="wb-cov-empty-")
    try:
        h = AttachedRun(log_dir=empty, description="empty", _grace=0.5)
        h._watcher = asyncio.create_task(h._watch())
        await h.wait(timeout=5)
        assert h.finished
        assert h.error and "no eval log appeared" in h.error, h.error
        print(f"✓ attach empty log_dir: bailed after grace ({h.error[:40]}…)")
    finally:
        shutil.rmtree(empty, ignore_errors=True)


async def _check_acp_and_handlers(read_dir: str) -> None:
    """ACP-interrupt error paths + ``_dispatch`` M1 handlers, sharing one subprocess.

    (a) no ``--acp-server`` (completed ``read_dir`` → no ctl) → ``False``.
    (b) L4 primitives on ``read_dir``: ``discover`` populates ``.location``,
        ``wait_for_sample("s0")`` returns immediately (already flushed) —
        the ``_h_import_running`` head-half.
    Then one live ``--acp-server`` subprocess serves:
    (c) unknown sample id → ``False`` (ACP kept);
    (d) ``_dispatch stop_sample`` → per-sample cancel → sample flushes;
    (e) ``_dispatch detach_cell`` / ``cancel_cell`` / ``approve``;
    (f) process dead → ``OSError`` on connect → ``False``, ``_acp`` reset.
    """
    # (a) no ACP server
    h = await AttachedRun.discover(read_dir)
    assert await h.interrupt_sample("s0") is False
    assert h._acp is False
    print("✓ interrupt_sample: no ACP server → False")

    # (b) L4: discover + wait_for_sample (import_running head-half)
    assert h.location and h.location.endswith(".eval"), h.location
    assert await h.wait_for_sample("s0", timeout=1.0) == h.location
    print("✓ AttachedRun.discover + wait_for_sample on completed log_dir")

    # (c)-(f): one live subprocess. ``exec`` (not ``shell``) so signals reach
    # inspect directly (no /bin/sh middle-process).
    log_dir = tempfile.mkdtemp(prefix="wb-cov-acp-")
    cmd = demo_eval_cmd(
        n=3, turns=5, turn_sleep=1.5, log_dir=log_dir, acp=True, display="none"
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd.split(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    turns: list[TurnSpec] = [
        ("…", [("python", {"code": "await asyncio.sleep(30)"})]),
        ("…", [("python", {"code": "ans = await wb.ask_human('ok?')"})]),
    ]
    try:
        h2 = AttachedRun(log_dir=log_dir, description="acp")
        await wait_running(h2)

        # (c) unknown sample id → False (but _acp discovered and kept)
        assert await h2.interrupt_sample("nonexistent") is False
        from inspect_ai.agent._acp.discovery import DiscoveredEval
        assert isinstance(h2._acp, DiscoveredEval), h2._acp
        print("✓ interrupt_sample: unknown sample_id → False (ACP kept)")

        async with mock_orch_session(turns, max_turns=6) as (session, orch, _conn):
            # (d) stop_sample handler → per-sample ACP cancel → flush
            target = h2.running_ids[0]
            await _dispatch(
                session, {"t": "stop_sample", "log_dir": log_dir, "id": target}
            )
            assert await h2.wait_for_sample(target, timeout=5.0) == h2.location
            await h2._poll()
            assert target not in h2.running_ids and h2.running_ids, h2.running_ids
            print(f"✓ _dispatch stop_sample → {target!r} flushed via ACP")

            # (e) detach_cell → cell backgrounded, agent unblocks
            orch.step()
            await wait_for(lambda: 1 in orch.kernel.bg)
            await _dispatch(session, {"t": "detach_cell"})
            await wait_for(lambda: 1 in orch.kernel._detached)
            assert 1 in orch.kernel.bg, "detach cancelled the cell"
            print("✓ _dispatch detach_cell → cell backgrounded, still running")

            # cancel_cell → bg task cancelled
            await _dispatch(session, {"t": "cancel_cell", "turn": 1})
            await wait_for(lambda: 1 not in orch.kernel.bg)
            assert any("cell-1 cancelled" in n for n in orch.kernel.notifications)
            print("✓ _dispatch cancel_cell → bg task cancelled")

            # approve → gate.resolve
            orch.step()
            await wait_for(lambda: orch.gate.pending)
            (gid,) = orch.gate.pending
            await _dispatch(session, {"t": "approve", "display_id": gid, "verdict": "yes"})
            await wait_for(lambda: not orch.gate.pending)
            assert orch.kernel.shell.user_ns.get("ans") == "yes"
            # unknown id → no-op (logged, no raise)
            await _dispatch(session, {"t": "approve", "display_id": "nope", "verdict": "x"})
            print("✓ _dispatch approve → gate.resolve; unknown id → no-op")

        # (f) process dead → OSError on connect → False, _acp reset
        proc.send_signal(signal.SIGKILL)
        await proc.wait()
        assert await h2.interrupt_sample(h2.running_ids[0]) is False
        assert h2._acp is False
        print("✓ interrupt_sample: dead socket → False (_acp → False)")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(log_dir, ignore_errors=True)


# -- 6. wb.cite (in-cell) ----------------------------------------------------


async def _check_wb_cite(k: OrchestratorKernel, gate: Gate) -> None:
    turn = asyncio.create_task(
        k.run_turn(
            "f = await wb.cite('leaks prompt', "
            "[{'sample_id':'s0','at':3,'role':'target','text':'…'}], "
            "description='see t3')\nf"
        )
    )
    await wait_for(lambda: gate.pending)
    (pid,) = gate.pending
    # pending card is a cite_proposal
    pending_ev = next(ev for ev in k.outputs[k._turn_counter] if ev.id == pid)
    assert pending_ev.bundle[WB_MIME]["kind"] == "cite_proposal"
    gate.resolve(pid, {"signed": True, "by": "tester"})
    r = await turn
    assert r.success, r.text
    f = k.shell.user_ns["f"]
    assert isinstance(f, Finding) and f.signed_by == "tester", f
    assert f.quotes[0].sample_id == "s0"
    # last-expr Finding card emitted separately from the proposal
    assert any(
        ev.bundle.get(WB_MIME, {}).get("kind") == "finding" for ev in r.outputs
    ), "no Finding card in outputs"
    print("✓ wb.cite (in-cell): gate → signed Finding in user_ns")


# -- 7. plots.py -------------------------------------------------------------


def _check_plots() -> None:
    from workbench.m1 import plots

    c = plots.model_color("anthropic/claude-opus-4-8")
    assert re.fullmatch(r"#[0-9a-f]{6}", c), c
    assert plots.model_label("anthropic/claude-opus-4-8") == "Claude Opus 4.8"
    # heuristic fallback (not in MODEL_LABELS)
    assert plots.model_label("openai/gpt-9-mini-20260101") == "GPT 9 mini"

    df = pd.DataFrame({"model": ["anthropic/claude-opus-4-8", "openai/gpt-5.4"]})
    kw = plots.by_model(df)
    assert set(kw) == {"color", "color_discrete_map", "category_orders"}
    assert kw["color"] == "model"
    cmap = kw["color_discrete_map"]
    assert cmap["anthropic/claude-opus-4-8"] == c
    assert cmap["Claude Opus 4.8"] == c, "label-keyed color missing"
    assert "model" in kw["category_orders"]

    import plotly.io as pio
    plots.install_template()
    assert pio.templates.default == "plotly_white+workbench"
    plots.install_template()  # idempotent
    print("✓ plots: model_color/model_label/by_model/install_template")


# -- entrypoint --------------------------------------------------------------


async def _amain() -> None:
    _prewarm()
    _check_plots()

    # One completed .eval reused by read.py + no-ACP + L4 checks.
    read_dir = tempfile.mkdtemp(prefix="wb-cov-read-")
    cmd = demo_eval_cmd(n=2, turns=1, turn_sleep=0, log_dir=read_dir, display="none")
    proc = await asyncio.create_subprocess_exec(
        *cmd.split(),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    from inspect_ai.log import list_eval_logs
    log_file = list_eval_logs(read_dir)[0].name

    await _check_bash_background()

    with OrchestratorKernel() as k:
        gate = Gate()
        k.shell.user_ns["wb"] = Workbench(gate)
        await _check_read(k, log_file)
        await _check_wb_cite(k, gate)

    await _check_wb_display_error()
    await _check_attach_empty_dir()
    await _check_acp_and_handlers(read_dir)

    shutil.rmtree(read_dir, ignore_errors=True)
    print("\n✓ all M1 coverage smokes passed (7 HIGH-risk paths)")


if __name__ == "__main__":
    anyio.run(_amain)
