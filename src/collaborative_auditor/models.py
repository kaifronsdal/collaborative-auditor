"""Core data structures for the Collaborative Auditor Interface.

This module defines the state management structures including Sessions, Branches,
and AtomicEvents that enable fine-grained branching and state reconstruction.
"""

from __future__ import annotations

import contextlib
import copy
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Generator

import jsonpatch
from inspect_ai.model import ChatMessage
from inspect_ai.tool import ToolParams
from pydantic import BaseModel, Field, TypeAdapter

# TypeAdapter for serializing/deserializing ChatMessage union types.
# This handles ToolCall (pydantic dataclass) serialization natively,
# eliminating the need for manual asdict() or exclude hacks.
ChatMessageList = TypeAdapter(list[ChatMessage])


def generate_id() -> str:
    """Generate a unique ID for sessions, branches, and events."""
    return str(uuid.uuid4())


class EventType(str, Enum):
    """Types of atomic events for precise branching."""

    AUDITOR_TURN_START = "auditor_turn_start"  # Starts a new turn, contains thinking/text
    TOOL_CALL_ADDED = "tool_call_added"  # A tool call is added to the turn
    TOOL_CALL_EXECUTED = "tool_call_executed"  # A tool call is executed
    TARGET_RESPONSE = "target_response"  # Target model generated a response
    RESEARCHER_MESSAGE = "researcher_message"  # Researcher adds feedback
    SYSTEM_INIT = "system_init"  # Session initialization


class BranchPointType(str, Enum):
    """Type of branch point for proper UI indication."""

    TURN = "turn"  # Branch point is a full turn resample
    TOOL_CALL = "tool_call"  # Branch point is a tool call edit/resample
    TARGET_RESPONSE = "target"  # Branch point is a target response resample


class ToolDefinition(BaseModel):
    """Definition of a tool available to the target model."""

    name: str
    description: str
    parameters: ToolParams


_ChatMessageAdapter = TypeAdapter(ChatMessage)


def serialize_chat_message(msg: ChatMessage) -> dict[str, Any]:
    """Serialize a ChatMessage to a JSON-safe dict.

    Uses TypeAdapter which handles ToolCall pydantic dataclasses natively.
    """
    return _ChatMessageAdapter.dump_python(msg, mode="json")


class TargetState(BaseModel):
    """Current state of the target model conversation.

    The system prompt is the first message in the messages list if present.
    """

    messages: list[ChatMessage] = Field(default_factory=list)
    tools: list[ToolDefinition] = Field(default_factory=list)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Custom dump using TypeAdapter for ChatMessage serialization."""
        return {
            "messages": ChatMessageList.dump_python(self.messages, mode="json"),
            "tools": [t.model_dump(**kwargs) for t in self.tools],
        }


class AtomicEvent(BaseModel):
    """An atomic event bundling auditor and target state changes via JSON patches.

    Each event represents a single atomic change to the session state.
    Events are recorded automatically by the track_state_changes context manager.
    """

    id: str = Field(default_factory=generate_id)
    seq: int = 0  # Currently unused — reserved for future reconnection sequencing
    timestamp: datetime = Field(default_factory=datetime.now)
    event_type: EventType = EventType.SYSTEM_INIT  # Type of this event
    turn_id: str | None = None  # Links events in the same auditor turn
    tool_call_id: str | None = None  # Links to specific tool call
    parent_event_id: str | None = None  # For nested events (e.g., target response within tool call)
    auditor_patches: list[dict[str, Any]] = Field(default_factory=list)
    target_patches: list[dict[str, Any]] = Field(default_factory=list)


class ResearcherBranch(BaseModel):
    """A researcher-level branch that forks the entire auditor conversation.

    Each branch maintains its own auditor messages, target state, and event history.
    Branches share a common prefix up to the branch point.
    """

    id: str = Field(default_factory=generate_id)
    auditor_messages: list[ChatMessage] = Field(default_factory=list)
    target_state: TargetState = Field(default_factory=TargetState)
    events: list[AtomicEvent] = Field(default_factory=list)


class BranchPoint(BaseModel):
    """Detailed branch point information.

    Tracks where branches diverge, with support for different granularities:
    turn-level, tool-call-level, or target-response-level branching.
    """

    id: str = Field(default_factory=generate_id)
    branch_type: BranchPointType
    event_id: str  # The event where branching occurs
    message_id: str | None = None  # Message ID (for UI display)
    tool_call_id: str | None = None  # Tool call ID (for tool-call branching)
    turn_id: str | None = None  # Turn ID (for turn-level branching)
    branch_ids: list[str] = Field(default_factory=list)  # Branches at this point


class Session(BaseModel):
    """The complete session state for a collaborative audit.

    A session contains multiple branches, with one being the current active branch.
    Branch points are tracked to enable navigation between branches in the UI.
    """

    id: str = Field(default_factory=generate_id)
    initial_prompt: str
    auditor_model: str
    target_model: str
    branches: list[ResearcherBranch] = Field(default_factory=list)
    current_branch_index: int = 0

    # Branch points with detailed type information for fine-grained branching
    branch_points: list[BranchPoint] = Field(default_factory=list)

    def current_branch(self) -> ResearcherBranch:
        """Get the currently active branch."""
        return self.branches[self.current_branch_index]


def create_session(initial_prompt: str, auditor_model: str, target_model: str) -> Session:
    """Create a new session with an initial empty branch.

    Args:
        initial_prompt: The researcher's initial instructions for the auditor
        auditor_model: Name of the model to use as the auditor
        target_model: Name of the model being audited

    Returns:
        A new Session with one empty branch
    """
    initial_branch = ResearcherBranch(id=generate_id())
    return Session(
        id=generate_id(),
        initial_prompt=initial_prompt,
        auditor_model=auditor_model,
        target_model=target_model,
        branches=[initial_branch],
        current_branch_index=0,
    )


def _serialize_auditor(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Serialize auditor messages to JSON-compatible dicts."""
    return ChatMessageList.dump_python(messages, mode="json")


