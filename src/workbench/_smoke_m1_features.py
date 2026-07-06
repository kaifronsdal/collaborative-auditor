"""M1 feature smokes not covered by ``_smoke_m1_{kernel,orchestrator,hybrid}``.

Three self-contained tests, each on a fresh ``Session`` / kernel:

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

The old ``snapshot_running`` / in-process ``stop_sample`` checks are gone
(M1-HYBRID step 6 — evals run in subprocesses now); the ACP per-sample
interrupt path is covered by ``_smoke_m1_hybrid._run_interrupt``.

Run:  ``uv run python -m workbench._smoke_m1_features``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
from inspect_ai.model import ChatMessageUser

from workbench.m1._fixtures import (
    TurnSpec,
    mock_orch_session,
    orch_events,
    settle,
    wait_for,
)
from workbench.m1.orchestrator import ORCH_SOURCE
from workbench.server import _dispatch
from workbench.session import Session


def _n_assistant(orch) -> int:
    if orch.state is None:
        return 0
    return sum(1 for m in orch.state.messages if m.role == "assistant")


# ── 1 + 2: rewind end-to-end, then rewind × persistence ────────────────────

_REWIND_TURNS: list[TurnSpec] = [
    ("run", [("python", {"code": f"{v} = {i}\n{v}"})])
    for i, v in enumerate("abcd", start=1)
]


async def _test_rewind_and_persist() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="wb-rewind-persist-"))
    async with mock_orch_session(_REWIND_TURNS, max_turns=20) as (session, orch, conn):
        # ---- run freely until parked (4 tool turns + no-tool turn(s)) ----------
        orch.play()
        await wait_for(lambda: orch.status == "paused")
        n_before = _n_assistant(orch)
        assert n_before >= 5, f"expected ≥5 assistant turns before rewind, got {n_before}"
        assert set(orch._turn_msg) == {1, 2, 3, 4}, orch._turn_msg

        # ---- rewind(2) ---------------------------------------------------------
        await orch.rewind(2)
        await settle(20)  # let drain() flush the enqueued {"t":"rewound"}
        assert orch.status == "paused"
        assert orch._rewind_to == 2

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
        for e in orch_events(session):
            t = (e.get("data") or {}).get("turn")
            if t is None:
                continue
            if t >= 2:
                assert e.get("rewound") is True, f"turn-{t} InfoEvent not flagged rewound"
            elif t == 1:
                assert not e.get("rewound"), f"turn-1 InfoEvent wrongly flagged: {e}"

        # ModelEvents: turn-1's is unflagged; every other one is. Ordered by
        # ``by_role`` (emission order — mockllm timestamps collide at ms res).
        orch_models = [
            session.events[u]
            for u in session.by_role.get(("orch", "orch"), [])
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
        session.store_dir = tmpdir
        session.session_id = "rw"
        session.save()
        assert (tmpdir / "rw" / "orchestrator.eval").exists()

        # ---- step() → _apply_rewind truncates → turn 2 re-runs -----------------
        orch.step()
        await wait_for(lambda: _n_assistant(orch) == 2 and not orch.kernel.bg)
        # truncation: only assistant #1 survived + one fresh re-run
        ids = [m.id for m in orch.state.messages if m.role == "assistant"]
        assert ids[0] == orch._turn_msg[1], "turn-1 assistant lost"
        assert set(orch._turn_msg) == {1, 5}, orch._turn_msg
        assert 2 not in orch.kernel.outputs and 5 in orch.kernel.outputs
        # the re-run executed CELLS[1] again
        assert orch.kernel.shell.user_ns["b"] == 2
        print(
            f"✓ step() after rewind: state.messages truncated "
            f"({n_before}→{_n_assistant(orch)} assistants), turn 2 re-ran as kernel turn 5"
        )

    # ---- load: rewound flags survive the .eval round-trip ------------------
    sess2 = await Session.load("rw", tmpdir)
    await settle(50)
    orch2 = sess2.orchestrator
    assert orch2 is not None
    loaded = {u for u, e in sess2.events.items() if e.get("rewound") is True}
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
        f"✓ rewind × persist: {len(loaded)} rewound flags survived load; "
        f"resume history truncated to {resumed_assistants} assistant"
    )
    await sess2.close()


# ── 3: interrupt-and-send ──────────────────────────────────────────────────


async def _test_interrupt_and_send() -> None:
    turns: list[TurnSpec] = [
        ("sleeping", [("python", {"code": "display('before')\nawait asyncio.sleep(30)"})]),
    ]
    async with mock_orch_session(turns, max_turns=6) as (session, orch, _):
        orch.step()
        # wait until the cell task is registered and the pre-sleep display landed
        await wait_for(lambda: 1 in orch.kernel.bg)
        await wait_for(lambda: orch.kernel.outputs.get(1))
        partial_before = list(orch.kernel.outputs[1])
        assert any("'before'" in ev.text for ev in partial_before), partial_before

        # ---- interrupt-and-send via the wire dispatch --------------------------
        await _dispatch(
            session, {"t": "interrupt_and_send", "turn": 1, "text": "do X instead"}
        )
        # ``_dispatch`` releases one turn; the second generate has no tool call
        # so the loop parks — at which point ``state.messages`` is exactly the
        # second generate's input plus its assistant reply.
        await wait_for(lambda: orch.status == "paused" and _n_assistant(orch) >= 2)

        # partial outputs survived the cancel
        assert any("'before'" in ev.text for ev in orch.kernel.outputs[1]), (
            "partial display lost after interrupt"
        )

        # next generate's input: tool result carries [interrupted by user …] AND
        # the queued user message follows it
        second = orch.state.messages[:-1]  # drop the "done." assistant reply
        tool_msgs = [m for m in second if m.role == "tool"]
        assert tool_msgs, "no tool result in second generate's input"
        tool_text = tool_msgs[-1].text
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


# ── entrypoint ─────────────────────────────────────────────────────────────


async def _amain() -> None:
    await _test_rewind_and_persist()
    await _test_interrupt_and_send()
    print("\n✓ all M1 feature smokes passed")


if __name__ == "__main__":
    anyio.run(_amain)
