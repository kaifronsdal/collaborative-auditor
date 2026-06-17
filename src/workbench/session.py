"""The audit-workbench session core — one transcript, span routing, derive messages.

STREAMING.md §B. The `Session` owns a single `Transcript`, subscribes once, and
routes every event to a (branch, role) by `span_id` ancestry. It derives the
`ChatMessage[]` per role backend-side and pushes the §C wire protocol
(`state` / `patch` / `stream`) to all connected WebSockets.

The `_subscribe` callback is sync and fast (no I/O, no awaits). It enqueues wire
messages onto an `anyio` memory stream; a single `drain()` task owns the actual
broadcast.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from inspect_ai._util.content import Content
from inspect_ai.event import Event, ModelEvent, SpanBeginEvent, ToolEvent
from inspect_ai.log._transcript import Transcript, init_transcript
from inspect_ai.model import ChatMessageTool

from workbench.view import BranchView, Role, SessionView, dump_message, dump_messages

if TYPE_CHECKING:
    from workbench.run import Branch

logger = logging.getLogger(__name__)


class Connection(Protocol):
    """A WebSocket-shaped sink. The smoke test substitutes a list-backed fake."""

    async def send_json(self, data: dict[str, Any]) -> None: ...


def _tool_message_from(ev: ToolEvent) -> ChatMessageTool:
    """Build the settled `ChatMessageTool` from a completed `ToolEvent`."""
    return ChatMessageTool(
        id=ev.message_id,
        content=ev.result,
        tool_call_id=ev.id,
        function=ev.function,
        error=ev.error,
    )


class Session:
    def __init__(self) -> None:
        # one transcript per session, set BEFORE any task group (F2) so sibling
        # auditor/target tasks inherit it.
        self.transcript = Transcript()
        init_transcript(self.transcript)
        self.transcript._subscribe(self._on_event)  # noqa: SLF001

        self.branches: dict[str, Branch] = {}
        self.current: str = ""

        # span routing state (STREAMING.md §B)
        self.span_role: dict[str, tuple[str, Role]] = {}  # span_id → (branch, role)
        self.span_parent: dict[str, str | None] = {}  # from SpanBeginEvent
        self.seen: set[str] = set()  # ModelEvent uuids first-seen-while-pending
        self.pending_in: dict[tuple[str, Role], str] = {}  # (branch, role) → uuid

        self.version: int = 0
        self.connections: list[Connection] = []
        self.branch_tasks: list[object] = []  # detached asyncio tasks (server)

        # sync handler → drain task hand-off. Unbounded buffer: the handler must
        # never block (it runs inline in the generating task).
        self._send: MemoryObjectSendStream[dict[str, Any]]
        self._recv: MemoryObjectReceiveStream[dict[str, Any]]
        self._send, self._recv = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=float("inf")
        )

    # -- event handling (sync, fast, inline in the generating task) -----------

    def _resolve(self, span_id: str | None) -> tuple[str, Role]:
        """Walk `span_parent` up from `span_id` until hitting a registered role span."""
        s = span_id
        while s is not None and s not in self.span_role:
            s = self.span_parent.get(s)
        if s is None or s not in self.span_role:
            raise KeyError(f"span_id {span_id!r} does not resolve to a role span")
        return self.span_role[s]

    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, SpanBeginEvent):
            self.span_parent[ev.id] = ev.parent_id
            return

        # only the two role timelines carry messages we surface; events outside
        # any registered span (e.g. session-level) are ignored.
        try:
            branch_id, role = self._resolve(ev.span_id)
        except KeyError:
            return

        match ev:
            case ModelEvent(pending=True) if ev.uuid not in self.seen:
                # first sight of this generate → it is now the pending one (F6:
                # a later retry's fresh uuid supersedes).
                self.seen.add(ev.uuid)
                self.pending_in[branch_id, role] = ev.uuid
            case ModelEvent(pending=True):
                if ev.uuid != self.pending_in.get((branch_id, role)):
                    return  # superseded retry attempt (F6)
                if not ev.output.message.content:
                    return  # empty placeholder flush (F11)
                self._emit_stream(branch_id, role, ev.output.message.content)
            case ModelEvent():
                self.pending_in.pop((branch_id, role), None)
                self.branches[branch_id].messages[role].append(ev.output.message)
                self._emit_patch(branch_id, role, dump_message(ev.output.message))
            case ToolEvent(pending=None):
                msg = _tool_message_from(ev)
                self.branches[branch_id].messages[role].append(msg)
                self._emit_patch(branch_id, role, dump_message(msg))

    # -- wire emission (enqueue only; drain() owns the socket) ----------------

    def _emit_patch(self, branch_id: str, role: Role, message: dict) -> None:
        self.version += 1
        op = {
            "op": "add",
            "path": f"/branches/{branch_id}/{role}/-",
            "value": message,
        }
        self._send.send_nowait({"t": "patch", "v": self.version, "ops": [op]})

    def _emit_stream(
        self, branch_id: str, role: Role, content: str | list[Content]
    ) -> None:
        # dump the in-flight message's content blocks for the provisional row.
        if isinstance(content, str):
            blocks: list[Any] = [content]
        else:
            blocks = [c.model_dump(mode="json") for c in content]
        self._send.send_nowait(
            {
                "t": "stream",
                "v": self.version,
                "branch": branch_id,
                "role": role,
                "content": blocks,
            }
        )

    async def drain(self) -> None:
        """Own the WebSockets: await enqueued wire messages and broadcast them."""
        async for msg in self._recv:
            await self.broadcast(msg)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[Connection] = []
        for conn in self.connections:
            try:
                await conn.send_json(msg)
            except (ConnectionError, OSError, RuntimeError):
                dead.append(conn)
        for conn in dead:
            self.connections.remove(conn)

    # -- view / full-state ----------------------------------------------------

    def view(self) -> SessionView:
        branches = {
            bid: BranchView(
                auditor=list(b.messages["auditor"]),
                target=list(b.messages["target"]),
                status=b.status,
                generating=b.generating,
            )
            for bid, b in self.branches.items()
        }
        return SessionView(branches=branches, current=self.current)

    async def push_full_state(self, conn: Connection) -> None:
        await conn.send_json(
            {"t": "state", "v": self.version, "view": self.view().model_dump(mode="json")}
        )

    # serialization helper kept here so callers don't import view internals.
    @staticmethod
    def dump_messages(msgs: list) -> list[dict]:
        return dump_messages(msgs)
