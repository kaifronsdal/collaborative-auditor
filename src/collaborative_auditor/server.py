"""FastAPI server for the Collaborative Auditor Interface.

This module provides the WebSocket-based API for real-time collaboration
between researchers and the auditor agent.

Protocol: Server-authoritative state push model.
- Server -> Client: state, delta_turn_start, delta_tool_call, delta_tool_result, error
- Client -> Server: start_session, play, pause, step, feedback, branch, switch_branch,
                    edit_message, resample_turn, edit_tool_call, resample_target_response
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    get_model,
)
from inspect_ai.tool import ToolCall

# Load environment variables from .env file
# Search for .env in the project root (up to 3 levels up from this file)
_env_file = Path(__file__).parent.parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
    logging.info(f"Loaded environment from {_env_file}")
else:
    # Also try current working directory
    load_dotenv()

from collaborative_auditor.auditor import (
    AUDITOR_SYSTEM_PROMPT,
    _execute_tool_call,
    add_researcher_feedback,
    execute_auditor_turn,
    initialize_auditor_messages,
)
from collaborative_auditor.models import (
    BranchPoint,
    BranchPointType,
    EventType,
    ResearcherBranch,
    Session,
    create_branch,
    create_session,
    generate_id,
    reconstruct_at_event,
    serialize_chat_message,
    track_state_changes,
)
from collaborative_auditor.tools import (
    create_cache_policy,
    execute_query_target,
)

logger = logging.getLogger(__name__)

# In-memory session storage (for now, no persistence)
sessions: dict[str, Session] = {}

# Active WebSocket connections per session
connections: dict[str, list[WebSocket]] = {}

# Playback state per session
playback_states: dict[str, str] = {}  # "idle" | "playing" | "stepping" | "paused"

# Cancellation tokens for stopping generation
cancel_tokens: dict[str, asyncio.Event] = {}

# Monotonic version counter per session (for ordering delta messages)
session_versions: dict[str, int] = {}

# Active generation tasks per session (to prevent race conditions)
generation_tasks: dict[str, asyncio.Task] = {}

# Per-session locks to serialize all mutations (prevents race conditions)
session_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Starting Collaborative Auditor server")
    yield
    logger.info("Shutting down Collaborative Auditor server")


app = FastAPI(
    title="Collaborative Auditor Interface",
    description="Real-time collaborative audit interface for AI safety research",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _find_ancestor_index(
    bp: BranchPoint,
    current_branch: ResearcherBranch,
    session: Session,
) -> int:
    """Find which branch in a branch point's branch_ids is the current branch's ancestor.

    When the current branch is not directly in branch_ids (it's a descendant),
    we determine which listed branch it descends from by counting how many
    message IDs AFTER the branch point are shared with the current branch.
    The true ancestor will share more post-branch-point messages because the
    current branch inherited (deep-copied) those messages from it.

    Returns the index into bp.branch_ids, or 0 as fallback.
    """
    current_msg_ids = {m.id for m in current_branch.auditor_messages}
    bp_msg_id = bp.message_id

    best_idx = 0
    best_overlap = -1

    for i, bid in enumerate(bp.branch_ids):
        candidate = None
        for b in session.branches:
            if b.id == bid:
                candidate = b
                break
        if candidate is None:
            continue

        # Count how many messages AFTER the branch point message are shared
        # with the current branch.  The true ancestor will share more because
        # the current branch was forked from it at a later point and inherited
        # (deep-copied) its messages.
        past_bp = False
        overlap = 0
        for m in candidate.auditor_messages:
            if bp_msg_id and m.id == bp_msg_id:
                past_bp = True
                continue
            if past_bp and m.id in current_msg_ids:
                overlap += 1

        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = i

    if best_overlap <= 0:
        raise ValueError(
            f"Could not find ancestor branch for branch point "
            f"event_id={bp.event_id}, message_id={bp.message_id}, "
            f"branch_ids={bp.branch_ids}. "
            f"Current branch {current_branch.id} is not a descendant of any listed branch."
        )

    return best_idx


def build_view_state(session_id: str) -> dict[str, Any]:
    """Build the complete ViewState for sending to clients.

    This is the single source of truth - sent on every non-streaming state change.
    Only the current branch's messages are included (not all branches).
    Branch points are pre-computed for the current branch.
    """
    session = sessions[session_id]
    branch = session.current_branch()

    # Pre-compute branch points visible on the current branch.
    # A branch point is visible if:
    # 1. The current branch is explicitly in branch_ids (direct participant), OR
    # 2. The branch point's message_id exists in our messages (inherited from ancestor)
    current_msg_ids = {m.id for m in branch.auditor_messages}
    current_tc_ids = {
        tc.id
        for m in branch.auditor_messages
        if m.role == "assistant" and m.tool_calls
        for tc in m.tool_calls
    }

    computed_branch_points = []
    for bp in session.branch_points:
        # Determine if this branch point is visible on the current branch
        if branch.id in bp.branch_ids:
            # Direct participant
            current_idx = bp.branch_ids.index(branch.id)
        elif bp.message_id and bp.message_id in current_msg_ids:
            # The branch point's anchor message is in our history (inherited from ancestor).
            # Find our closest ancestor in the branch_ids list.
            current_idx = _find_ancestor_index(bp, branch, session)
        elif bp.tool_call_id and bp.tool_call_id in current_tc_ids:
            # The branch point's tool call is in our history (inherited from ancestor)
            current_idx = _find_ancestor_index(bp, branch, session)
        else:
            continue  # Not relevant to current branch

        # Resolve message_id for display. For TURN branch points, the stored
        # message_id is the last message BEFORE the divergent turn. We want the
        # indicator on the FIRST DIVERGENT assistant message -- except when the
        # anchor message itself IS the divergent content (e.g., edit_initial_prompt
        # modifies the user message in-place, so the indicator should stay there).
        #
        # Rule: if the branch point has a turn_id (meaning it's a turn resample,
        # not an initial prompt edit), resolve forward to the next assistant message.
        # This handles both tool result anchors (invisible) and user message anchors
        # (researcher feedback between turns).
        resolved_message_id = bp.message_id
        if bp.branch_type == BranchPointType.TURN and bp.message_id and bp.turn_id:
            # This is a turn resample -- resolve to the first assistant message after the anchor
            found_anchor = False
            for msg in branch.auditor_messages:
                if found_anchor and msg.role == "assistant":
                    resolved_message_id = msg.id
                    break
                if msg.id == bp.message_id:
                    found_anchor = True

        # Resolve tool_call_id for the current branch. When a tool call is edited,
        # the new branch has a different tool call ID than the original stored in
        # the branch point. Find the equivalent tool call on the current branch.
        resolved_tc_id = bp.tool_call_id
        if bp.tool_call_id and bp.branch_type in (BranchPointType.TOOL_CALL, BranchPointType.TARGET_RESPONSE):
            if bp.tool_call_id not in current_tc_ids:
                # Original tc ID not on this branch -- find the replacement.
                # Use the branch's event history: the first TOOL_CALL_ADDED event
                # after the branch point event is the replacement tool call.
                found_bp_event = False
                for event in branch.events:
                    if event.id == bp.event_id:
                        found_bp_event = True
                        continue
                    if found_bp_event and event.event_type == EventType.TOOL_CALL_ADDED and event.tool_call_id:
                        resolved_tc_id = event.tool_call_id
                        break
                else:
                    # Fallback: search messages for the last assistant's last tool call
                    logger.warning(
                        f"Could not find replacement tool call after branch point event "
                        f"{bp.event_id}; falling back to last tool call (may be incorrect)"
                    )
                    for msg in reversed(branch.auditor_messages):
                        if msg.role == "assistant" and msg.tool_calls:
                            resolved_tc_id = msg.tool_calls[-1].id
                            break

        computed_branch_points.append({
            "id": bp.id,
            "branch_type": bp.branch_type.value,
            "event_id": bp.event_id,
            "message_id": resolved_message_id,
            "tool_call_id": resolved_tc_id,
            "turn_id": bp.turn_id,
            "current_index": current_idx,
            "total_branches": len(bp.branch_ids),
            "branch_ids": bp.branch_ids,
        })

    return {
        "session_id": session.id,
        "initial_prompt": session.initial_prompt,
        "auditor_model": session.auditor_model,
        "target_model": session.target_model,
        "current_branch": {
            "id": branch.id,
            "auditor_messages": [serialize_chat_message(m) for m in branch.auditor_messages],
            "target_state": branch.target_state.model_dump(mode="json"),
        },
        "branches": [
            {"id": b.id, "message_count": len(b.auditor_messages)}
            for b in session.branches
        ],
        "current_branch_index": session.current_branch_index,
        "branch_points": computed_branch_points,
        "playback_state": playback_states[session_id],
        "is_generating": session_id in generation_tasks and not generation_tasks[session_id].done(),
        "version": session_versions[session_id],
    }


def get_next_version(session_id: str) -> int:
    """Get and increment the version counter for a session."""
    if session_id not in session_versions:
        session_versions[session_id] = 0
    version = session_versions[session_id]
    session_versions[session_id] += 1
    return version


async def push_view_state(session_id: str) -> None:
    """Push the current ViewState to all connected clients.

    Increments the version counter so that subsequent delta messages
    will have a strictly higher version and won't be dropped by the
    client's stale-delta check (version <= last_seen_version).
    """
    if session_id not in sessions:
        raise RuntimeError(f"push_view_state called for non-existent session: {session_id}")
    # Bump the version counter BEFORE building state so the state
    # carries this version, and the next get_next_version() call
    # returns a strictly higher value.
    get_next_version(session_id)
    state = build_view_state(session_id)
    await broadcast_to_session(session_id, {"type": "state", "state": state})


async def broadcast_to_session(session_id: str, message: dict[str, Any]) -> None:
    """Broadcast a message to all connections for a session."""
    if session_id in connections:
        dead_connections = []
        for ws in connections[session_id]:
            try:
                await ws.send_json(message)
            except (WebSocketDisconnect, ConnectionError, OSError) as exc:
                dead_connections.append(ws)
            except RuntimeError as exc:
                # WebSocket libraries raise RuntimeError for closed connections
                if "close" in str(exc).lower() or "websocket" in str(exc).lower():
                    dead_connections.append(ws)
                else:
                    raise
        for ws in dead_connections:
            connections[session_id].remove(ws)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time communication.

    On connect, sends the full ViewState if a session exists.
    """
    await websocket.accept()

    # Register connection
    if session_id not in connections:
        connections[session_id] = []
    connections[session_id].append(websocket)

    # Initialize cancellation token if needed
    if session_id not in cancel_tokens:
        cancel_tokens[session_id] = asyncio.Event()

    # Initialize playback state if needed
    if session_id not in playback_states:
        playback_states[session_id] = "idle"

    # Initialize session lock if needed
    if session_id not in session_locks:
        session_locks[session_id] = asyncio.Lock()

    # Initialize session version if needed
    if session_id not in session_versions:
        session_versions[session_id] = 0

    try:
        # If session exists, send full state to the connecting client
        if session_id in sessions:
            state = build_view_state(session_id)
            await websocket.send_json({"type": "state", "state": state})

        while True:
            data = await websocket.receive_json()
            await handle_client_message(websocket, session_id, data)

    except (WebSocketDisconnect, ConnectionError, OSError):
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"Unexpected WebSocket error for session {session_id}: {e}", exc_info=True)
    finally:
        # Unregister connection
        if session_id in connections and websocket in connections[session_id]:
            connections[session_id].remove(websocket)

        # If all clients disconnected, cancel any running generation task
        if session_id in connections and len(connections[session_id]) == 0:
            if session_id in generation_tasks:
                generation_tasks[session_id].cancel()
                try:
                    await generation_tasks[session_id]
                except asyncio.CancelledError:
                    pass
                if session_id in generation_tasks:
                    del generation_tasks[session_id]


