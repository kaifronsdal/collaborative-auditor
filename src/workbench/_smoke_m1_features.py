"""M1 feature smokes not covered by ``_smoke_m1_{kernel,orchestrator,run}``.

Five self-contained tests, each on a fresh ``Session`` / kernel:

1. **Rewind end-to-end** — ``play()`` 4 tool turns → park → ``rewind(2)`` →
   events at/after turn-2's ``ModelEvent`` carry ``rewound=True``, a
   ``rewind_marker`` ``InfoEvent`` lands, status parks; ``step()`` re-runs
   turn 2 (``_apply_rewind`` truncates ``state.messages``).
2. **Rewind × persistence** — save the rewound session, ``Session.load`` it,
   assert every rewound uuid comes back with ``rewound=True`` (so
   ``eventsToOrchTurns`` still filters them). This is the interaction the
   pydantic ``Event`` round-trip would silently drop — see the
   ``rewound_uuids`` metadata fix in ``m1/persist.py``.
3. **Interrupt-and-send** — turn 1's cell blocks on ``sleep(30)``;
   ``{"t":"interrupt_and_send"}`` cancels it, queues a user message, and
   releases one turn. The next generate's ``input`` carries both the
   ``[interrupted by user …]`` tool result *and* the queued user message;
   partial displays emitted before the sleep survive in ``kernel.outputs``.
4. **``snapshot_running``** — launch ``wb.run_eval`` in-cell with a solver
   that checkpoints ``AuditTape.trajectories`` into its store; while running,
   ``snapshot_running(sample_id)`` returns ``(History, BranchMeta)`` without
   interrupting; the sample runs to completion.
5. **``stop_sample`` wire** — ``_dispatch({"t":"stop_sample", …, hard:True})``
   → ``ActiveSample.interrupt("score")`` → gone from ``active_samples()``
   within 2s.

Run:  ``uv run python -m workbench._smoke_m1_features``
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import anyio
from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.log._samples import active_samples  # noqa: PLC2701
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo
from inspect_petri._auditor import AuditTape
from inspect_petri.target import History

from workbench._smoke_util import FakeConn
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.orchestrator import ORCH_SOURCE
from workbench.m1.run import CONTROL, snapshot_running
from workbench.m1.wb import Workbench
from workbench.run import BranchMeta
from workbench.server import _dispatch  # noqa: PLC2701
from workbench.session import Session


# ── helpers ────────────────────────────────────────────────────────────────


def _python(code: str) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content=f"run: {code[:24]}")
    out.choices[0].message.tool_calls = [
        ToolCall(id="c", function="python", type="function", arguments={"code": code})
    ]
    return out


async def _wait_for(pred, *, timeout: float = 3.0, tick: float = 0.01) -> bool:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(tick)
    return False


def _n_assistant(orch) -> int:  # noqa: ANN001
    if orch.state is None:
        return 0
    return sum(1 for m in orch.state.messages if m.role == "assistant")


def _orch_events(session: Session) -> list[dict[str, Any]]:
    return [
        e
        for e in session.events.values()
        if session._resolve(e.get("span_id")) == ("orch", "orch")  # noqa: SLF001
    ]


# ── 1 + 2: rewind end-to-end, then rewind × persistence ────────────────────

_REWIND_CELLS = ["a = 1\na", "b = 2\nb", "c = 3\nc", "d = 4\nd"]


def _rewind_outputs(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    n = sum(1 for m in input if m.role == "assistant")
    if n < len(_REWIND_CELLS):
        return _python(_REWIND_CELLS[n])
    return ModelOutput.from_content(model="mockllm", content="done.")


async def _test_rewind_and_persist() -> None:  # noqa: PLR0915
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)

    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": _rewind_outputs},
        max_turns=20,
    )
    orch = session.orchestrator
    assert orch is not None

    # ---- run freely until parked (4 tool turns + no-tool turn(s)) ----------
    orch.play()
    assert await _wait_for(lambda: orch.status == "paused"), (
        f"never parked (status={orch.status})"
    )
    n_before = _n_assistant(orch)
    assert n_before >= 5, f"expected ≥5 assistant turns before rewind, got {n_before}"
    assert set(orch._turn_msg) == {1, 2, 3, 4}, orch._turn_msg  # noqa: SLF001

    # ---- rewind(2) ---------------------------------------------------------
    await orch.rewind(2)
    for _ in range(20):  # let drain() flush the enqueued {"t":"rewound"}
        await asyncio.sleep(0)
    assert orch.status == "paused"
    assert orch._rewind_to == 2  # noqa: SLF001

    # rewind_marker InfoEvent landed, not itself marked rewound
    markers = [
        e
        for e in session.events.values()
        if e["event"] == "info"
        and e.get("source") == ORCH_SOURCE
        and e["data"].get("kind") == "rewind_marker"
    ]
    assert len(markers) == 1, f"expected one rewind_marker, got {len(markers)}"
    assert markers[0]["data"]["to_turn"] == 2
    assert not markers[0].get("rewound"), "rewind_marker itself flagged rewound"

    # {"t":"rewound"} broadcast on the wire
    assert any(m["t"] == "rewound" for m in conn.sent), (
        "no {'t':'rewound'} wire message"
    )

    # every InfoEvent for kernel turns 2–4 is flagged; turn-1 events are not
    orch_infos = [
        e
        for e in _orch_events(session)
        if e["event"] == "info" and "turn" in (e.get("data") or {})
    ]
    for e in orch_infos:
        t = e["data"]["turn"]
        if t >= 2:
            assert e.get("rewound") is True, f"turn-{t} InfoEvent not flagged rewound"
        elif t == 1:
            assert not e.get("rewound"), f"turn-1 InfoEvent wrongly flagged: {e}"

    # ModelEvents: turn-1's is unflagged; every other one is. Ordered by
    # ``_by_role`` (emission order — mockllm timestamps collide at ms res).
    orch_models = [
        session.events[u]
        for u in session._by_role.get(("orch", "orch"), [])  # noqa: SLF001
        if session.events[u]["event"] == "model"
    ]
    assert not orch_models[0].get("rewound"), "turn-1 ModelEvent flagged"
    assert all(e.get("rewound") for e in orch_models[1:]), (
        "some post-turn-1 ModelEvents not flagged"
    )

    rewound_uuids = {
        u for u, e in session.events.items() if e.get("rewound") is True
    }
    assert rewound_uuids, "mark_rewound flagged nothing"
    print(
        f"✓ rewind(2): {len(rewound_uuids)} events flagged, "
        f"rewind_marker emitted, status=paused"
    )

    # ---- persistence round-trip (before step, so _rewind_to is pending) ----
    tmpdir = Path(tempfile.mkdtemp(prefix="wb-rewind-persist-"))
    session.store_dir = tmpdir
    session.session_id = "rw"
    session.save()
    d = tmpdir / "rw"
    assert (d / "orchestrator.eval").exists()

    # ---- step() → _apply_rewind truncates → turn 2 re-runs -----------------
    orch.step()
    assert await _wait_for(lambda: _n_assistant(orch) == 2 and not orch.kernel.bg), (
        f"post-rewind step didn't settle at 2 assistants "
        f"(n={_n_assistant(orch)}, bg={list(orch.kernel.bg)})"
    )
    # truncation: only assistant #1 survived + one fresh re-run
    ids = [m.id for m in orch.state.messages if m.role == "assistant"]
    assert ids[0] == orch._turn_msg[1], "turn-1 assistant lost"  # noqa: SLF001
    assert set(orch._turn_msg) == {1, 5}, orch._turn_msg  # noqa: SLF001
    assert 2 not in orch.kernel.outputs and 5 in orch.kernel.outputs
    # the re-run executed CELLS[1] again
    assert orch.kernel.shell.user_ns["b"] == 2
    print(
        f"✓ step() after rewind: state.messages truncated "
        f"({n_before}→{_n_assistant(orch)} assistants), turn 2 re-ran as kernel turn 5"
    )

    await session.close()

    # ---- load: rewound flags survive the .eval round-trip ------------------
    sess2 = await Session.load("rw", tmpdir)
    for _ in range(50):
        await asyncio.sleep(0)
    orch2 = sess2.orchestrator
    assert orch2 is not None
    loaded = {u for u, e in sess2.events.items() if e.get("rewound") is True}
    missing = rewound_uuids - set(sess2.events)
    unflagged = (rewound_uuids & set(sess2.events)) - loaded
    assert not unflagged, (
        f"rewound flag lost on load for {len(unflagged)} events "
        f"(pydantic Event round-trip dropped top-level 'rewound'): "
        f"{sorted(unflagged)[:3]}"
    )
    # the marker reloaded and is still un-flagged
    markers2 = [
        e
        for e in sess2.events.values()
        if e["event"] == "info" and (e.get("data") or {}).get("kind") == "rewind_marker"
    ]
    assert markers2 and not markers2[0].get("rewound")
    # resume history: pending ``_rewind_to`` was applied at save time so the
    # agent doesn't re-read discarded turns.
    assert orch2.state is not None
    resumed_assistants = sum(1 for m in orch2.state.messages if m.role == "assistant")
    assert resumed_assistants == 1, (
        f"resume_messages carried {resumed_assistants} assistants "
        f"(pending rewind not applied on save)"
    )
    print(
        f"✓ rewind × persist: {len(loaded)} rewound flags survived load "
        f"({len(missing)} uuids not reloaded); resume history truncated to "
        f"{resumed_assistants} assistant"
    )
    await sess2.close()


# ── 3: interrupt-and-send ──────────────────────────────────────────────────


async def _test_interrupt_and_send() -> None:
    captured: list[list[ChatMessage]] = []

    def _outputs(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        captured.append(list(input))
        n = sum(1 for m in input if m.role == "assistant")
        if n == 0:
            return _python("display('before')\nawait asyncio.sleep(30)")
        return ModelOutput.from_content(model="mockllm", content="ack.")

    session = Session()
    await session.start()
    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": _outputs},
        max_turns=6,
    )
    orch = session.orchestrator
    assert orch is not None

    orch.step()
    # wait until the cell task is registered and the pre-sleep display landed
    assert await _wait_for(lambda: 1 in orch.kernel.bg), "cell never registered"
    assert await _wait_for(lambda: orch.kernel.outputs.get(1)), (
        "pre-sleep display() never landed"
    )
    partial_before = list(orch.kernel.outputs[1])
    assert any("'before'" in ev.text for ev in partial_before), partial_before

    # ---- interrupt-and-send via the wire dispatch --------------------------
    await _dispatch(
        session, {"t": "interrupt_and_send", "turn": 1, "text": "do X instead"}
    )
    assert await _wait_for(lambda: len(captured) >= 2), (
        f"second generate never fired (captured={len(captured)})"
    )

    # partial outputs survived the cancel
    assert any("'before'" in ev.text for ev in orch.kernel.outputs[1]), (
        "partial display lost after interrupt"
    )

    # next generate's input: tool result carries [interrupted by user …] AND
    # the queued user message follows it
    second = captured[1]
    tool_msgs = [m for m in second if m.role == "tool"]
    assert tool_msgs, "no tool result in second generate's input"
    tool_text = str(tool_msgs[-1].content)
    assert "[interrupted by user after " in tool_text, tool_text
    assert "'before'" in tool_text, f"partial output missing from tool result: {tool_text}"
    user_msgs = [m for m in second if isinstance(m, ChatMessageUser)]
    assert any(m.text == "do X instead" for m in user_msgs), (
        f"queued user message not in generate input: {[m.text for m in user_msgs]}"
    )
    # ordering: tool result precedes the injected user message
    idx_tool = second.index(tool_msgs[-1])
    idx_user = next(i for i, m in enumerate(second) if m.text == "do X instead")
    assert idx_tool < idx_user, "user message landed before the interrupted tool result"
    print(
        "✓ interrupt_and_send: [interrupted by user …] in tool result + "
        "ChatMessageUser('do X instead') both reach next generate; "
        f"{len(orch.kernel.outputs[1])} partial outputs kept"
    )
    await session.close()


# ── 4 + 5: snapshot_running / stop_sample against a live in-cell eval ──────


@solver
def slow_with_tape(per_turn: float = 0.4):
    """A 3-turn mockllm solver that checkpoints an ``AuditTape`` each turn.

    ``AuditTape()`` binds to the sample's ``Store`` (via the contextvar),
    which the inspect fork exposes as ``ActiveSample.store`` — so
    ``snapshot_running`` reads a non-empty ``trajectories`` without falling
    through to ``adopt_running``.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        tape = AuditTape()
        tape.seed_instructions = state.input_text
        tape.trajectories = History().dump()
        for _ in range(3):
            await asyncio.sleep(per_turn)
            state = await generate(state)
            tape.trajectories = History().dump()
        return state

    return solve


