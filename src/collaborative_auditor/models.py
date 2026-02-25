"""Core data structures for the Collaborative Auditor Interface.

Event tree (git-like DAG) architecture:
- Events form a tree with bottom-up parent pointers (immutable once created)
- Branches are pointers to tip events + materialized state
- Branch points are structural (multi-child nodes) — no separate bookkeeping
"""

from __future__ import annotations

import contextlib
import copy
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Generator
from typing import Any

import jsonpatch
from inspect_ai.model import ChatMessage
from inspect_ai.tool import ToolParams
from pydantic import BaseModel, Field, TypeAdapter

_chat_message_list_adapter = TypeAdapter(list[ChatMessage])
_chat_message_adapter = TypeAdapter(ChatMessage)


def generate_id() -> str:
    return str(uuid.uuid4())


class EventType(str, Enum):
    SYSTEM_INIT = "system_init"
    AUDITOR_TURN_START = "auditor_turn_start"
    TOOL_CALL_ADDED = "tool_call_added"
    TOOL_CALL_EXECUTED = "tool_call_executed"
    RESEARCHER_MESSAGE = "researcher_message"


class ToolDefinition(BaseModel):
    """Definition of a tool available to the target model."""

    name: str
    description: str
    parameters: ToolParams


def serialize_chat_message(msg: ChatMessage) -> dict[str, Any]:
    return _chat_message_adapter.dump_python(msg, mode="json")


class TargetState(BaseModel):
    """Current state of the target model conversation."""

    messages: list[ChatMessage] = Field(default_factory=list)
    tools: list[ToolDefinition] = Field(default_factory=list)

    def model_dump(self, *, mode: str = "json", **kwargs: Any) -> dict[str, Any]:
        return {
            "messages": _chat_message_list_adapter.dump_python(self.messages, mode=mode),
            "tools": [t.model_dump(mode=mode, **kwargs) for t in self.tools],
        }


class EventNode(BaseModel):
    """A single event in the shared event tree. Immutable once created.

    Each event stores JSON patches that describe the state change from its
    parent's state. The parent_id forms a bottom-up pointer (like git commits).
    """

    id: str = Field(default_factory=generate_id)
    parent_id: str | None = None
    event_type: EventType = EventType.SYSTEM_INIT
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    turn_id: str | None = None
    tool_call_id: str | None = None
    auditor_patches: list[dict[str, Any]] = Field(default_factory=list)
    target_patches: list[dict[str, Any]] = Field(default_factory=list)


class Branch(BaseModel):
    """A branch is a pointer to a tip event + materialized (current) state.

    The materialized state is kept up-to-date as events are appended. It can
    also be reconstructed from scratch by replaying patches from root to tip.
    """

    id: str = Field(default_factory=generate_id)
    tip_event_id: str | None = None
    auditor_messages: list[ChatMessage] = Field(default_factory=list)
    target_state: TargetState = Field(default_factory=TargetState)


class Session(BaseModel):
    """A collaborative audit session.

    Events are stored in a shared pool (dict keyed by ID). Branches are
    lightweight pointers into the event tree. Branch points are discovered
    structurally by finding events with multiple children.
    """

    id: str = Field(default_factory=generate_id)
    initial_prompt: str = ""
    auditor_model: str = ""
    target_model: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    events: dict[str, EventNode] = Field(default_factory=dict)
    branches: list[Branch] = Field(default_factory=list)