async def handle_client_message(
    websocket: WebSocket,
    session_id: str,
    data: dict[str, Any],
) -> None:
    """Handle a message from the client."""
    msg_type = data.get("type")
    if msg_type is None:
        raise ValueError("Missing required 'type' field in client message")
    logger.info(f"[handle_client_message] Received type={msg_type} for session {session_id[:8]}...")

    if session_id not in session_locks:
        session_locks[session_id] = asyncio.Lock()
    async with session_locks[session_id]:
        logger.info(f"[handle_client_message] Acquired lock for type={msg_type}")
        try:
            if msg_type == "start_session":
                await handle_start_session(websocket, session_id, data)
            elif msg_type == "play":
                await handle_play(session_id)
            elif msg_type == "pause":
                await handle_pause(session_id)
            elif msg_type == "step":
                await handle_step(session_id)
            elif msg_type == "feedback":
                await handle_feedback(session_id, data)
            elif msg_type == "branch":
                await handle_branch(session_id, data)
            elif msg_type == "switch_branch":
                await handle_switch_branch(session_id, data)
            elif msg_type == "edit_message":
                await handle_edit_message(session_id, data)
            elif msg_type == "edit_initial_prompt":
                await handle_edit_initial_prompt(session_id, data)
            elif msg_type == "resample_turn":
                await handle_resample_turn(session_id, data)
            elif msg_type == "edit_tool_call":
                await handle_edit_tool_call(session_id, data)
            elif msg_type == "rewrite_tool_call":
                await handle_rewrite_tool_call(websocket, session_id, data)
            elif msg_type == "resample_target_response":
                await handle_resample_target_response(session_id, data)
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}",
                })
        except (ValueError, KeyError) as e:
            logger.error(f"Error handling message type={msg_type}: {e}", exc_info=True)
            await websocket.send_json({
                "type": "error",
                "message": f"Error in {msg_type}: {str(e)}",
            })


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from model output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Rewrite model did not return a valid JSON object")


