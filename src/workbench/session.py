"""The audit-workbench session core — a dumb event pipe with message pooling.

STREAMING.md §B. The `Session` owns a single `Transcript`, subscribes once, and
forwards every inspect `Event` to all connected WebSockets as-is via
`model_dump(mode="json")`. The only type-specific handling is `ModelEvent`:
its `input` (the full conversation history, re-sent on every streaming flush)
is condensed into an append-only, content-hash-deduped message `pool` and
replaced by run-length `input_refs` ranges. The frontend resolves those refs
against the pool with `@tsmono/inspect-common`'s `expandEvents` (STREAMING.md §C).

No backend message derivation, no per-role `ChatMessage[]` — the frontend reads
what it needs from the events (STREAMING.md §B/§D).

The `_subscribe` callback is sync and fast (no I/O, no awaits). It enqueues wire
messages onto an `anyio` memory stream; a single `drain()` task owns the actual
broadcast.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import WebSocketDisconnect
from inspect_ai.event import BranchEvent, Event, ModelEvent, SpanBeginEvent
from inspect_ai.event._pool import (  # noqa: PLC2701
    _msg_hash,
    condense_model_event_inputs_with_lookup,
)
from inspect_ai.log._transcript import Transcript, init_transcript
from inspect_ai.model import ChatMessage
from inspect_petri._auditor._target_timeline import build_target_timeline  # noqa: PLC2701

from workbench.view import Role

if TYPE_CHECKING:
    from workbench.run import Branch

logger = logging.getLogger(__name__)


class Connection(Protocol):
    """A WebSocket-shaped sink. The smoke test substitutes a list-backed fake."""

    async def send_json(self, data: dict[str, Any]) -> None: ...


class Session:
    def __init__(self) -> None:
        # One transcript per session. The contextvar is NOT set here — __init__
        # runs in whichever WS task created the session, and a `start` from a
        # later connection spawns `Branch.run()` in a different task context
        # that wouldn't inherit it. `Branch.run()` sets it explicitly instead.
        self.transcript = Transcript()
        self.transcript._subscribe(self._on_event)  # noqa: SLF001

        self.branches: dict[str, Branch] = {}
        self.current: str | None = None

        # message pool (STREAMING.md §B): content-hash-deduped, append-only.
        # ModelEvent.input is interned here and replaced by input_refs ranges.
        self.pool: list[ChatMessage] = []
        self.pool_idx: dict[str, int] = {}  # content hash → pool index
        self.pool_sent: int = 0  # high-water-mark already shipped to clients

        # event store + span routing (STREAMING.md §B)
        self.events: dict[str, dict[str, Any]] = {}  # uuid → dumped event
        self.span_role: dict[str, tuple[str, Role]] = {}  # span_id → (branch, role)
        self.span_parent: dict[str, str | None] = {}  # from SpanBeginEvent

        self.version: int = 0
        self.connections: list[Connection] = []
        self.branch_tasks: list[asyncio.Task[None]] = []  # detached branch.run() tasks
        self._dispatch_lock = asyncio.Lock()

        # sync handler → drain task hand-off. Bounded buffer: the handler runs
        # inline in the generating task and must not block, so on overflow we
        # log + drop rather than back-pressure (which would stall generate) or
        # grow unbounded (which would OOM under a stalled client).
        self._send: MemoryObjectSendStream[dict[str, Any]]
        self._recv: MemoryObjectReceiveStream[dict[str, Any]]
        self._send, self._recv = anyio.create_memory_object_stream[dict[str, Any]](
            max_buffer_size=2048
        )

        # `drain` is owned by the session, not by any one branch (STREAMING.md
        # §B; footgun #12): a single drain task per session consumes `_recv`,
        # so concurrent or successive branches share one broadcast loop instead
        # of each starting their own and splitting wire messages. `start()`
        # opens this; `close()` shuts it down.
        self._closed = anyio.Event()
        self._run_task: asyncio.Task[None] | None = None

    # -- drain lifecycle (session-owned, STREAMING.md §B) ---------------------

    async def start(self) -> None:
        """Open the session-level task group and start the single `drain` task.

        Returns once `drain` is running. The task group stays open until
        `close()` is called; until then `drain` consumes every wire message the
        sync `_on_event` handler enqueues and broadcasts it to all connections.
        """
        started = anyio.Event()
        self._run_task = asyncio.create_task(self._run(started))
        self._run_task.add_done_callback(lambda _t: started.set())
        await started.wait()

    async def _run(self, started: anyio.Event) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self.drain)
            started.set()
            await self._closed.wait()
            # `drain`'s `async for` exits when `_send` is closed; closing it
            # here lets the task group finish cleanly without a cancel scope.
            await self._send.aclose()

    async def close(self) -> None:
        """Cancel running branches, then shut down the drain task.

        Branch tasks are cancelled first so they don't try to enqueue onto a
        closed `_send` stream (which would raise `ClosedResourceError`).
        """
        for t in self.branch_tasks:
            if not t.done():
                t.cancel()
        for t in self.branch_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._closed.set()
        if self._run_task is not None:
            await self._run_task

    # -- message pool ---------------------------------------------------------

    def _lookup(self, msg: ChatMessage) -> int:
        """Intern a message into the pool, returning its index.

        The hash matches inspect's `.eval` recorder (`event/_pool.py`), so the
        wire encoding is byte-identical to what `expandEvents` resolves.
        """
        h = _msg_hash(msg)
        idx = self.pool_idx.get(h)
        if idx is None:
            idx = len(self.pool)
            self.pool_idx[h] = idx
            self.pool.append(msg)
        return idx

    # -- event handling (sync, fast, inline in the generating task) -----------

    def _condense(self, ev: Event) -> dict[str, Any]:
        """Dump an event, interning `ModelEvent.input` into the pool.

        Delegates to inspect's `condense_model_event_inputs_with_lookup` — a
        typed `model_copy` that sets `input_refs` and clears `input`, so the
        wire shape tracks inspect's own `.eval` condensation rather than a
        hand-patched dict.
        """
        return condense_model_event_inputs_with_lookup(ev, self._lookup).model_dump(
            mode="json"
        )

    def _on_event(self, ev: Event) -> None:
        assert ev.uuid is not None, "event missing uuid at ingress"
        is_update = ev.uuid in self.events

        if isinstance(ev, SpanBeginEvent):
            self.span_parent[ev.id] = ev.parent_id

        dumped = self._condense(ev)
        if isinstance(ev, ModelEvent) and not is_update:
            self._emit_pool_delta()

        self.events[ev.uuid] = dumped
        self.version += 1
        self._enqueue(
            {
                "t": "update" if is_update else "event",
                "v": self.version,
                "event": dumped,
            }
        )

        # Rebuild + ship the target timeline when its structure or content
        # changes: on BranchEvent (new trajectory) and on each new target
        # ModelEvent (new turn — not streaming updates). `_on_event` runs in
        # the branch task's context, so `build_target_timeline` can read
        # `transcript().events`.
        if not is_update and isinstance(ev, (BranchEvent, ModelEvent)):
            resolved = self._resolve(ev.span_id)
            if (
                resolved is not None
                and resolved[1] == "target"
                and (branch := self.branches.get(resolved[0])) is not None
            ):
                self._enqueue(
                    {
                        "t": "timeline",
                        "v": self.version,
                        "branch": branch.branch_id,
                        "role": "target",
                        "timeline": self._target_timeline(branch),
                    }
                )

    def _resolve(self, span_id: str | None) -> tuple[str, Role] | None:
        """Walk `span_id → parent → …` to the nearest registered role span."""
        cur = span_id
        while cur is not None:
            hit = self.span_role.get(cur)
            if hit is not None:
                return hit
            cur = self.span_parent.get(cur)
        return None

    def _target_timeline(self, branch: "Branch") -> dict[str, Any]:
        return build_target_timeline(
            branch.history, branch.target_span_id, f"{branch.branch_id}:target"
        ).model_dump(mode="json")

    def _enqueue(self, msg: dict[str, Any]) -> None:
        try:
            self._send.send_nowait(msg)
        except anyio.WouldBlock:
            # bounded buffer full — a client is stalled. Drop rather than block
            # the generating task or OOM. The next `state` snapshot resyncs.
            logger.warning(
                "wire buffer full; dropping %r (v=%d)", msg["t"], msg.get("v")
            )
        except anyio.ClosedResourceError:
            # session.close() raced a late event from a branch's finally block.
            pass

    # -- wire emission (enqueue only; drain() owns the socket) ----------------

    def _emit_pool_delta(self) -> None:
        """Ship pool entries appended since the last delta (STREAMING.md §C)."""
        if len(self.pool) <= self.pool_sent:
            return
        entries = [m.model_dump(mode="json") for m in self.pool[self.pool_sent :]]
        self.version += 1
        self._enqueue(
            {
                "t": "pool",
                "v": self.version,
                "from": self.pool_sent,
                "entries": entries,
            }
        )
        self.pool_sent = len(self.pool)

    async def drain(self) -> None:
        """Own the WebSockets: await enqueued wire messages and broadcast them."""
        async for msg in self._recv:
            await self.broadcast(msg)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[Connection] = []
        for conn in list(self.connections):
            try:
                await conn.send_json(msg)
            except (WebSocketDisconnect, ConnectionError, OSError, RuntimeError) as exc:
                # RuntimeError("Cannot call 'send' once a close message has been sent.")
                # is raised by Starlette when the WS peer has already closed.
                logger.debug("dropping dead connection: %r", exc)
                dead.append(conn)
        for conn in dead:
            self.connections.remove(conn)

    # -- view / full-state ----------------------------------------------------

    def view(self) -> dict[str, Any]:
        """The full session snapshot (STREAMING.md §C `state` message body)."""
        # `build_target_timeline` reads `transcript().events`; set the var in
        # this task's context so it resolves to the session's transcript.
        init_transcript(self.transcript)
        timelines = {
            bid: {"target": self._target_timeline(b)} for bid, b in self.branches.items()
        }
        queued = {
            bid: {
                role: [m.model_dump(mode="json") for m in msgs]
                for role, msgs in b.queued.items()
            }
            for bid, b in self.branches.items()
        }
        branches_meta = {
            bid: {
                "parent": b.meta.parent,
                "branched_at": b.meta.branched_at,
                "status": b.status,
                "seed": b.meta.seed[:80],
            }
            for bid, b in self.branches.items()
        }
        return {
            "pool": [m.model_dump(mode="json") for m in self.pool],
            "events": list(self.events.values()),
            "span_role": {sid: list(v) for sid, v in self.span_role.items()},
            "queued": queued,
            "current": self.current,
            "status": self.current_status(),
            "branches": branches_meta,
            "timelines": timelines,
        }

    def current_status(self) -> str | None:
        """Status literal of the current branch, or None if no branch."""
        if self.current is None:
            return None
        return self.branches[self.current].status

    async def broadcast_status(self) -> None:
        """Push the current branch's status as a lightweight `status` message."""
        self.version += 1
        await self.broadcast(
            {"t": "status", "v": self.version, "status": self.current_status()}
        )

    async def push_full_state(self, conn: Connection) -> None:
        await conn.send_json({"t": "state", "v": self.version, **self.view()})
