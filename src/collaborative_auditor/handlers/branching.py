"""Branch management handlers."""

from __future__ import annotations

import copy
import logging
from typing import Any

from inspect_ai.model import ChatMessageAssistant
from inspect_ai.tool import ToolCall

from collaborative_auditor.auditor import execute_tool_call
from collaborative_auditor.models import (
    Branch,
    EventType,
    find_event_on_path,
    find_turn_start,
    generate_id,
    track_state_changes,
)
from collaborative_auditor.handlers.common import (
    find_assistant_for_turn,
    find_message_event_id,
    push_view_state,
)
from collaborative_auditor.handlers.playback import execute_single_turn, execute_target_resample
from collaborative_auditor.session_manager import PlaybackState, SessionRuntime
from collaborative_auditor.tools import make_auditor_tools

logger = logging.getLogger(__name__)


async def replay_turn(
    runtime: SessionRuntime,
    new_branch: Branch,
    original_content: str | list,
    original_tool_calls: list[ToolCall],
    *,
    argument_overrides: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Replay a turn's tool calls on a new branch, optionally overriding arguments.

    Creates a new AUDITOR_TURN_START event with the original content, then
    replays each tool call (TOOL_CALL_ADDED + TOOL_CALL_EXECUTED), substituting
    arguments from *argument_overrides* where the tool_call_id matches.
    """
    session = runtime.session
    overrides = argument_overrides or {}
    new_turn_id = generate_id()

    with track_state_changes(
        session, new_branch,
        event_type=EventType.AUDITOR_TURN_START,
        turn_id=new_turn_id,
    ):
        assistant_msg = ChatMessageAssistant(
            id=generate_id(),
            content=copy.deepcopy(original_content),
            tool_calls=[],
            metadata={"source": "Auditor", "turn_id": new_turn_id},
        )
        new_branch.auditor_messages.append(assistant_msg)

    await push_view_state(runtime, save=False)

    assistant_idx = len(new_branch.auditor_messages) - 1
    tools = make_auditor_tools()

    for original_tool_call in original_tool_calls:
        args = overrides.get(original_tool_call.id, original_tool_call.arguments)

        new_tool_call = ToolCall(
            id=generate_id(),
            function=original_tool_call.function,
            arguments=args,
            type="function",
        )

        with track_state_changes(
            session, new_branch,
            event_type=EventType.TOOL_CALL_ADDED,
            turn_id=new_turn_id,
            tool_call_id=new_tool_call.id,
        ):
            new_branch.auditor_messages[assistant_idx].tool_calls.append(new_tool_call)

        with track_state_changes(
            session, new_branch,
            event_type=EventType.TOOL_CALL_EXECUTED,
            turn_id=new_turn_id,
            tool_call_id=new_tool_call.id,
        ):
            result = await execute_tool_call(
                session, new_branch,
                original_tool_call.function, args, new_tool_call.id,
                tools=tools,
            )
            new_branch.auditor_messages.append(result)

        await push_view_state(runtime, save=False)

    await push_view_state(runtime)


async def handle_branch(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    message_id = data.get("message_id")
    if message_id is None:
        raise ValueError("message_id is required for branching")

    event_id = find_message_event_id(runtime, message_id)
    runtime.fork_to_new_branch(event_id)
    await push_view_state(runtime)


async def handle_switch_branch(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    branch_id = data["branch_id"]

    for i, branch in enumerate(runtime.session.branches):
        if branch.id == branch_id:
            runtime.current_branch_index = i
            runtime.last_sent_view_state = None
            await push_view_state(runtime)
            return

    raise ValueError(f"Branch not found: {branch_id}")


async def handle_resample_turn(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    session = runtime.session
    turn_id = data["turn_id"]

    turn_start = find_turn_start(session, runtime.current_branch, turn_id)
    if turn_start is None:
        raise ValueError(f"Turn not found: {turn_id}")

    if turn_start.parent_id is None:
        raise ValueError("Cannot resample: turn has no parent event")

    runtime.fork_to_new_branch(turn_start.parent_id)

    resample_id = generate_id()
    runtime.playback_state = PlaybackState.STEPPING
    await push_view_state(runtime)

    runtime.launch_generation(execute_single_turn(runtime, resample_id=resample_id))


async def handle_edit_tool_call(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    """Turn-level replay: fork before the turn, replay all tool calls with the
    edited one's arguments replaced, re-executing each.
    """
    session = runtime.session
    branch = runtime.current_branch
    tool_call_id = data["tool_call_id"]

    tool_call_event = find_event_on_path(
        session, branch,
        event_type=EventType.TOOL_CALL_ADDED,
        tool_call_id=tool_call_id,
    )
    if tool_call_event is None:
        raise ValueError(f"Tool call not found: {tool_call_id}")

    turn_start = find_turn_start(session, branch, tool_call_event.turn_id)
    if turn_start is None:
        raise ValueError(f"Turn start not found for turn_id={tool_call_event.turn_id}")

    if turn_start.parent_id is None:
        raise ValueError("Cannot edit tool call: turn has no parent event")

    original_assistant = find_assistant_for_turn(branch, tool_call_event.turn_id)
    new_branch = runtime.fork_to_new_branch(turn_start.parent_id)

    await replay_turn(
        runtime, new_branch,
        original_content=original_assistant.content,
        original_tool_calls=original_assistant.tool_calls or [],
        argument_overrides={tool_call_id: data["new_arguments"]},
    )


async def handle_resample_target_response(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    session = runtime.session

    tool_call_id = data.get("tool_call_id")
    if tool_call_id is None:
        raise ValueError("tool_call_id is required for resampling target response")

    tool_call_event = find_event_on_path(
        session, runtime.current_branch,
        event_type=EventType.TOOL_CALL_ADDED,
        tool_call_id=tool_call_id,
    )
    if tool_call_event is None:
        raise ValueError(f"Tool call event not found: {tool_call_id}")

    runtime.fork_to_new_branch(tool_call_event.id)

    runtime.playback_state = PlaybackState.STEPPING
    await push_view_state(runtime)

    runtime.launch_generation(execute_target_resample(runtime, tool_call_id))