def _flatten_model_content(content: str | list[Any]) -> str:
    """Convert model content to plain text for JSON parsing."""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for item in content:
        if isinstance(item, ContentText):
            parts.append(item.text)
        elif isinstance(item, ContentReasoning):
            if item.redacted:
                if item.summary:
                    parts.append(item.summary)
            else:
                parts.append(item.reasoning)
    return "\n".join(parts).strip()


async def _generate_rewritten_arguments(
    session: Session,
    context_messages: list[Any],
    tool_name: str,
    original_arguments: dict[str, Any],
    instruction: str,
    selected_text: str | None = None,
    target_field: str | None = None,
) -> dict[str, Any]:
    """Use the auditor model to rewrite tool-call arguments as JSON."""
    rewrite_model = get_model(session.auditor_model)
    cache_policy = create_cache_policy(
        session_id=session.id,
        resample_id=generate_id(),  # Force fresh rewrite output
        expiry="1D",
    )

    selected_text_block = (
        f"\nSelected text from current tool call:\n{selected_text}\n"
        if selected_text and selected_text.strip()
        else ""
    )
    target_field_block = f"\nPreferred field to edit first: {target_field}\n" if target_field else ""
    prompt = (
        "Rewrite the arguments for an existing auditor tool call.\n"
        "Return ONLY a JSON object with the rewritten arguments (no markdown, no code fences).\n"
        "Preserve the same top-level schema and required fields for this tool.\n\n"
        f"Tool name: {tool_name}\n"
        f"Original arguments JSON:\n{json.dumps(original_arguments, indent=2)}\n"
        f"{target_field_block}"
        f"{selected_text_block}"
        f"\nRewrite instruction:\n{instruction.strip()}\n"
    )

    response = await rewrite_model.generate(
        input=[
            *context_messages,
            ChatMessageUser(
                id=generate_id(),
                content=prompt,
                metadata={"source": "Researcher"},
            ),
        ],
        config=GenerateConfig(cache=cache_policy, max_tokens=4096),
    )

    content_text = _flatten_model_content(response.message.content)
    if not content_text:
        raise ValueError("Rewrite model returned empty content")

    return _extract_json_object(content_text)


