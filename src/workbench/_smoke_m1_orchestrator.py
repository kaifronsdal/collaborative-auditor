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
  and ``kernel.resolve()`` clears it.
- Reconnect: ``push_full_state`` on a fresh connection carries every display
  ``InfoEvent`` (the fix the spike's ``on_display → _enqueue`` shortcut broke).

Run:  ``uv run python -m workbench._smoke_m1_orchestrator``
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from workbench._smoke_util import FakeConn
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


def _python(code: str) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content=f"running: {code[:20]}")
    out.choices[0].message.tool_calls = [
        ToolCall(id="c", function="python", type="function", arguments={"code": code})
    ]
    return out


def _orch_outputs(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    # Pick reply by count of prior assistant turns (deterministic across replay).
    n = sum(1 for m in input if m.role == "assistant")
    if n < len(CELLS):
        return _python(CELLS[n])
    return ModelOutput.from_content(model="mockllm", content="done.")


# ── the test ────────────────────────────────────────────────────────────────


async def _amain() -> None:  # noqa: PLR0915
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)

    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": _orch_outputs},
        max_turns=5,
    )
    orch = session.orchestrator
    assert orch is not None
    # TODO(m1-refactor): drop once ``Orchestrator.run()`` wraps ``with self.kernel:``
    orch.kernel.__enter__()

    # ---- turn 1: display/update/print/last-expr → InfoEvents ---------------
    orch.step()
    await _settle()
    orch_events = _orch_events(session)
    assert orch_events, "no orchestrator InfoEvents landed in session.events"

    # stable display: uuid == display_id, and session.events holds the LATEST
    stable = session.events.get("job")
    assert stable is not None, "stable display uuid != display_id"
    assert stable["data"]["bundle"]["text/plain"] == "'v2'", stable["data"]["bundle"]

    # wire: one {"t":"event"} for uuid=="job", ≥1 {"t":"update"} for it
    job_msgs = [m for m in conn.sent if m.get("event", {}).get("uuid") == "job"]
    assert [m["t"] for m in job_msgs] == ["event", "update"], [m["t"] for m in job_msgs]
    assert all("v" in m for m in job_msgs), "display wire messages missing version"

    # span_id on every InfoEvent resolves to ("orch","orch")
    for e in orch_events:
        assert session._resolve(e["span_id"]) == ("orch", "orch"), e["span_id"]

    # last-expr and stream both landed, in order, under turn 1
    turn1 = [e for e in orch_events if e["data"]["turn"] == 1]
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
    result_text = _tool_result_text(tool_evs[0])
    assert "'v2'" in result_text and "'v1'" not in result_text, result_text
    assert "hello" in result_text and result_text.strip().endswith("2"), result_text
    print("✓ tool result = collapsed model-facing text")

    # ---- turn 2: gate → view() → resolve() ---------------------------------
    orch.step()
    await _settle()
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
    assert len(reconnect_orch) == len(_orch_events(session)), (
        "reconnect dropped orchestrator display events"
    )
    assert state["orchestrator"]["pending_gates"] == [gid]
    print(
        f"✓ reconnect: push_full_state ships {len(reconnect_orch)} display events + gate"
    )

    # resolve → dh.update fires → {"t":"update"} for gid, pending=False
    assert orch.kernel.resolve(gid, "yes")
    await _settle()
    assert (
        session.events[gid]["data"]["bundle"]["application/vnd.workbench.v1+json"][
            "pending"
        ]
        is False
    )
    assert not orch.kernel.pending
    assert session.view()["orchestrator"]["pending_gates"] == []
    print("✓ resolve() → update event, gate cleared")

    # ---- turn 3: no tool call → parks --------------------------------------
    orch.step()
    await _settle()
    assert orch.status == "paused"

    # ---- M1.3 persistence: save() writes pure .eval (no sidecar) -----------
    tmpdir = Path(tempfile.mkdtemp(prefix="wb-orch-persist-"))
    session.store_dir = tmpdir
    session.session_id = "t"
    session.save()
    d = tmpdir / "t"
    assert (d / "orchestrator.eval").exists(), list(d.iterdir())
    assert not (d / "orchestrator_events.json").exists(), (
        "sidecar written — should be pure .eval"
    )
    saved_msgs = list(orch.state.messages)
    saved_orch_events = len(_orch_events(session))
    assert saved_msgs and saved_orch_events, "nothing to save"
    print(
        f"✓ save(): {len(saved_msgs)} messages + {saved_orch_events} display "
        f"events → orchestrator.eval (no sidecar)"
    )

    await session.close()  # cancels orch.task → run() finally → kernel.__exit__

    # ---- M1.3 persistence: load() → resumed orchestrator -------------------
    sess2 = await Session.load("t", tmpdir)
    await _settle()  # let orch2.run() reach the gate and set .state
    orch2 = sess2.orchestrator
    assert orch2 is not None, "load() didn't resume orchestrator"
    assert orch2.span_id == orch.span_id, "resumed span_id mismatch"

    # display InfoEvents merged into sess2.events (via _condense — same shape)
    loaded_orch_events = len(_orch_events(sess2))
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
    print("\n✓ all M1.1 wire + M1.3 persistence smoke checks passed")


def _orch_events(session: Session) -> list[dict[str, Any]]:
    return [
        e
        for e in session.events.values()
        if e["event"] == "info" and e.get("source") == ORCH_SOURCE
    ]


def _text(e: dict[str, Any]) -> str:
    b = e["data"]["bundle"]
    if "application/vnd.jupyter.stream+json" in b:
        return b["application/vnd.jupyter.stream+json"]["text"]
    return b.get("text/plain", "")


def _tool_result_text(tool_ev: dict[str, Any]) -> str:
    # ToolEvent.result may be str or list[Content]; mockllm path yields str.
    r = tool_ev.get("result")
    if isinstance(r, list):
        return "".join(c.get("text", "") for c in r if isinstance(c, dict))
    return r or ""


async def _settle(n: int = 100) -> None:
    """Let the loop churn until in-flight tasks quiesce (mockllm is sync)."""
    for _ in range(n):
        await asyncio.sleep(0)


if __name__ == "__main__":
    anyio.run(_amain)
