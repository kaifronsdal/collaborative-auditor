"""Shared helpers for the workbench smoke tests."""

from __future__ import annotations

from typing import Any

from workbench.session import Session


class FakeConn:
    """A WebSocket-shaped sink that records every wire message it receives."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def resolve_role(span_id: str | None, session: Session) -> tuple[str, str] | None:
    """Walk `span_id → parent → …` until a registered role span is hit."""
    cur = span_id
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        hit = session.span_role.get(cur)
        if hit is not None:
            return hit
        seen.add(cur)
        cur = session.span_parent.get(cur)
    return None


def wire_events(
    conn: FakeConn, *, kinds: tuple[str, ...] = ("event", "update")
) -> list[dict[str, Any]]:
    """All `event` payloads from the wire capture, in send order."""
    return [m["event"] for m in conn.sent if m["t"] in kinds]