async def handle_start_session(
    websocket: WebSocket,
    session_id: str,
    data: dict[str, Any],
) -> None:
    """Handle session creation request.

    If the session already exists, just push the existing state instead of overwriting.
    """
    if session_id in sessions:
        # Session already exists - just send state
        await push_view_state(session_id)
        return

    session = create_session(
        initial_prompt=data["initial_prompt"],
        auditor_model=data["auditor_model"],
        target_model=data["target_model"],
    )

    # Override the session ID with the one from the URL
    session.id = session_id

    # Initialize auditor messages
    initialize_auditor_messages(session)

    sessions[session_id] = session
    playback_states[session_id] = "idle"
    session_versions[session_id] = 0

    await push_view_state(session_id)


async def handle_play(session_id: str) -> None:
    """Handle play request - continue generation until stopped."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    # Check if already generating
    if session_id in generation_tasks:
        task = generation_tasks[session_id]
        if not task.done():
            return  # Already playing, ignore duplicate request

    # Cancel any existing task (in case it's done but not cleaned up)
    if session_id in generation_tasks:
        generation_tasks[session_id].cancel()
        try:
            await generation_tasks[session_id]
        except asyncio.CancelledError:
            pass

    playback_states[session_id] = "playing"
    cancel_tokens[session_id].clear()

    await push_view_state(session_id)

    # Create and track the task
    task = asyncio.create_task(_generation_loop(session_id))
    generation_tasks[session_id] = task


async def handle_pause(session_id: str) -> None:
    """Handle pause request - stop generation."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    playback_states[session_id] = "paused"

    # Set cancel token if it exists
    if session_id in cancel_tokens:
        cancel_tokens[session_id].set()

    # Cancel the generation task if it exists
    if session_id in generation_tasks:
        task = generation_tasks.pop(session_id)  # Atomically remove to prevent race condition
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await push_view_state(session_id)


async def handle_step(session_id: str) -> None:
    """Handle step request - execute one turn then pause."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    # Check if already generating
    if session_id in generation_tasks:
        task = generation_tasks[session_id]
        if not task.done():
            return  # Already running, ignore duplicate request

    # Cancel any existing task (in case it's done but not cleaned up)
    if session_id in generation_tasks:
        generation_tasks[session_id].cancel()
        try:
            await generation_tasks[session_id]
        except asyncio.CancelledError:
            pass

    playback_states[session_id] = "stepping"
    cancel_tokens[session_id].clear()

    await push_view_state(session_id)

    # Create and track the task
    task = asyncio.create_task(_execute_single_turn(session_id))
    generation_tasks[session_id] = task


async def handle_feedback(session_id: str, data: dict[str, Any]) -> None:
    """Handle researcher feedback."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    content = data["content"]

    add_researcher_feedback(session, content)

    await push_view_state(session_id)


async def handle_branch(session_id: str, data: dict[str, Any]) -> None:
    """Handle branch creation request.

    Supports both at_event_index (integer) and message_id (string) for specifying
    the branch point.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    current_branch = session.current_branch()

    # Determine at_event_index from either direct value or message_id
    message_id = data.get("message_id")
    if message_id is not None:
        # Search for the event that added this message.
        # Branching should INCLUDE the selected message itself, so branch at
        # that exact event index (not the previous one).
        at_event_index = None
        for i, event in enumerate(current_branch.events):
            for patch in event.auditor_patches:
                if patch.get("op") == "add":
                    value = patch.get("value", {})
                    if isinstance(value, dict) and value.get("id") == message_id:
                        at_event_index = i
                        break
            if at_event_index is not None:
                break

        if at_event_index is None:
            raise ValueError(f"Message {message_id} not found in event history")
    else:
        at_event_index = data["at_event_index"]

    logger.info(f"[handle_branch] at_event_index={at_event_index}, source_branch has {len(current_branch.events)} events, {len(current_branch.auditor_messages)} messages")
    new_branch = create_branch(session, current_branch.id, at_event_index, branch_type=BranchPointType.TURN)
    logger.info(f"[handle_branch] new_branch has {len(new_branch.events)} events, {len(new_branch.auditor_messages)} messages")

    # Switch to the new branch
    session.current_branch_index = len(session.branches) - 1

    await push_view_state(session_id)


async def handle_switch_branch(session_id: str, data: dict[str, Any]) -> None:
    """Handle branch switch request."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    branch_id = data["branch_id"]

    # Find branch index - fail loudly if not found
    branch_index = None
    for i, branch in enumerate(session.branches):
        if branch.id == branch_id:
            branch_index = i
            break

    if branch_index is None:
        raise ValueError(f"Branch not found: {branch_id}")

    session.current_branch_index = branch_index

    await push_view_state(session_id)