def create_session(
    initial_prompt: str,
    auditor_model: str,
    target_model: str,
    session_id: str | None = None,
) -> Session:
    """Create a new session with one empty branch."""
    branch = Branch(id=generate_id())
    return Session(
        id=session_id or generate_id(),
        initial_prompt=initial_prompt,
        auditor_model=auditor_model,
        target_model=target_model,
        events={},
        branches=[branch],
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_auditor(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return _chat_message_list_adapter.dump_python(messages, mode="json")


def _serialize_target(state: TargetState) -> dict[str, Any]:
    return state.model_dump()


# ---------------------------------------------------------------------------
# Event tree operations
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def track_state_changes(
    session: Session,
    branch: Branch,
    event_type: EventType = EventType.SYSTEM_INIT,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
) -> Generator[None, None, None]:
    """Record state changes as an EventNode in the shared event tree.

    On successful exit, creates an EventNode with JSON patches describing
    the change, adds it to session.events, and advances branch.tip_event_id.

    If an exception occurs inside the block, no event is recorded.
    """
    auditor_before = _serialize_auditor(branch.auditor_messages)
    target_before = _serialize_target(branch.target_state)
    msg_ids_before = {m.id for m in branch.auditor_messages if m.id}

    yield

    auditor_after = _serialize_auditor(branch.auditor_messages)
    target_after = _serialize_target(branch.target_state)

    auditor_patch = jsonpatch.make_patch(auditor_before, auditor_after)
    target_patch = jsonpatch.make_patch(target_before, target_after)

    event = EventNode(
        id=generate_id(),
        parent_id=branch.tip_event_id,
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        auditor_patches=auditor_patch.patch,
        target_patches=target_patch.patch,
    )
    session.events[event.id] = event
    branch.tip_event_id = event.id

    for msg in branch.auditor_messages:
        if msg.id and msg.id not in msg_ids_before:
            if msg.metadata is None:
                msg.metadata = {}
            msg.metadata["event_id"] = event.id


def _walk_to_root(session: Session, start: str | None) -> list[str]:
    """Walk parent pointers from *start* to the root, returning root-to-start order."""
    path: list[str] = []
    event_id = start
    while event_id is not None:
        path.append(event_id)
        event_id = session.events[event_id].parent_id
    path.reverse()
    return path


def get_branch_path(session: Session, branch: Branch) -> list[str]:
    """Walk from branch tip to root, return event IDs in root-to-tip order."""
    return _walk_to_root(session, branch.tip_event_id)


class TreeIndex:
    """Precomputed indexes over the event tree."""

    __slots__ = ("children", "event_to_branch")

    def __init__(self, session: Session) -> None:
        self.children: dict[str, list[str]] = defaultdict(list)
        for event in session.events.values():
            if event.parent_id is not None:
                self.children[event.parent_id].append(event.id)

        self.event_to_branch: dict[str, str] = {}
        for branch in session.branches:
            event_id = branch.tip_event_id
            while event_id is not None:
                if event_id not in self.event_to_branch:
                    self.event_to_branch[event_id] = branch.id
                event_id = session.events[event_id].parent_id


def reconstruct_at_event(
    session: Session,
    target_event_id: str,
) -> tuple[list[ChatMessage], TargetState]:
    """Reconstruct state by replaying patches from root to the target event.

    Walks parent pointers to build the path, then applies patches in order.
    """
    if target_event_id not in session.events:
        raise ValueError(f"Event not found: {target_event_id}")

    path = _walk_to_root(session, target_event_id)

    auditor_state: list[dict[str, Any]] = []
    target_state: dict[str, Any] = TargetState().model_dump()

    for event_id in path:
        event = session.events[event_id]
        auditor_state = jsonpatch.apply_patch(auditor_state, event.auditor_patches)
        target_state = jsonpatch.apply_patch(target_state, event.target_patches)

    return (
        _chat_message_list_adapter.validate_python(auditor_state),
        TargetState.model_validate(target_state),
    )


def create_branch(
    session: Session,
    fork_event_id: str,
) -> Branch:
    """Create a new branch forking at the given event.

    Reconstructs the state at fork_event_id and deep-copies it into a new
    Branch whose tip points at the fork event. No existing events are mutated.
    """
    if fork_event_id not in session.events:
        raise ValueError(f"Event not found: {fork_event_id}")

    auditor_msgs, target_state = reconstruct_at_event(session, fork_event_id)

    new_branch = Branch(
        id=generate_id(),
        tip_event_id=fork_event_id,
        auditor_messages=copy.deepcopy(auditor_msgs),
        target_state=copy.deepcopy(target_state),
    )
    session.branches.append(new_branch)
    return new_branch


def find_event_on_path(
    session: Session,
    branch: Branch,
    *,
    event_type: EventType | None = None,
    turn_id: str | None = None,
    tool_call_id: str | None = None,
) -> EventNode | None:
    """Find the first event matching criteria on a branch's path (tip to root)."""
    event_id = branch.tip_event_id
    while event_id is not None:
        event = session.events[event_id]
        if all((
            event_type is None or event.event_type == event_type,
            turn_id is None or event.turn_id == turn_id,
            tool_call_id is None or event.tool_call_id == tool_call_id,
        )):
            return event
        event_id = event.parent_id
    return None


def find_turn_start(
    session: Session,
    branch: Branch,
    turn_id: str,
) -> EventNode | None:
    """Find the AUDITOR_TURN_START event for a given turn_id on this branch."""
    return find_event_on_path(
        session, branch,
        event_type=EventType.AUDITOR_TURN_START,
        turn_id=turn_id,
    )
