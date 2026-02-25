from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool

from collaborative_auditor.auditor import close_pending_tool_calls, execute_auditor_turn
from collaborative_auditor.handlers.common import broadcast, push_view_state
from collaborative_auditor.handlers.feedback import drain_pending_feedback
from collaborative_auditor.models import EventType, generate_id, track_state_changes
from collaborative_auditor.session_manager import PlaybackState, SessionRuntime
from collaborative_auditor.tools import (
    call_target_model,
    create_cache_policy,
    format_target_response,
)

logger = logging.getLogger(__name__)
MAX_EMPTY_TURNS = 3


@asynccontextmanager
async def managed_generation(
    runtime: SessionRuntime,
) -> AsyncGenerator[anyio.CancelScope]:
    """Manage a generation lifecycle: scope setup, teardown, and final state push.

    Sets ``runtime.generation_scope`` on entry and clears both ``generation_scope``
    and ``generation_task`` on exit (regardless of success/failure).  Always
    pushes view state on exit so clients see the final playback state.
    """
    scope = anyio.CancelScope()
    runtime.generation_scope = scope
    try:
        with scope:
            yield scope
    finally:
        runtime.generation_scope = None
        runtime.generation_task = None
        try:
            close_pending_tool_calls(runtime.session, runtime.current_branch)
            await push_view_state(runtime)
        except Exception:
            pass


async def handle_play(runtime: SessionRuntime) -> None:
    runtime.playback_state = PlaybackState.PLAYING
    await push_view_state(runtime)
    runtime.launch_generation(generation_loop(runtime))


async def handle_pause(runtime: SessionRuntime) -> None:
    await runtime.cancel_generation()
    runtime.playback_state = PlaybackState.PAUSED
    drain_pending_feedback(runtime)
    await push_view_state(runtime)


async def handle_step(runtime: SessionRuntime) -> None:
    runtime.playback_state = PlaybackState.STEPPING
    await push_view_state(runtime)
    runtime.launch_generation(execute_single_turn(runtime))


async def _on_event(runtime: SessionRuntime) -> None:
    await push_view_state(runtime)


async def generation_loop(runtime: SessionRuntime) -> None:
    consecutive_empty = 0
    async with managed_generation(runtime) as scope:
        while runtime.playback_state == PlaybackState.PLAYING:
            branch = runtime.current_branch
            msg_count_before = len(branch.auditor_messages)
            try:
                should_continue = await execute_auditor_turn(
                    runtime.session, branch, lambda: _on_event(runtime),
                )
            except Exception as e:
                logger.error(f"Error in generation loop: {e}", exc_info=True)
                runtime.playback_state = PlaybackState.PAUSED
                await broadcast(runtime, {"type": "error", "message": str(e)})
                break
            if not should_continue:
                runtime.playback_state = PlaybackState.IDLE
                break
            new_msgs = branch.auditor_messages[msg_count_before:]
            has_content = any(
                isinstance(m, ChatMessageAssistant)
                and (m.tool_calls or len(str(m.content)) > 10)
                for m in new_msgs
            )
            if has_content:
                consecutive_empty = 0
            else:
                consecutive_empty += 1
            if consecutive_empty >= MAX_EMPTY_TURNS:
                runtime.playback_state = PlaybackState.PAUSED
                await broadcast(
                    runtime,
                    {
                        "type": "error",
                        "message": f"Auditor paused: {MAX_EMPTY_TURNS} empty turns",
                    },
                )
                break
            drain_pending_feedback(runtime)
        if scope.cancelled_caught:
            runtime.playback_state = PlaybackState.PAUSED


async def execute_target_resample(
    runtime: SessionRuntime,
    tool_call_id: str,
) -> None:
    try:
        async with managed_generation(runtime):
            session = runtime.session
            branch = runtime.current_branch
            cache_policy = create_cache_policy(
                session_id=session.id,
                resample_id=generate_id(),
                expiry="1D",
            )

            with track_state_changes(
                session, branch,
                event_type=EventType.TOOL_CALL_EXECUTED,
                tool_call_id=tool_call_id,
            ):
                response = await call_target_model(
                    branch.target_state.messages,
                    branch.target_state.tools,
                    session.target_model,
                    cache_policy=cache_policy,
                )
                branch.target_state.messages.append(response)
                message_index = len(branch.target_state.messages) - 1
                formatted = format_target_response(response, message_index)
                tool_result = ChatMessageTool(
                    id=generate_id(),
                    content=formatted,
                    tool_call_id=tool_call_id,
                    function="query_target",
                    metadata={"source": "System"},
                )
                branch.auditor_messages.append(tool_result)

        runtime.playback_state = PlaybackState.PAUSED
    except Exception as e:
        logger.error(f"Error resampling target: {e}", exc_info=True)
        runtime.playback_state = PlaybackState.PAUSED
        await broadcast(runtime, {"type": "error", "message": str(e)})


async def execute_single_turn(
    runtime: SessionRuntime,
    resample_id: str | None = None,
) -> None:
    try:
        async with managed_generation(runtime) as scope:
            should_continue = await execute_auditor_turn(
                runtime.session, runtime.current_branch,
                lambda: _on_event(runtime),
                resample_id=resample_id,
            )
            if scope.cancelled_caught:
                runtime.playback_state = PlaybackState.PAUSED
            elif not should_continue:
                runtime.playback_state = PlaybackState.IDLE
            else:
                runtime.playback_state = PlaybackState.PAUSED
    except Exception as e:
        logger.error(f"Error in single turn: {e}", exc_info=True)
        runtime.playback_state = PlaybackState.PAUSED
        await broadcast(runtime, {"type": "error", "message": str(e)})
