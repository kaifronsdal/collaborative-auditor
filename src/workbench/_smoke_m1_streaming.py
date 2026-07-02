"""M1 streaming smoke: orchestrator ``ModelEvent.output`` grows on the wire.

mockllm's ``stream_chunks=N`` mode emits N growing-prefix partial outputs via
``update_active_model_event_output`` → ``transcript()._event_updated`` →
``Session._on_event`` (``is_update=True``) → ``{"t":"update"}`` wire message.
This asserts the vertical slice: multiple ``update`` messages ship for the
same ``ModelEvent.uuid`` with strictly-growing content, so the frontend's
``assignByRole`` (which produces a fresh array per update — see
``events.ts``) can re-render the orch column incrementally.

Run:  ``uv run python -m workbench._smoke_m1_streaming``
"""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo

from workbench._smoke_util import FakeConn
from workbench.session import Session

PROSE = "The quick brown fox jumps over the lazy dog and keeps on running."


def _orch_outputs(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    del input, tools, tool_choice, config
    # No tool call → the agent loop parks after this turn (``orch.pause()``).
    return ModelOutput.from_content(model="mockllm", content=PROSE)


def _content(dumped: dict[str, Any]) -> str:
    """Pull the assistant text out of a dumped ``ModelEvent``."""
    out = dumped.get("output") or {}
    choices = out.get("choices") or []
    if not choices:
        return ""
    c = choices[0].get("message", {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):  # list[ContentText]
        return "".join(part.get("text", "") for part in c if isinstance(part, dict))
    return ""


async def _settle(n: int = 200) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def _amain() -> None:
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)

    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": _orch_outputs, "stream_chunks": 4},
        max_turns=2,
    )
    orch = session.orchestrator
    assert orch is not None

    orch.step()
    await _settle()

    # ---- the one orch ModelEvent -------------------------------------------
    model_uuids = [
        uuid
        for uuid, e in session.events.items()
        if e["event"] == "model" and session._resolve(e["span_id"]) == ("orch", "orch")
    ]
    assert len(model_uuids) == 1, f"expected one orch ModelEvent, got {model_uuids}"
    uuid = model_uuids[0]

    # ---- wire: multiple {"t":"update"} for this uuid, content grows --------
    updates = [
        m for m in conn.sent if m["t"] == "update" and m["event"]["uuid"] == uuid
    ]
    assert len(updates) >= 2, (
        f"expected ≥2 streaming updates on the wire, got {len(updates)} "
        f"(stream_chunks not reaching Session._on_event?)"
    )
    lengths = [len(_content(m["event"])) for m in updates]
    # Monotone non-decreasing (final complete() may repeat the last prefix),
    # with at least one strict growth step — i.e. the frontend sees the text
    # arrive incrementally, not in one lump.
    assert lengths == sorted(lengths), f"content shrank across updates: {lengths}"
    assert lengths[0] < lengths[-1], f"no incremental growth: {lengths}"
    assert _content(updates[-1]["event"]) == PROSE, _content(updates[-1]["event"])

    # ---- session.events[uuid] holds the latest (full) output ---------------
    assert _content(session.events[uuid]) == PROSE

    # ---- each update is a distinct dumped snapshot (not one shared ref) ----
    # If ``_condense`` returned a shared dict, every ``updates[i]["event"]``
    # would alias the final state and ``lengths`` would be flat — the growth
    # assertion above already covers this, but make the intent explicit.
    assert updates[0]["event"] is not updates[-1]["event"]

    print(
        f"✓ orchestrator ModelEvent streamed {len(updates)} wire updates, "
        f"content lengths {lengths}"
    )

    await session.close()
    print("\n✓ M1 streaming smoke passed")


if __name__ == "__main__":
    anyio.run(_amain)