def _serialize_target(state: TargetState) -> dict[str, Any]:
    """Serialize target state to a JSON-compatible dict."""
    return state.model_dump()


@contextlib.contextmanager
def track_state_changes(
    branch: ResearcherBranch,
    event_type: EventType = EventType.SYSTEM_INIT,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
    parent_event_id: str | None = None,
) -> Generator[None, None, None]:
    """Context manager that automatically records state changes as an atomic event.

    Usage:
        with track_state_changes(branch, event_type=EventType.TOOL_CALL_ADDED):
            branch.auditor_messages.append(new_message)
            branch.target_state.messages.append(target_msg)
        # AtomicEvent is automatically created and added to branch.events
        # Access the created event via branch.events[-1] if needed

    Args:
        branch: The branch to track changes on
        event_type: The type of event being recorded.
        turn_id: Optional turn ID linking events in the same auditor turn.
        tool_call_id: Optional tool call ID linking to a specific tool call.
        parent_event_id: Optional parent event ID for nested events.

    Note: If an exception is raised inside the context, the event is NOT recorded
    to avoid capturing partial/corrupt state in the event log.
    """
    # Snapshot before
    auditor_before = _serialize_auditor(branch.auditor_messages)
    target_before = _serialize_target(branch.target_state)

    exception_occurred = False
    try:
        yield
    except BaseException:
        exception_occurred = True
        raise
    finally:
        # Only record event if the operation completed successfully.
        # If an exception occurred, skip recording to avoid capturing
        # partial/corrupt state in the event log.
        if not exception_occurred:
            auditor_after = _serialize_auditor(branch.auditor_messages)
            target_after = _serialize_target(branch.target_state)

            # Compute diffs using jsonpatch.make_patch
            auditor_patch = jsonpatch.make_patch(auditor_before, auditor_after)
            target_patch = jsonpatch.make_patch(target_before, target_after)

            # Always record the event, even if the diff is empty.
            # An empty-diff event is harmless when replayed but ensures
            # callers can safely reference branch.events[-1].
            event = AtomicEvent(
                id=generate_id(),
                seq=0,
                timestamp=datetime.now(),
                event_type=event_type,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                parent_event_id=parent_event_id,
                auditor_patches=auditor_patch.patch,
                target_patches=target_patch.patch,
            )
            branch.events.append(event)


def reconstruct_at_event(
    source: ResearcherBranch,
    event_index: int,
) -> tuple[list[ChatMessage], TargetState]:
    """Reconstruct state by replaying events up to event_index.

    Args:
        source: The branch to reconstruct state from
        event_index: The event index to reconstruct up to (inclusive)

    Returns:
        A tuple of (auditor_messages, target_state) at that point in time

    Raises:
        ValueError: If event_index is negative or out of range
    """
    if event_index < 0:
        raise ValueError(f"event_index must be non-negative, got {event_index}")
    if event_index >= len(source.events):
        raise ValueError(
            f"event_index {event_index} out of range for branch with {len(source.events)} events"
        )

    auditor_state: list[dict[str, Any]] = []
    target_state: dict[str, Any] = TargetState().model_dump()

    for event in source.events[: event_index + 1]:
        auditor_state = jsonpatch.apply_patch(auditor_state, event.auditor_patches)
        target_state = jsonpatch.apply_patch(target_state, event.target_patches)

    return (
        ChatMessageList.validate_python(auditor_state),
        TargetState.model_validate(target_state),
    )


