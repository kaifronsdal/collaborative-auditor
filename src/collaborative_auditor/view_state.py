"""Builds the ViewState dict for sending to WebSocket clients."""

from __future__ import annotations

from typing import Any

from collaborative_auditor.models import (
    Branch,
    EventType,
    Session,
    TreeIndex,
    get_branch_path,
    serialize_chat_message,
)


def _find_message_for_turn(branch: Branch, turn_id: str) -> str | None:
    """Find the assistant message with this turn_id in its metadata."""
    for msg in branch.auditor_messages:
        if msg.role == "assistant" and msg.metadata and msg.metadata.get("turn_id") == turn_id:
            return msg.id
    return None


def _compute_branch_points(
    session: Session,
    branch: Branch,
    tree: TreeIndex,
) -> list[dict[str, Any]]:
    path = get_branch_path(session, branch)
    if len(path) < 2:
        return []

    result: list[dict[str, Any]] = []

    for i, event_id in enumerate(path[:-1]):
        child_ids = tree.children.get(event_id, [])
        if len(child_ids) <= 1:
            continue

        our_child = path[i + 1]
        child_event = session.events[our_child]

        sorted_children = sorted(child_ids, key=lambda cid: session.events[cid].timestamp)
        current_index = sorted_children.index(our_child)

        branch_ids = [tree.event_to_branch[cid] for cid in sorted_children if cid in tree.event_to_branch]

        branch_type = "target" if child_event.event_type == EventType.TOOL_CALL_EXECUTED else "turn"
        message_id = _find_message_for_turn(branch, child_event.turn_id) if child_event.turn_id else None

        result.append({
            "id": event_id,
            "branch_type": branch_type,
            "event_id": event_id,
            "message_id": message_id,
            "tool_call_id": child_event.tool_call_id,
            "turn_id": child_event.turn_id,
            "current_index": current_index,
            "total_branches": len(branch_ids),
            "branch_ids": branch_ids,
        })

    return result


def build_view_state(
    session: Session,
    current_branch_index: int,
    playback_state: str,
    is_generating: bool,
    version: int,
    pending_feedback: list[str],
) -> dict[str, Any]:
    branch = session.branches[current_branch_index]
    tree = TreeIndex(session)

    return {
        "session_id": session.id,
        "initial_prompt": session.initial_prompt,
        "auditor_model": session.auditor_model,
        "target_model": session.target_model,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "current_branch": {
            "id": branch.id,
            "auditor_messages": [
                serialize_chat_message(m) for m in branch.auditor_messages
            ],
            "target_state": branch.target_state.model_dump(),
        },
        "branches": [
            {"id": b.id, "message_count": len(b.auditor_messages)}
            for b in session.branches
        ],
        "current_branch_index": current_branch_index,
        "branch_points": _compute_branch_points(session, branch, tree),
        "playback_state": playback_state,
        "is_generating": is_generating,
        "version": version,
        "pending_feedback": pending_feedback,
    }