async def handle_edit_message(session_id: str, data: dict[str, Any]) -> None:
    """Handle message edit request.

    Editing creates a new branch from before that message with the edited content.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    message_id = data["message_id"]
    new_content = data["new_content"]

    branch = session.current_branch()

    # Find the event index for this message
    # We need to find the event that added this message
    event_index = None
    for i, event in enumerate(branch.events):
        for patch in event.auditor_patches:
            if patch.get("op") == "add":
                value = patch.get("value", {})
                if isinstance(value, dict) and value.get("id") == message_id:
                    event_index = max(0, i - 1)
                    break
        if event_index is not None:
            break

    if event_index is None:
        raise ValueError(f"Message {message_id} not found in event history")

    # Create new branch from before the edited message
    new_branch = create_branch(session, branch.id, event_index, branch_type=BranchPointType.TURN)
    session.current_branch_index = len(session.branches) - 1

    # Add the edited message
    with track_state_changes(new_branch, event_type=EventType.RESEARCHER_MESSAGE):
        new_branch.auditor_messages.append(
            ChatMessageUser(
                id=generate_id(),
                content=new_content,
                metadata={"source": "Researcher", "edited": True},
            )
        )

    await push_view_state(session_id)


async def handle_edit_initial_prompt(session_id: str, data: dict[str, Any]) -> None:
    """Handle editing the initial researcher instructions.

    This creates a new branch from the very beginning with updated instructions.
    The system prompt and user message are re-generated with the new prompt.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    new_prompt = data["new_content"]

    # Create a new branch from event 0 (the system init)
    branch = session.current_branch()
    new_branch = create_branch(session, branch.id, at_event_index=0)
    session.current_branch_index = len(session.branches) - 1

    # Re-initialize with the new prompt by replacing the auditor messages
    # Update the session's initial_prompt for future reference
    session.initial_prompt = new_prompt

    system_prompt = AUDITOR_SYSTEM_PROMPT.format(
        initial_prompt=new_prompt,
        today_date=datetime.now().strftime("%Y-%m-%d"),
        target_model=session.target_model,
    )

    with track_state_changes(new_branch):
        # Modify content in-place (keeps same message IDs so the branch point works)
        new_branch.auditor_messages[0].content = system_prompt
        new_branch.auditor_messages[1].content = (
            f"Please begin investigating the target model. "
            f"The researcher's instructions are:\n\n{new_prompt}"
        )

    await push_view_state(session_id)


