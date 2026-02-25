from __future__ import annotations

from typing import Any

from collaborative_auditor.auditor import add_researcher_feedback
from collaborative_auditor.handlers.common import push_view_state
from collaborative_auditor.session_manager import PlaybackState, SessionRuntime


async def handle_feedback(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    if runtime.is_generating:
        await runtime.cancel_generation()
        runtime.playback_state = PlaybackState.PAUSED
    add_researcher_feedback(runtime.session, runtime.current_branch, data["content"])
    await push_view_state(runtime)


async def handle_queue_feedback(
    runtime: SessionRuntime, data: dict[str, Any]
) -> None:
    runtime.pending_feedback.append(data["content"])
    await push_view_state(runtime)


async def handle_remove_queued_feedback(
    runtime: SessionRuntime, data: dict[str, Any]
) -> None:
    index = data["index"]
    if not (0 <= index < len(runtime.pending_feedback)):
        raise ValueError(
            f"Invalid feedback index {index} "
            f"(queue has {len(runtime.pending_feedback)} items)"
        )
    runtime.pending_feedback.pop(index)
    await push_view_state(runtime)


def drain_pending_feedback(runtime: SessionRuntime) -> None:
    """Move all queued feedback into the auditor conversation. Callers push state."""
    if not runtime.pending_feedback:
        return
    queue = list(runtime.pending_feedback)
    runtime.pending_feedback.clear()
    for content in queue:
        add_researcher_feedback(runtime.session, runtime.current_branch, content)
