from __future__ import annotations

import logging
from typing import Any

import jsonpatch
from fastapi import WebSocket, WebSocketDisconnect
from inspect_ai.model import ChatMessageAssistant
from inspect_ai.tool import ToolCall

from collaborative_auditor.models import Branch
from collaborative_auditor.session_manager import SessionRuntime
from collaborative_auditor.view_state import build_view_state

logger = logging.getLogger(__name__)


def find_message_event_id(runtime: SessionRuntime, message_id: str) -> str:
    """Look up event_id from a message's metadata."""
    for msg in runtime.current_branch.auditor_messages:
        if msg.id == message_id:
            event_id = (msg.metadata or {}).get("event_id")
            if event_id is None:
                raise ValueError(f"Message {message_id} has no event_id in metadata")
            return event_id
    raise ValueError(f"Message {message_id} not found in branch")


def find_tool_call(branch: Branch, tool_call_id: str) -> ToolCall:
    """Find a ToolCall by ID in the branch's assistant messages (most recent first)."""
    for msg in reversed(branch.auditor_messages):
        if isinstance(msg, ChatMessageAssistant) and msg.tool_calls:
            for tool_call in msg.tool_calls:
                if tool_call.id == tool_call_id:
                    return tool_call
    raise ValueError(f"Tool call {tool_call_id} not found in branch messages")


def find_assistant_for_turn(branch: Branch, turn_id: str) -> ChatMessageAssistant:
    """Find the assistant message for a given turn_id."""
    for msg in branch.auditor_messages:
        if isinstance(msg, ChatMessageAssistant):
            meta = msg.metadata or {}
            if meta.get("turn_id") == turn_id:
                return msg
    raise ValueError(f"Assistant message not found for turn_id={turn_id}")


async def broadcast(runtime: SessionRuntime, message: dict[str, Any]) -> None:
    dead = []
    for ws in runtime.connections:
        try:
            await ws.send_json(message)
        except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError):
            dead.append(ws)
    for ws in dead:
        runtime.connections.remove(ws)


def _build_current_view_state(runtime: SessionRuntime) -> dict[str, Any]:
    return build_view_state(
        session=runtime.session,
        current_branch_index=runtime.current_branch_index,
        playback_state=runtime.playback_state,
        is_generating=runtime.is_generating,
        version=runtime.version,
        pending_feedback=runtime.pending_feedback,
    )


async def push_view_state(
    runtime: SessionRuntime, *, save: bool = True,
) -> None:
    """Compute ViewState, diff against last sent, broadcast patch or full state.

    Set *save=False* to skip the disk write (useful in tight loops where a
    final save will follow).
    """
    runtime.version += 1
    new_state = _build_current_view_state(runtime)

    if runtime.last_sent_view_state is not None:
        patch = jsonpatch.make_patch(runtime.last_sent_view_state, new_state)
        if patch.patch:
            await broadcast(
                runtime, {"type": "patch", "ops": patch.patch, "version": runtime.version}
            )
    else:
        await broadcast(runtime, {"type": "state", "state": new_state})

    runtime.last_sent_view_state = new_state
    if save:
        await runtime.save()


async def push_full_state(runtime: SessionRuntime, ws: WebSocket) -> None:
    """Send full state to a single WebSocket (used on connect).

    Does NOT update ``last_sent_view_state`` because that baseline is shared
    across all clients and is managed exclusively by :func:`push_view_state`.
    """
    state = _build_current_view_state(runtime)
    await ws.send_json({"type": "state", "state": state})
