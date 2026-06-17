"""Wire types for the audit-workbench backend → frontend protocol.

STREAMING.md §C. The frontend never sees inspect event types, span trees, or
`ModelEvent.input`; it sees `ChatMessage[]` per (branch, role), serialized via
inspect's own `.model_dump()`.
"""

from __future__ import annotations

from typing import Literal

from inspect_ai.model import ChatMessage
from pydantic import BaseModel

Role = Literal["auditor", "target"]
Status = Literal["idle", "running", "paused", "ended"]


class BranchView(BaseModel):
    auditor: list[ChatMessage]
    target: list[ChatMessage]
    status: Status
    generating: Role | None  # which role's ModelEvent is currently pending


class SessionView(BaseModel):
    branches: dict[str, BranchView]
    current: str


def dump_message(msg: ChatMessage) -> dict:
    """Serialize a single `ChatMessage` for the wire (inspect's own shape)."""
    return msg.model_dump(mode="json")


def dump_messages(msgs: list[ChatMessage]) -> list[dict]:
    return [dump_message(m) for m in msgs]