def has_branch_point(session: Session, branch_id: str, message_id: str) -> bool:
    """Check if there's a branch point after this message for this branch."""
    for bp in session.branch_points:
        if bp.message_id == message_id and branch_id in bp.branch_ids:
            return True
    return False


def get_branches_at_point(session: Session, message_id: str) -> list[str]:
    """Get all branches that diverge after this message (ordered by creation time)."""
    for bp in session.branch_points:
        if bp.message_id == message_id:
            return bp.branch_ids
    return []


def get_branch_point_by_message(session: Session, message_id: str) -> BranchPoint | None:
    """Get the branch point for a specific message ID."""
    for bp in session.branch_points:
        if bp.message_id == message_id:
            return bp
    return None


def get_branch_point_by_tool_call(session: Session, tool_call_id: str) -> BranchPoint | None:
    """Get the branch point for a specific tool call ID."""
    for bp in session.branch_points:
        if bp.tool_call_id == tool_call_id:
            return bp
    return None


def get_branch_point_by_event(session: Session, event_id: str) -> BranchPoint | None:
    """Get the branch point for a specific event ID."""
    for bp in session.branch_points:
        if bp.event_id == event_id:
            return bp
    return None


def create_branch(
    session: Session,
    source_branch_id: str,
    at_event_index: int,
    branch_type: BranchPointType | None = None,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
) -> ResearcherBranch:
    """Create a new branch from a source branch at the given event.

    Args:
        session: The session to create the branch in
        source_branch_id: ID of the branch to fork from
        at_event_index: Index of the event to branch from
        branch_type: Type of branching (turn, tool_call, or target). Defaults to TURN.
        turn_id: Optional turn ID for the branch point.
        tool_call_id: Optional tool call ID for tool-call-level branching.

    Returns:
        The newly created branch

    Raises:
        ValueError: If source_branch_id not found or at_event_index is invalid
    """
    if at_event_index < 0:
        raise ValueError(f"at_event_index must be non-negative, got {at_event_index}")

    source = None
    for b in session.branches:
        if b.id == source_branch_id:
            source = b
            break
    if source is None:
        raise ValueError(f"Branch not found: {source_branch_id}")

    if at_event_index >= len(source.events):
        raise ValueError(
            f"at_event_index {at_event_index} out of range for branch with {len(source.events)} events"
        )

    # Reconstruct state at that point
    auditor_msgs, target_state = reconstruct_at_event(source, at_event_index)

    # Get the event at the branch point
    branch_event = source.events[at_event_index]

    # Get the PARENT message ID - the last shared message BEFORE the branch point.
    # This is the same across all branches and serves as the anchor/reference point.
    # The UI will show the branch indicator on the message AFTER this one.
    if not auditor_msgs:
        raise ValueError(
            f"No auditor messages at event_index {at_event_index} — "
            f"state reconstruction may be broken"
        )
    branch_point_msg_id = auditor_msgs[-1].id

    # Determine branch type from event if not specified
    if branch_type is None:
        if branch_event.event_type in (EventType.TOOL_CALL_ADDED, EventType.TOOL_CALL_EXECUTED):
            branch_type = BranchPointType.TOOL_CALL
        elif branch_event.event_type == EventType.TARGET_RESPONSE:
            branch_type = BranchPointType.TARGET_RESPONSE
        else:
            branch_type = BranchPointType.TURN

    new_branch = ResearcherBranch(
        id=generate_id(),
        auditor_messages=copy.deepcopy(auditor_msgs),
        target_state=copy.deepcopy(target_state),
        events=copy.deepcopy(source.events[: at_event_index + 1]),
    )

    session.branches.append(new_branch)

    # Find or create branch point
    existing_bp = None
    for bp in session.branch_points:
        if bp.event_id == branch_event.id:
            existing_bp = bp
            break

    if existing_bp:
        # Add to existing branch point
        if source_branch_id not in existing_bp.branch_ids:
            existing_bp.branch_ids.append(source_branch_id)
        if new_branch.id not in existing_bp.branch_ids:
            existing_bp.branch_ids.append(new_branch.id)
    else:
        # Create new branch point
        # Only inherit tool_call_id from event for tool-call/target branch types.
        # Turn branch points should not have a spurious tool_call_id from the
        # preceding event.
        bp_tool_call_id = tool_call_id
        if not bp_tool_call_id and branch_type in (BranchPointType.TOOL_CALL, BranchPointType.TARGET_RESPONSE):
            bp_tool_call_id = branch_event.tool_call_id

        new_bp = BranchPoint(
            id=generate_id(),
            branch_type=branch_type,
            event_id=branch_event.id,
            message_id=branch_point_msg_id,
            tool_call_id=bp_tool_call_id,
            turn_id=turn_id if turn_id is not None else branch_event.turn_id,
            branch_ids=[source_branch_id, new_branch.id],
        )
        session.branch_points.append(new_bp)

    return new_branch
