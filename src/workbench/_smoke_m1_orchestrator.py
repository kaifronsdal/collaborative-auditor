"""M1.1 wire smoke: kernel outputs → ``InfoEvent`` → ``session.events`` → wire.

Drives an ``Orchestrator`` with a mockllm scripted to emit ``python`` tool
calls, and asserts the vertical slice:

- ``DisplayEvent`` → ``InfoEvent(source="orchestrator")`` lands in
  ``session.events`` and is broadcast as ``{"t":"event", "v":…}`` — same pipe
  as M0 ``ModelEvent``s.
- ``dh.update()`` on a stable ``display_id`` ships as ``{"t":"update"}`` with
  the same ``uuid``, and ``session.events[uuid]`` holds the *latest* bundle.
- The orchestrator span resolves via ``session._resolve`` → ``("orch","orch")``.
- A pending gate appears in ``Session.view()["orchestrator"]["pending_gates"]``
  and ``orch.gate.resolve()`` clears it.
- Reconnect: ``push_full_state`` on a fresh connection carries every display
  ``InfoEvent`` (the fix the spike's ``on_display → _enqueue`` shortcut broke).

Run:  ``uv run python -m workbench._smoke_m1_orchestrator``
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import anyio

from workbench.m1._fixtures import (
    FakeConn,
    mock_orch_session,
    orch_events,
    settle,
    tool_result_text,
)
from workbench.m1.orchestrator import ORCH_SOURCE
from workbench.session import Session

# ── scripted orchestrator model ─────────────────────────────────────────────
# Turn 1: display + update on a stable id, print, last-expr.
# Turn 2: gated ask_human (blocks until resolve()).
# Turn 3: no tool call (parks the loop).
CELLS = [
    "dh = display('v1', display_id='job')\ndh.update('v2')\nprint('hello')\n1 + 1",
    "ans = await wb.ask_human('ok?')\nans",
]
TURNS = [("run", [("python", {"code": c})]) for c in CELLS]


# ── the test ────────────────────────────────────────────────────────────────


async def _amain() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="wb-orch-persist-"))
    async with mock_orch_session(TURNS, max_turns=5) as (session, orch, conn):
        # Persistence identity set BEFORE any turn so the P0.1 per-turn
        # ``record_turn`` → ``session.save()`` path fires (it no-ops when
        # ``store_dir``/``session_id`` are unset).
        session.store_dir = tmpdir
        session.session_id = "t"
        d = tmpdir / "t"

        # ---- P1.2: wb.DEFAULTS in user_ns; audit_defaults threaded ----------
        wb = orch.kernel.shell.user_ns["wb"]
        assert isinstance(wb.DEFAULTS, dict) and "target" in wb.DEFAULTS, wb.DEFAULTS
        assert orch.audit_defaults == wb.DEFAULTS, (wb.DEFAULTS, orch.audit_defaults)
        assert "max_turns" in wb.DEFAULTS and "auditor" in wb.DEFAULTS
        # system prompt was interpolated with concrete defaults (no {target}
        # placeholder survives); mockllm doesn't read it, but resume does.
        assert "{target}" not in orch.system_prompt
        assert wb.DEFAULTS["target"] in orch.system_prompt
        print(f"✓ P1.2: wb.DEFAULTS = {sorted(wb.DEFAULTS)} in user_ns; prompt interpolated")

        # ---- turn 1: display/update/print/last-expr → InfoEvents ---------------
        orch.step()
        await settle()
        evs = orch_events(session)
        assert evs, "no orchestrator InfoEvents landed in session.events"

        # P0.1 regression guard: ``orchestrator.eval`` on disk after one
        # turn, with NO explicit ``session.save()`` and NO M0 branch running.
        assert (d / "orchestrator.eval").exists(), (
            "P0.1: record_turn did not persist orchestrator.eval"
        )
        print("✓ P0.1: orchestrator.eval on disk after turn 1 (per-turn save)")

        # stable display: uuid == display_id, and session.events holds the LATEST
        stable = session.events.get("job")
        assert stable is not None, "stable display uuid != display_id"
        assert stable["data"]["bundle"]["text/plain"] == "'v2'", stable["data"]["bundle"]

        # wire: one {"t":"event"} for uuid=="job", ≥1 {"t":"update"} for it
        job_msgs = [m for m in conn.sent if m.get("event", {}).get("uuid") == "job"]
        assert [m["t"] for m in job_msgs] == ["event", "update"], [m["t"] for m in job_msgs]
        assert all("v" in m for m in job_msgs), "display wire messages missing version"

        # span_id on every InfoEvent resolves to ("orch","orch")
        for e in evs:
            assert session._resolve(e["span_id"]) == ("orch", "orch"), e["span_id"]

        # last-expr and stream both landed, in order, under turn 1
        turn1 = [e for e in evs if e["data"]["turn"] == 1]
        texts = [_text(e) for e in turn1]
        assert "hello\n" in texts and "2" in texts, texts
        print(f"✓ turn 1: {len(turn1)} InfoEvents in session.events; update path OK")

        # the model's tool result was rendered from the DisplayEvent stream.
        # Read from session.events (latest state — ToolEvent is emitted pending
        # then updated with the result), not the first wire message.
        tool_evs = [
            e
            for e in session.events.values()
            if e["event"] == "tool" and e.get("function") == "python"
        ]
        assert tool_evs, "no python ToolEvent in session.events"
        result_text = tool_result_text(tool_evs[0])
        assert "'v2'" in result_text and "'v1'" not in result_text, result_text
        # Last-expr `2` present; §5 timing suffix `[N.Ns]` is the final line.
        assert "hello" in result_text and "\n2\n" in result_text, result_text
        assert result_text.strip().endswith("s]"), result_text
        print("✓ tool result = collapsed model-facing text + [duration]")

        # ---- turn 2: gate → view() → resolve() ---------------------------------
        orch.step()
        await settle()
        view = session.view()
        gates = view["orchestrator"]["pending_gates"]
        assert len(gates) == 1, gates
        (gid,) = gates
        pending_ev = session.events[gid]
        assert (
            pending_ev["data"]["bundle"]["application/vnd.workbench.v1+json"]["pending"]
            is True
        )
        print("✓ pending gate visible in Session.view() and session.events")

        # ---- P0.4: mid-gate save → synthetic tool_result for dangling call ---
        # The turn-2 assistant's ``python`` tool_call is in ``state.messages``
        # but ``execute_tools`` is blocked on the gate — no tool result yet.
        # ``messages_for_save`` must pair it so a resumed generate doesn't 400.
        mid = orch.messages_for_save()
        assert mid[-2].role == "assistant" and mid[-2].tool_calls, mid[-2]
        assert mid[-1].role == "tool", f"no synthetic tool result: {mid[-1].role}"
        assert mid[-1].tool_call_id == mid[-2].tool_calls[0].id
        assert "session restarted" in mid[-1].text, mid[-1].text
        # round-trip via m1/persist: save mid-gate, reload into a fresh
        # session, resume_messages carries the synthetic result.
        session.save()
        from workbench.m1.persist import load_orchestrator
        mid_meta = load_orchestrator(Session(), d)
        rm = mid_meta["resume_messages"]
        assert rm[-1].role == "tool" and "session restarted" in rm[-1].text, rm[-1]
        assert rm[-1].tool_call_id == rm[-2].tool_calls[0].id
        # every assistant tool_call in the resumed history is paired — the
        # invariant a real provider checks.
        tool_ids = {m.tool_call_id for m in rm if m.role == "tool"}
        for m in rm:
            if m.role == "assistant" and m.tool_calls:
                for tc in m.tool_calls:
                    assert tc.id in tool_ids, f"unpaired tool_call {tc.id} on resume"
        print("✓ P0.4: mid-gate save → synthetic tool_result; persist round-trip paired")

        # ---- reconnect while gate pending: full state carries display events ---
        conn2 = FakeConn()
        await session.push_full_state(conn2)
        state = conn2.sent[0]
        reconnect_orch = [
            e
            for e in state["events"]
            if e["event"] == "info" and e["source"] == ORCH_SOURCE
        ]
        assert len(reconnect_orch) == len(orch_events(session)), (
            "reconnect dropped orchestrator display events"
        )
        assert state["orchestrator"]["pending_gates"] == [gid]
        print(
            f"✓ reconnect: push_full_state ships {len(reconnect_orch)} display events + gate"
        )

        # resolve → dh.update fires → {"t":"update"} for gid, pending=False
        assert orch.gate.resolve(gid, "yes")
        await settle()
        assert (
            session.events[gid]["data"]["bundle"]["application/vnd.workbench.v1+json"][
                "pending"
            ]
            is False
        )
        assert not orch.gate.pending
        assert session.view()["orchestrator"]["pending_gates"] == []
        print("✓ resolve() → update event, gate cleared")

        # ---- turn 3: no tool call → parks --------------------------------------
        orch.step()
        await settle()
        assert orch.status == "paused"

        # ---- P3: .ipynb export -------------------------------------------------
        import nbformat

        from workbench.m1.export import export_ipynb

        nb = export_ipynb(orch)
        nbformat.validate(nb)
        assert len(nb["cells"]) > 0, "export_ipynb produced no cells"
        # Header + ≥1 markdown (turn-1 prose "run") + ≥1 code cell (turn-1
        # python) with outputs mapped from ``kernel.outputs``.
        code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
        assert code_cells, [c["cell_type"] for c in nb["cells"]]
        c1 = code_cells[0]
        assert c1["source"] == CELLS[0], c1["source"]
        otypes = {o["output_type"] for o in c1["outputs"]}
        # turn-1 emitted a stable display, a print, and a last-expr (2)
        assert "stream" in otypes and "execute_result" in otypes, otypes
        assert nb["cells"][0]["cell_type"] == "markdown"
        assert "Session" in nb["cells"][0]["source"]
        print(
            f"✓ P3: export_ipynb → {len(nb['cells'])} cells "
            f"({len(code_cells)} code), nbformat.validate OK"
        )

        # ---- M1.3 persistence: pure .eval (no sidecar) -------------------------
        # No explicit ``session.save()`` — P0.1's per-turn save wrote turns
        # 1+2, and P0.2's ``Session.close()`` (in ``mock_orch_session``'s
        # finally) captures turn 3's no-tool assistant before ``load()``.
        assert (d / "orchestrator.eval").exists(), list(d.iterdir())
        assert not (d / "orchestrator_events.json").exists(), (
            "sidecar written — should be pure .eval"
        )
        span_id = orch.span_id
        saved_msgs = list(orch.state.messages)
        saved_orch_events = len(orch_events(session))
        assert saved_msgs and saved_orch_events, "nothing to save"
        print(
            f"✓ save(): {len(saved_msgs)} messages + {saved_orch_events} display "
            f"events → orchestrator.eval (no sidecar)"
        )
    # (mock_orch_session closed the session + kernel and restored cwd)

    # ---- M1.3 persistence: load() → resumed orchestrator -------------------
    sess2 = await Session.load("t", tmpdir)
    await settle()  # let orch2.run() reach the gate and set .state
    orch2 = sess2.orchestrator
    assert orch2 is not None, "load() didn't resume orchestrator"
    assert orch2.span_id == span_id, "resumed span_id mismatch"

    # display InfoEvents merged into sess2.events (via _condense — same shape)
    loaded_orch_events = len(orch_events(sess2))
    assert loaded_orch_events >= saved_orch_events, (
        f"lost display events on load: {loaded_orch_events} < {saved_orch_events}"
    )
    assert sess2.events["job"]["data"]["bundle"]["text/plain"] == "'v2'"

    # loaded ModelEvents were re-interned into THIS session's pool: every
    # input_refs range is in-bounds (the sidecar bug this design avoids).
    for e in sess2.events.values():
        if e["event"] == "model" and e.get("input_refs"):
            for start, end in e["input_refs"]:
                assert 0 <= start < end <= len(sess2.pool), (start, end, len(sess2.pool))
    assert len(sess2.pool) > 0, "orch ModelEvents not re-interned into pool"

    # resumed agent history = saved messages + [kernel restarted …] note
    assert orch2.state is not None
    msgs2 = orch2.state.messages
    assert len(msgs2) == len(saved_msgs) + 1, (len(msgs2), len(saved_msgs))
    for a, b in zip(saved_msgs, msgs2, strict=False):
        assert a.role == b.role and a.text == b.text, (a.role, b.role)
    assert msgs2[-1].role == "user"
    assert "[kernel restarted" in msgs2[-1].text, msgs2[-1].text
    print(
        f"✓ load(): {loaded_orch_events} display events restored, "
        f"resume history = {len(saved_msgs)} + kernel-restart note"
    )

    await sess2.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- P2 session fork: fork_orchestrator → new Session ------------------
    from workbench import server as _server

    tmpdir2 = Path(tempfile.mkdtemp(prefix="wb-orch-fork-"))
    async with mock_orch_session(TURNS, max_turns=5) as (session, orch, conn):
        session.store_dir = tmpdir2
        session.session_id = "p"
        # run turns 1+2 (resolve the gate) so ``_turn_msg[2]`` exists
        orch.step()
        await settle()
        orch.step()
        await settle()
        assert orch.gate.resolve(next(iter(orch.gate.pending)), "yes")
        await settle()
        # seed things fork_seed / __init__ should propagate
        orch.run_log_dirs.append("runs/r1")
        (orch.session_dir / "seeds.json").write_text('["a"]')
        (orch.session_dir / "runs").mkdir(exist_ok=True)
        parent_dir = orch.session_dir

        new_id = await session.fork_orchestrator(2)
        assert new_id in _server.sessions and "p" not in _server.sessions
        child = _server.sessions[new_id]
        await settle()
        corch = child.orchestrator
        assert corch is not None and corch.state is not None
        # fork at turn 2 = keep [system, asst_t1, tool_t1] + FORK_NOTE
        roles = [m.role for m in corch.state.messages]
        assert roles == ["system", "assistant", "tool", "user"], roles
        assert "[forked from session p at turn 2" in corch.state.messages[-1].text
        # run_log_dirs absolutized against parent's session_dir + exposed
        assert corch.run_log_dirs == [str(parent_dir / "runs/r1")], corch.run_log_dirs
        wb2 = corch.kernel.shell.user_ns["wb"]
        assert wb2.DEFAULTS["parent_runs"] == corch.run_log_dirs
        # own session_dir; top-level file copied, ``runs/`` NOT
        assert corch.session_dir != parent_dir
        assert (corch.session_dir / "seeds.json").read_text() == '["a"]'
        assert not (corch.session_dir / "runs").exists()
        # fresh findings store
        assert not (corch.session_dir / "findings.jsonl").exists()
        print(
            f"✓ P2 fork: session {new_id!r} · {roles} · "
            f"run_log_dirs={corch.run_log_dirs} · seeds.json copied"
        )
        await child.close()
        _server.sessions.pop(new_id, None)
    shutil.rmtree(tmpdir2, ignore_errors=True)

    print("\n✓ all M1.1 wire + M1.3 persistence smoke checks passed")


def _text(e: dict[str, Any]) -> str:
    b = e["data"]["bundle"]
    if "application/vnd.jupyter.stream+json" in b:
        return b["application/vnd.jupyter.stream+json"]["text"]
    return b.get("text/plain", "")


if __name__ == "__main__":
    anyio.run(_amain)