async def handle_resample_turn(session_id: str, data: dict[str, Any]) -> None:
    """Handle resampling an entire auditor turn (like Claude.ai regenerate).

    This creates a new branch and immediately executes a fresh auditor turn,
    bypassing the cache to ensure a new response.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    # Cancel any running generation to prevent concurrent mutation
    if session_id in generation_tasks:
        task = generation_tasks.pop(session_id)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    session = sessions[session_id]
    turn_id = data["turn_id"]
    branch = session.current_branch()

    logger.debug(
        f"[handle_resample_turn] turn_id={turn_id}, branch_id={branch.id}, "
        f"num_events={len(branch.events)}, current_branch_index={session.current_branch_index}"
    )

    # Find the AUDITOR_TURN_START event with this turn_id
    turn_start_index = None
    for i, event in enumerate(branch.events):
        logger.debug(
            f"  Event {i}: type={event.event_type}, turn_id={event.turn_id}, id={event.id}"
        )
        if event.turn_id == turn_id and event.event_type == EventType.AUDITOR_TURN_START:
            turn_start_index = i
            break

    if turn_start_index is None:
        # Log all available turn_ids for debugging
        available_turn_ids = {e.turn_id for e in branch.events if e.turn_id}
        logger.error(
            f"Turn not found: {turn_id}. Available turn_ids: {available_turn_ids}. "
            f"Branch {branch.id} has {len(branch.events)} events.",
            exc_info=True,
        )
        raise ValueError(f"Turn not found: {turn_id}")

    # Branch from just BEFORE the turn started
    at_event_index = max(0, turn_start_index - 1)

    new_branch = create_branch(
        session, branch.id, at_event_index,
        branch_type=BranchPointType.TURN,
        turn_id=turn_id,
    )

    session.current_branch_index = len(session.branches) - 1

    # Automatically execute a new auditor turn with cache bypass
    resample_id = generate_id()
    playback_states[session_id] = "stepping"

    await push_view_state(session_id)

    on_event = _make_on_event(session_id)

    try:
        logger.debug(
            f"[handle_resample_turn] Executing new turn, branch now has {len(new_branch.events)} events"
        )
        should_continue = await execute_auditor_turn(
            session, on_event, resample_id=resample_id
        )
        logger.debug(
            f"[handle_resample_turn] Turn complete, branch now has {len(new_branch.events)} events. "
            f"Turn IDs in events: {[e.turn_id for e in new_branch.events if e.event_type == EventType.AUDITOR_TURN_START]}"
        )

        if not should_continue:
            playback_states[session_id] = "idle"
        else:
            playback_states[session_id] = "paused"

        await push_view_state(session_id)

    except (ValueError, KeyError) as e:
        logger.error(f"Error executing resampled turn: {e}", exc_info=True)
        playback_states[session_id] = "paused"
        await broadcast_to_session(session_id, {
            "type": "error",
            "message": f"Error in resampled turn: {str(e)}",
        })
        await push_view_state(session_id)


async def handle_edit_tool_call(session_id: str, data: dict[str, Any]) -> None:
    """Handle editing a tool call (modifying arguments and re-executing).

    The branch indicator is placed on the entire auditor message (TURN level),
    not on the individual tool call.  This means that if the user first resamples
    a turn and then edits one of its tool calls, both operations share the same
    anchor event and merge into a single branch point on the assistant message.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    tool_call_id = data["tool_call_id"]
    new_arguments = data["new_arguments"]
    branch = session.current_branch()

    # Find the TOOL_CALL_ADDED event with this tool_call_id and its turn_id
    tool_call_event_index = None
    tool_call_turn_id = None
    for i, event in enumerate(branch.events):
        if event.tool_call_id == tool_call_id and event.event_type == EventType.TOOL_CALL_ADDED:
            tool_call_event_index = i
            tool_call_turn_id = event.turn_id
            break

    if tool_call_event_index is None:
        raise ValueError(f"Tool call not found: {tool_call_id}")

    # Find the original tool call BEFORE branching (it won't exist in the new branch
    # since we branch from before it was added)
    original_tc = None
    for msg in reversed(branch.auditor_messages):
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.id == tool_call_id:
                    original_tc = tc
                    break
            if original_tc:
                break

    if not original_tc:
        raise ValueError(f"Tool call {tool_call_id} not found in current branch messages")

    # Find the AUDITOR_TURN_START event for the turn containing this tool call
    turn_start_index = None
    for i, event in enumerate(branch.events):
        if event.turn_id == tool_call_turn_id and event.event_type == EventType.AUDITOR_TURN_START:
            turn_start_index = i
            break

    if turn_start_index is None:
        raise ValueError(
            f"AUDITOR_TURN_START not found for turn_id={tool_call_turn_id} "
            f"(tool_call_id={tool_call_id})"
        )

    # Branch from just BEFORE the turn started — same anchor as handle_resample_turn.
    # This ensures that turn resamples and tool call edits on the same turn merge
    # into a single branch point on the assistant message.
    anchor_event_index = max(0, turn_start_index - 1)

    new_branch = create_branch(
        session, branch.id, anchor_event_index,
        branch_type=BranchPointType.TURN,
        turn_id=tool_call_turn_id,
    )

    # Fast-forward: copy events and state from turn start up to (but not including)
    # the edited tool call.  This preserves the assistant message and any prior
    # tool calls in the same turn.
    events_to_replay = branch.events[anchor_event_index + 1 : tool_call_event_index]
    if events_to_replay:
        full_msgs, full_target = reconstruct_at_event(branch, tool_call_event_index - 1)
        new_branch.auditor_messages = full_msgs
        new_branch.target_state = full_target
        new_branch.events.extend(copy.deepcopy(events_to_replay))

    session.current_branch_index = len(session.branches) - 1

    # Create modified tool call with new arguments
    modified_tc = ToolCall(
        id=generate_id(),
        function=original_tc.function,
        arguments=new_arguments,
        type="function",
    )

    # Find the last assistant message to add the tool call to
    assistant_idx = None
    for i in range(len(new_branch.auditor_messages) - 1, -1, -1):
        if new_branch.auditor_messages[i].role == "assistant":
            assistant_idx = i
            break

    if assistant_idx is None:
        raise ValueError("No assistant message found in branch")

    # Add the modified tool call
    with track_state_changes(new_branch, event_type=EventType.TOOL_CALL_ADDED, tool_call_id=modified_tc.id):
        if new_branch.auditor_messages[assistant_idx].tool_calls is None:
            raise ValueError(
                "Assistant message has tool_calls=None — state reconstruction may be broken"
            )
        new_branch.auditor_messages[assistant_idx].tool_calls.append(modified_tc)

    # Execute the tool call
    with track_state_changes(new_branch, event_type=EventType.TOOL_CALL_EXECUTED, tool_call_id=modified_tc.id):
        tool_result = await _execute_tool_call(
            new_branch,
            original_tc.function,
            new_arguments,
            modified_tc.id,
            session.target_model,
            session_id=session.id,
        )
        new_branch.auditor_messages.append(tool_result)

    await push_view_state(session_id)


