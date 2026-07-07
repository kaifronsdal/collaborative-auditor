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
    print("\n✓ all M1.1 wire + M1.3 persistence smoke checks passed")


def _text(e: dict[str, Any]) -> str:
    b = e["data"]["bundle"]
    if "application/vnd.jupyter.stream+json" in b:
        return b["application/vnd.jupyter.stream+json"]["text"]
    return b.get("text/plain", "")


if __name__ == "__main__":
    anyio.run(_amain)