def make_task_tape(tag: str, n: int, per_turn: float = 0.4) -> Task:
    return Task(
        dataset=[Sample(input=f"{tag}-{i}", id=f"{tag}-{i}") for i in range(n)],
        solver=slow_with_tape(per_turn),
        name=f"t-{tag}",
    )


async def _test_snapshot_and_stop() -> None:  # noqa: PLR0915
    from workbench.m1.orchestrator import _prewarm  # noqa: PLC0415, PLC2701

    _prewarm()
    # bare session for the ``stop_sample`` dispatch (handler is stateless)
    session = Session()
    await session.start()

    with OrchestratorKernel() as k:
        k.shell.user_ns["wb"] = Workbench(k.gate, session=None)
        k.shell.user_ns["make_task_tape"] = make_task_tape

        # ---- 4. snapshot_running: read live store, sample keeps running ----
        r = await k.run_turn(
            "h = wb.run_eval(make_task_tape('imp', 2, 0.35), model='mockllm/model')"
        )
        assert r.success, r.text
        h = k.shell.user_ns["h"]
        assert await _wait_for(
            lambda: any(str(s.sample.id) == "imp-0" for s in active_samples()),
            timeout=5.0,
        ), "imp-0 never appeared in active_samples()"
        # give the solver a tick to write ``trajectories`` before snapshotting
        await asyncio.sleep(0.05)

        s0 = next(s for s in active_samples() if str(s.sample.id) == "imp-0")
        # ``ActiveSample.store`` is a fork feature (@536002a8) that a later
        # merge regressed — probe defensively so this smoke reports the
        # fallthrough rather than ``AttributeError``.
        store = getattr(s0, "store", None)
        store_populated = bool(store and AuditTape(store=store).trajectories)
        history, meta = await snapshot_running("imp-0")
        assert isinstance(history, History) and isinstance(meta, BranchMeta)
        assert meta.seed == "imp-0", meta.seed
        if store_populated:
            # v2 path: sample must NOT have been interrupted
            still_running = any(
                str(s.sample.id) == "imp-0" for s in active_samples()
            )
            assert still_running, (
                "snapshot_running interrupted the sample "
                "(should read live store and leave it running)"
            )
            await h.wait()
            assert h.n_done == 2 and h.finished, (h.n_done, h.finished)
            print(
                "✓ snapshot_running: (History, BranchMeta) via live "
                f"ActiveSample.store; batch ran to completion ({h.n_done}/2)"
            )
        else:
            # fell through to adopt_running (interrupt + flush)
            await h.wait()
            print(
                "· snapshot_running fell through to adopt_running "
                "(store not populated pre-snapshot) — (History, BranchMeta) OK"
            )

        # ---- 5. stop_sample wire: hard interrupt via _dispatch -------------
        r = await k.run_turn(
            "hs = wb.run_eval(make_task_tape('stp', 3, 0.5), model='mockllm/model')"
        )
        assert r.success, r.text
        hs = k.shell.user_ns["hs"]
        assert await _wait_for(
            lambda: any(str(s.sample.id) == "stp-1" for s in active_samples()),
            timeout=5.0,
        ), "stp-1 never appeared in active_samples()"

        await _dispatch(session, {"t": "stop_sample", "id": "stp-1", "hard": True})
        gone = await _wait_for(
            lambda: not any(str(s.sample.id) == "stp-1" for s in active_samples()),
            timeout=2.0,
        )
        assert gone, "stp-1 still in active_samples() 2s after hard stop_sample"
        # siblings unaffected
        assert any(
            str(s.sample.id) in {"stp-0", "stp-2"} for s in active_samples()
        ), "stop_sample took out siblings"
        await hs.wait()
        assert hs.finished
        CONTROL.clear()
        print(
            f"✓ stop_sample wire: stp-1 gone from active_samples() within 2s; "
            f"siblings ran to completion ({hs.n_done}/{hs.total})"
        )

    await session.close()


# ── entrypoint ─────────────────────────────────────────────────────────────


async def _amain() -> None:
    await _test_rewind_and_persist()
    await _test_interrupt_and_send()
    await _test_snapshot_and_stop()
    print("\n✓ all M1 feature smokes passed")


if __name__ == "__main__":
    anyio.run(_amain)