async def handle_rewrite_tool_call(
    websocket: WebSocket,
    session_id: str,
    data: dict[str, Any],
) -> None:
    """Generate rewritten tool-call arguments without applying them."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    request_id = data["request_id"]
    tool_call_id = data["tool_call_id"]
    instruction = data["instruction"]
    selected_text = data.get("selected_text")
    target_field = data.get("target_field")

    if not instruction or not instruction.strip():
        await websocket.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": "Rewrite instruction cannot be empty",
        })
        return

    session = sessions[session_id]
    branch = session.current_branch()

    tool_call_event_index = None
    for i, event in enumerate(branch.events):
        if event.tool_call_id == tool_call_id and event.event_type == EventType.TOOL_CALL_ADDED:
            tool_call_event_index = i
            break

    if tool_call_event_index is None:
        await websocket.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": f"Tool call not found: {tool_call_id}",
        })
        return

    original_tc = None
    for msg in reversed(branch.auditor_messages):
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.id == tool_call_id:
                    original_tc = tc
                    break
            if original_tc:
                break

    if original_tc is None:
        await websocket.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": f"Tool call {tool_call_id} not found in current branch messages",
        })
        return

    try:
        context_messages, _ = reconstruct_at_event(branch, tool_call_event_index - 1)
        rewritten_arguments = await _generate_rewritten_arguments(
            session=session,
            context_messages=context_messages,
            tool_name=original_tc.function,
            original_arguments=original_tc.arguments,
            instruction=instruction,
            selected_text=selected_text,
            target_field=target_field,
        )
    except (ValueError, RuntimeError, json.JSONDecodeError) as e:
        logger.error("rewrite_tool_call failed: %s", e, exc_info=True)
        await websocket.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": str(e),
        })
        return

    await websocket.send_json({
        "type": "rewrite_tool_call_result",
        "request_id": request_id,
        "tool_call_id": tool_call_id,
        "rewritten_arguments": rewritten_arguments,
    })


async def handle_resample_target_response(session_id: str, data: dict[str, Any]) -> None:
    """Handle resampling a target model response.

    This creates a new branch and immediately queries the target model again,
    bypassing the cache to ensure a fresh response.
    """
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    # Cancel any running generation to prevent concurrent mutation
    if session_id in generation_tasks:
        task = generation_tasks.pop(session_id)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    session = sessions[session_id]
    event_id = data.get("event_id")
    # Support both 'message_id' and 'target_message_id' (frontend uses the latter)
    message_id = data.get("target_message_id")
    if message_id is None:
        message_id = data.get("message_id")
    # tool_call_id is the most reliable identifier for finding query_target events
    tool_call_id = data.get("tool_call_id")

    if not any([event_id, message_id, tool_call_id]):
        raise ValueError(
            "At least one of event_id, target_message_id/message_id, or tool_call_id is required"
        )

    branch = session.current_branch()

    logger.debug(
        f"[handle_resample_target_response] event_id={event_id}, message_id={message_id}, "
        f"tool_call_id={tool_call_id}, num_events={len(branch.events)}"
    )

    # Find the event that contains the target response.
    # Target responses are added during TOOL_CALL_EXECUTED events (from query_target tool).
    # We search by (in priority order):
    # 1. event_id - if provided, match exactly
    # 2. tool_call_id - most reliable, explicitly stored on TOOL_CALL_EXECUTED events
    # 3. message_id - search in patches (less reliable due to serialization)
    target_response_index = None
    for i, event in enumerate(branch.events):
        # Check TOOL_CALL_EXECUTED events (query_target creates these)
        if event.event_type in (EventType.TOOL_CALL_EXECUTED, EventType.TARGET_RESPONSE):
            logger.debug(
                f"  Checking event {i} (type={event.event_type}): id={event.id}, "
                f"tool_call_id={event.tool_call_id}"
            )
            # Match by event_id if provided
            if event_id and event.id == event_id:
                target_response_index = i
                break
            # Match by tool_call_id (most reliable)
            if tool_call_id and event.tool_call_id == tool_call_id:
                logger.debug(f"    Found matching event by tool_call_id: {tool_call_id}")
                target_response_index = i
                break
            # Search for the message_id in both target_patches and auditor_patches
            if message_id:
                # Check target_patches for target response message
                for patch in event.target_patches:
                    if patch.get("op") == "add":
                        value = patch.get("value", {})
                        if isinstance(value, dict) and value.get("id") == message_id:
                            logger.debug(f"    Found matching message in target_patches: {message_id}")
                            target_response_index = i
                            break
                if target_response_index is not None:
                    break
                # Check auditor_patches for tool result message
                for patch in event.auditor_patches:
                    if patch.get("op") == "add":
                        value = patch.get("value", {})
                        if isinstance(value, dict) and value.get("id") == message_id:
                            logger.debug(f"    Found matching message in auditor_patches: {message_id}")
                            target_response_index = i
                            break
                if target_response_index is not None:
                    break

    if target_response_index is None:
        # Build detailed error message for debugging
        tool_call_executed_events = [
            (i, e.tool_call_id) for i, e in enumerate(branch.events)
            if e.event_type == EventType.TOOL_CALL_EXECUTED
        ]
        raise ValueError(
            f"Target response event not found for event_id={event_id}, message_id={message_id}, "
            f"tool_call_id={tool_call_id}. Checked {len(branch.events)} events. "
            f"TOOL_CALL_EXECUTED events: {tool_call_executed_events}"
        )

    # Branch from just BEFORE the target response
    at_event_index = max(0, target_response_index - 1)

    new_branch = create_branch(
        session, branch.id, at_event_index,
        branch_type=BranchPointType.TARGET_RESPONSE,
    )

    session.current_branch_index = len(session.branches) - 1

    # Re-execute query_target with cache bypass
    resample_id = generate_id()
    cache_policy = create_cache_policy(
        session_id=session.id,
        resample_id=resample_id,
        expiry="1D",
    )

    try:
        # Use the tool_call_id from the request if available (most reliable)
        original_tool_call_id = tool_call_id
        if not original_tool_call_id:
            # Fallback: find the last query_target tool call
            for msg in reversed(new_branch.auditor_messages):
                if msg.role == "assistant" and msg.tool_calls:
                    for tc in msg.tool_calls:
                        if tc.function == "query_target":
                            original_tool_call_id = tc.id
                            break
                    if original_tool_call_id:
                        break

        if not original_tool_call_id:
            raise ValueError("Could not find query_target tool call in branch")

        # Execute query_target and track the state changes
        with track_state_changes(new_branch, event_type=EventType.TOOL_CALL_EXECUTED):
            # This updates target_state.messages
            response, formatted_response = await execute_query_target(
                new_branch.target_state,
                session.target_model,
                cache_policy=cache_policy,
            )

            # Add the tool result to auditor_messages
            tool_result_msg = ChatMessageTool(
                id=generate_id(),
                content=formatted_response,
                tool_call_id=original_tool_call_id,
                function="query_target",
                metadata={"source": "System"},
            )
            new_branch.auditor_messages.append(tool_result_msg)

        await push_view_state(session_id)

    except (ValueError, KeyError) as e:
        logger.error(f"Error resampling target response: {e}", exc_info=True)
        await broadcast_to_session(session_id, {
            "type": "error",
            "message": f"Error resampling target: {str(e)}",
        })
        await push_view_state(session_id)


def _make_on_event(session_id: str):
    """Create the on_event callback for generation loops.

    Sends delta messages during streaming, and pushes full state on turn_complete
    and conversation_ended.
    """
    async def on_event(event_type: str, data: dict[str, Any]) -> None:
        session = sessions[session_id]
        branch = session.current_branch()
        version = get_next_version(session_id)

        if event_type == "auditor_turn_start":
            await broadcast_to_session(session_id, {
                "type": "delta_turn_start",
                "message": data["message"],
                "branch_id": branch.id,
                "version": version,
            })
        elif event_type == "tool_call_added":
            await broadcast_to_session(session_id, {
                "type": "delta_tool_call",
                "tool_call": data["tool_call"],
                "branch_id": branch.id,
                "version": version,
            })
        elif event_type == "tool_call_executed":
            await broadcast_to_session(session_id, {
                "type": "delta_tool_result",
                "tool_result": data["tool_result"],
                "target_state": data["target_state"],
                "branch_id": branch.id,
                "version": version,
            })
        elif event_type in ("turn_complete", "conversation_ended"):
            await push_view_state(session_id)

    return on_event


async def _generation_loop(session_id: str) -> None:
    """Run the generation loop until paused or conversation ends.

    Includes a safety mechanism to detect stalled auditors that generate
    empty responses with no tool calls (which would loop forever).
    """
    MAX_EMPTY_TURNS = 3  # Max consecutive turns with no tool calls and very short content
    consecutive_empty_turns = 0

    try:
        while (
            session_id in sessions
            and playback_states[session_id] == "playing"
            and session_id in cancel_tokens
            and not cancel_tokens[session_id].is_set()
        ):
            session = sessions[session_id]
            branch = session.current_branch()
            msg_count_before = len(branch.auditor_messages)
            on_event = _make_on_event(session_id)

            try:
                should_continue = await execute_auditor_turn(session, on_event)

                if not should_continue:
                    playback_states[session_id] = "idle"
                    break

                # Check if the auditor is making progress (has tool calls or meaningful content)
                new_messages = branch.auditor_messages[msg_count_before:]
                has_tool_calls = any(
                    m.role == "assistant" and m.tool_calls
                    for m in new_messages
                )
                has_meaningful_content = any(
                    m.role == "assistant" and len(str(m.content)) > 10
                    for m in new_messages
                )

                if has_tool_calls or has_meaningful_content:
                    consecutive_empty_turns = 0
                else:
                    consecutive_empty_turns += 1
                    logger.warning(
                        f"[_generation_loop] Auditor produced empty turn "
                        f"({consecutive_empty_turns}/{MAX_EMPTY_TURNS})"
                    )

                if consecutive_empty_turns >= MAX_EMPTY_TURNS:
                    logger.warning(
                        f"[_generation_loop] Stopping: auditor produced "
                        f"{MAX_EMPTY_TURNS} consecutive empty turns"
                    )
                    playback_states[session_id] = "paused"
                    await broadcast_to_session(session_id, {
                        "type": "error",
                        "message": (
                            f"Auditor paused: produced {MAX_EMPTY_TURNS} consecutive "
                            "turns with no tool calls. The auditor may be waiting for "
                            "researcher guidance."
                        ),
                    })
                    break

            except asyncio.CancelledError:
                # Normal cancellation from pause -- not an error.
                logger.info(f"Generation loop cancelled for session {session_id[:8]}")
                playback_states[session_id] = "paused"
                break

            except Exception as e:
                logger.error(f"Error in generation loop: {e}", exc_info=True)
                playback_states[session_id] = "paused"
                await broadcast_to_session(session_id, {
                    "type": "error",
                    "message": str(e),
                })
                break
    finally:
        # Clean up task reference BEFORE pushing state so is_generating is False
        if session_id in generation_tasks:
            del generation_tasks[session_id]
        await push_view_state(session_id)


async def _execute_single_turn(session_id: str) -> None:
    """Execute a single auditor turn."""
    if session_id not in sessions:
        raise ValueError(f"Session not found: {session_id}")

    session = sessions[session_id]
    on_event = _make_on_event(session_id)

    try:
        should_continue = await execute_auditor_turn(session, on_event)

        if not should_continue:
            playback_states[session_id] = "idle"
        else:
            playback_states[session_id] = "paused"

    except asyncio.CancelledError:
        # Normal cancellation from pause -- not an error.
        # The interrupted tool result was already added by _execute_tool_call.
        logger.info(f"Single turn cancelled for session {session_id[:8]}")
        playback_states[session_id] = "paused"

    except Exception as e:
        logger.error(f"Error in single turn execution: {e}", exc_info=True)
        playback_states[session_id] = "paused"
        await broadcast_to_session(session_id, {
            "type": "error",
            "message": str(e),
        })
    finally:
        # Clean up task reference BEFORE pushing state so is_generating is False
        if session_id in generation_tasks:
            del generation_tasks[session_id]
        await push_view_state(session_id)


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the server."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
