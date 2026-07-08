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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from fastapi import WebSocketDisconnect
from inspect_ai.event import AnchorEvent, BranchEvent, Event, ModelEvent, SpanBeginEvent
from inspect_ai.event._pool_index import (
    CallPoolIndex,
    MessagePoolIndex,
    condense_model_event_with_indices,
)
from inspect_ai.log import Transcript
from inspect_ai.log._transcript import init_transcript
from inspect_ai.model import ChatMessage
from inspect_petri._auditor import build_history_timeline
from inspect_petri.target import History
from shortuuid import uuid

from workbench.timeline import build_auditor_timeline
from workbench.view import Role

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator
    from workbench.run import Branch

logger = logging.getLogger(__name__)


class Connection(Protocol):
    """A WebSocket-shaped sink. The smoke test substitutes a list-backed fake."""

    async def send_json(self, data: dict[str, Any]) -> None: ...


@dataclass
class CandidateBatch:
    """One Resample-N request: N background sibling forks at one anchor.

    `picked` is ``None`` while the picker is open; set to the chosen child
    on `pick_candidate` or to `parent` on `dismiss_candidates` (the original
    is the implicit candidate #0). Unpicked children's `Trajectory` nodes
    stay in `audit_history` — only their tasks are cancelled.
    """

    parent: str
    anchor: str
    kind: Literal["target", "auditor"]
    children: list[str] = field(default_factory=list)
    picked: str | None = None


class Session:
    def __init__(
        self, session_id: str | None = None, store_dir: Path | None = None
    ) -> None:
        #: Persistence identity. `save()` writes under
        #: ``{store_dir}/{session_id}/`` when both are set; otherwise no-op.
        self.session_id = session_id
        self.store_dir = store_dir
        self.created_at: str = datetime.now(UTC).isoformat()

        # One transcript per session. The contextvar is NOT set here — __init__
        # runs in whichever WS task created the session, and a `start` from a
        # later connection spawns `Branch.run()` in a different task context
        # that wouldn't inherit it. `Branch.run()` sets it explicitly instead.
        self.transcript = Transcript()
        self.transcript._subscribe(self._on_event)  # noqa: SLF001

        # The session-wide level-2 audit `History` (PETRI-L2-HISTORY). Each
        # `Branch` wraps one `Trajectory` from this tree; `start` creates a
        # restart child of `.root`, `fork` calls `.branch(anchor, …)`. The
        # auditor timeline is built directly from this tree.
        self.audit_history = History()
        self.branches: dict[str, Branch] = {}
        self.current: str | None = None
        #: Resample-N batches keyed by `batch_id` (RESAMPLE-N.md). Candidates
        #: are background `Branch`es that run without repointing `current`.
        self.candidate_batches: dict[str, CandidateBatch] = {}
        #: The M1 orchestrator (M1-NOTEBOOK.md). At most one per *process*
        #: (``InteractiveShell`` singleton — M1-KERNEL-NOTES.md §3); ``None``
        #: on M0-only sessions.
        self.orchestrator: Orchestrator | None = None

        # message pool (STREAMING.md §B): content-hash-deduped, append-only.
        # ModelEvent.input is interned here and replaced by input_refs ranges.
        # Indexing is inspect's own (`event/_pool_index.py`, #4222) so dedup
        # matches the `.eval` recorder byte-for-byte.
        self.pool: list[ChatMessage] = []
        self._msg_index = MessagePoolIndex()
        self._call_index = CallPoolIndex()
        self.pool_sent: int = 0  # high-water-mark already shipped to clients

        # event store + span routing (STREAMING.md §B)
        self.events: dict[str, dict[str, Any]] = {}  # uuid → dumped event
        self.span_role: dict[str, tuple[str, Role]] = {}  # span_id → (branch, role)
        self.span_parent: dict[str, str | None] = {}  # from SpanBeginEvent
        # Per-(branch, role) ordered event-uuid lists, maintained incrementally
        # for `build_auditor_timeline` (cheaper than re-scanning `events` on
        # every rebuild).
        self.by_role: dict[tuple[str, Role], list[str]] = {}
        # anchor_id → [uuid, …] — incremental mirror of petri's
        # `_anchor_lookup` over dumped events, so `_target_timeline` can
        # resolve a just-emitted anchor without a full transcript scan.
        self._by_anchor: dict[str, list[str]] = {}

        self.version: int = 0
        self.connections: list[Connection] = []
        #: detached `branch.run()` tasks, keyed by `branch_id` so
        #: `_stop_running_branches(only=…)` can cancel selectively.
        self.branch_tasks: dict[str, asyncio.Task[None]] = {}
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
        """Persist, cancel running branches, then shut down the drain task.

        `save()` runs first (P0.2) so an in-flight orchestrator/branch is
        captured before its task is cancelled. Branch tasks are cancelled
        before the drain so they don't try to enqueue onto a closed `_send`
        stream (which would raise `ClosedResourceError`).
        """
        self.save()
        tasks = list(self.branch_tasks.values())
        if self.orchestrator is not None and self.orchestrator.task is not None:
            tasks.append(self.orchestrator.task)
        await _cancel_all(tasks)
        # P0.5: reap bash subprocesses AFTER the orch task is cancelled (so it
        # can't spawn more). ``close()`` killpg's each tracked process group.
        if self.orchestrator is not None:
            self.orchestrator.close()
        self._closed.set()
        if self._run_task is not None:
            await self._run_task

    # -- event handling (sync, fast, inline in the generating task) -----------

    def _condense(self, ev: Event) -> dict[str, Any]:
        """Dump an event, interning `ModelEvent.input` into the pool.

        Delegates to inspect's `condense_model_event_with_indices` (#4222),
        which sets `input_refs` and clears `input` so the wire shape is what
        `@tsmono/inspect-common`'s `expandEvents` resolves. Identity-bucketed
        lookup means re-sent history costs no per-message hashing. Walk is
        identity (no attachment refs in the workbench). `event.call` is never
        populated (no `log_model_api`), so the `calls` index and `add_call`
        are unreached stubs; if that changes, add a call pool to the wire.
        """
        if isinstance(ev, ModelEvent):

            def add_message(_hash: str, msg: ChatMessage) -> int:
                self.pool.append(msg)
                return len(self.pool) - 1

            ev = condense_model_event_with_indices(
                ev,
                messages=self._msg_index,
                calls=self._call_index,
                walk_message=lambda m: m,
                walk_call_message=lambda v: v,
                add_message=add_message,
                add_call=lambda _h, _v: 0,
            )
        return ev.model_dump(mode="json")

    def _on_event(self, ev: Event) -> None:
        assert ev.uuid is not None, "event missing uuid at ingress"
        is_update = ev.uuid in self.events

        if isinstance(ev, SpanBeginEvent):
            self.span_parent[ev.id] = ev.parent_id

        resolved = self._resolve(ev.span_id)

        # RACE-FIXES.md R3 / WS-race #15: track the in-flight auditor
        # generate's uuid so `Branch.pause()` can retract the stuck
        # `pending=True` bubble on interrupt. Set on the first (pending)
        # emit, cleared on the terminal (`pending=False`) update.
        if (
            isinstance(ev, ModelEvent)
            and resolved is not None
            and resolved[1] == "auditor"
            and (b := self.branches.get(resolved[0])) is not None
        ):
            b._pending_gen_uuid = ev.uuid if ev.pending else None  # noqa: SLF001

        # Splice model: while a forked branch is replaying its *shared*
        # prefix (`tape.log[:prefix_len]`), drop every auditor-role event —
        # `execute_tools` runs live on the served `ModelOutput`, so its
        # `ToolEvent`s have the parent's `tool_call_id`s and would double up
        # in `_anchor_lookup` (D3-no, PETRI-L2-HISTORY). The auditor column
        # splices the parent's events in instead. Target-role events are
        # kept: `_anchor_lookup` dedups `AnchorEvent`s (parent's wins) and no
        # target `ModelEvent` is emitted on a serve, so they're harmless.
        #
        # Gate: drop until the *first auditor `ModelEvent`* for this branch
        # lands — that's either `workbench_auditor`'s inline divergent emit
        # (the edited step) or the first live generate. Once it lands,
        # `by_role[(branch, "auditor")]` exists and every subsequent event
        # passes.
        if (
            resolved is not None
            and resolved[1] == "auditor"
            and (b := self.branches.get(resolved[0])) is not None
            and b.shared_prefix_len > 0
            and resolved not in self.by_role
            and not isinstance(ev, ModelEvent)
        ):
            return

        dumped = self._condense(ev)
        if isinstance(ev, ModelEvent) and not is_update:
            self._emit_pool_delta()

        self.events[ev.uuid] = dumped
        if not is_update and resolved is not None:
            self.by_role.setdefault(resolved, []).append(ev.uuid)
        self._index_anchor(ev, dumped)
        self.version += 1
        self._enqueue(
            {
                "t": "update" if is_update else "event",
                "v": self.version,
                "event": dumped,
            }
        )

        # Rebuild + ship a column's timeline when its structure or content
        # changes: on BranchEvent (new trajectory) and on each new ModelEvent
        # (new turn — not streaming updates). Target additionally rebuilds on
        # AnchorEvent so a forked child's replayed prefix — served without
        # `ModelEvent`s — reaches the wire before the first live turn
        # (RACE-FIXES.md R2 / chaos s4). `_on_event` runs in the branch task's
        # context, so `build_history_timeline` can read `transcript().events`.
        if (
            not is_update
            and resolved is not None
            and (branch := self.branches.get(resolved[0])) is not None
        ):
            role = resolved[1]
            if isinstance(ev, (BranchEvent, ModelEvent)) or (
                role == "target" and isinstance(ev, AnchorEvent)
            ):
                timeline = (
                    self._target_timeline(branch)
                    if role == "target"
                    else self._auditor_timeline()
                )
                # RACE-FIXES.md R2 gap #10: the auditor timeline is
                # session-wide; key it to the *viewed* branch so a background
                # candidate's emit refreshes the foreground column instead of
                # its own (unwatched) slot.
                key = (
                    branch.branch_id
                    if role == "target"
                    else (self.current or branch.branch_id)
                )
                self._enqueue(
                    {
                        "t": "timeline",
                        "v": self.version,
                        "branch": key,
                        "role": role,
                        "timeline": timeline,
                    }
                )

    def _index_anchor(self, ev: Event, dumped: dict[str, Any]) -> None:
        """Maintain `_by_anchor` (petri `_anchor_lookup` over dumped uuids).

        `ModelEvent` is keyed by its assistant ``message.id`` once ``choices``
        arrive (i.e. on the first streaming update); `AnchorEvent` by its
        ``anchor_id`` (one kept per anchor — L1/L2 emits are duplicates).
        """
        assert ev.uuid is not None
        if isinstance(ev, ModelEvent):
            choices = (dumped.get("output") or {}).get("choices") or []
            mid = choices[0].get("message", {}).get("id") if choices else None
            if mid and ev.uuid not in (bucket := self._by_anchor.setdefault(mid, [])):
                bucket.insert(0, ev.uuid)
        elif isinstance(ev, AnchorEvent):
            bucket = self._by_anchor.setdefault(ev.anchor_id, [])
            if not any(self.events[u]["event"] == "anchor" for u in bucket):
                bucket.append(ev.uuid)

    def _resolve(self, span_id: str | None) -> tuple[str, Role] | None:
        """Walk `span_id → parent → …` to the nearest registered role span."""
        cur = span_id
        while cur is not None:
            hit = self.span_role.get(cur)
            if hit is not None:
                return hit
            cur = self.span_parent.get(cur)
        return None

    def _target_timeline(self, branch: Branch) -> dict[str, Any]:
        """Target-column timeline for ``branch``, with in-flight events appended.

        `build_history_timeline` reads ``tape.log``, so it misses (a) a target
        `ModelEvent` while ``pending`` (no anchor yet) and (b) the step whose
        `AnchorEvent` just fired — petri's ``replayable`` emits *before*
        ``tape.log.append``. Both are already in `by_role`: append the former
        by uuid and resolve the latter via `_by_anchor`, onto the tip span.
        Covers target-column streaming and the forked child's replayed prefix,
        which is all case (b) since a serve emits no `ModelEvent`
        (RACE-FIXES.md R2 / chaos s4).
        """
        tl = build_history_timeline(
            branch.history, f"{branch.branch_id}:target"
        ).model_dump(mode="json")
        seen: set[str] = set()

        def tip(span: dict[str, Any]) -> dict[str, Any]:
            seen.update(c["event"] for c in span["content"] if c["type"] == "event")
            t = span
            for b in span["branches"]:
                t = tip(b)
            return t

        t = tip(tl["root"])

        def add(u: str) -> None:
            if u not in seen:
                t["content"].append({"type": "event", "event": u})
                seen.add(u)

        for u in self.by_role.get((branch.branch_id, "target"), []):
            ev = self.events[u]
            if ev["event"] == "model":
                add(u)
            elif ev["event"] == "anchor":
                for r in self._by_anchor.get(ev["anchor_id"], []):
                    add(r)
        return tl

    def _auditor_timeline(self) -> dict[str, Any]:
        return build_auditor_timeline(self)

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
        auditor_tl = self._auditor_timeline() if self.branches else None
        timelines = {
            bid: {"target": self._target_timeline(b), "auditor": auditor_tl}
            for bid, b in self.branches.items()
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
                "parent": b.parent_id,
                "branched_at": b.branched_at,
                "branched_at_turn": b.branched_at_turn,
                "status": b.status,
                "generating": b.generating,
                "seed": b.meta.seed[:80],
                "batch": b.meta.batch,
            }
            for bid, b in self.branches.items()
        }
        return {
            "pool": [m.model_dump(mode="json") for m in self.pool],
            "events": list(self.events.values()),
            "orchestrator": self.orchestrator.view() if self.orchestrator else None,
            "span_role": {sid: list(v) for sid, v in self.span_role.items()},
            "queued": queued,
            "current": self.current,
            "status": self.current_status(),
            "generating": self.current_generating(),
            "branches": branches_meta,
            "candidate_batches": {
                bid: asdict(b) for bid, b in self.candidate_batches.items()
            },
            "timelines": timelines,
        }

    def current_status(self) -> str | None:
        """Status literal of the current branch, or None if no branch."""
        if self.current is None:
            return None
        return self.branches[self.current].status

    def current_generating(self) -> Role | None:
        """Which role the current branch is mid-generate on, or None."""
        if self.current is None:
            return None
        return self.branches[self.current].generating

    async def broadcast_status(self) -> None:
        """Push the current branch's status as a lightweight `status` message."""
        self.version += 1
        await self.broadcast(
            {
                "t": "status",
                "v": self.version,
                "status": self.current_status(),
                # RACE-FIXES.md R3 gap #3,4: which column (if any) is
                # mid-generate — lets `showShimmer` gate on the actual role
                # instead of `status === "running"` (which shimmered the
                # auditor column during a target generate).
                "generating": self.current_generating(),
                "orch_status": self.orchestrator.status if self.orchestrator else None,
            }
        )

    async def push_full_state(self, conn: Connection) -> None:
        await conn.send_json({"t": "state", "v": self.version, **self.view()})

    # -- orchestrator (M1) ----------------------------------------------------

    async def start_orchestrator(
        self,
        *,
        model: str,
        system_prompt: str | None = None,
        model_args: dict[str, Any] | None = None,
        generate_config: dict[str, Any] | None = None,
        audit_defaults: dict[str, Any] | None = None,
        max_turns: int = 10_000,
        resume_messages: list[ChatMessage] | None = None,
        span_id: str | None = None,
        run_log_dirs: list[str] | None = None,
        parent_session_dir: Path | None = None,
        parent_session_id: str | None = None,
        fork_at_turn: int | None = None,
    ) -> None:
        """Construct the M1 `Orchestrator`, spawn its `run()`, broadcast state."""
        from workbench.m1.orchestrator import Orchestrator

        orch = Orchestrator(
            self,
            model=model,
            system_prompt=system_prompt,
            model_args=model_args,
            generate_config=generate_config,
            audit_defaults=audit_defaults,
            max_turns=max_turns,
            resume_messages=resume_messages,
            span_id=span_id,
            run_log_dirs=run_log_dirs,
            parent_session_dir=parent_session_dir,
            parent_session_id=parent_session_id,
            fork_at_turn=fork_at_turn,
        )
        self.orchestrator = orch
        orch.task = asyncio.create_task(orch.run())
        await self.broadcast({"t": "state", "v": self.version, **self.view()})

    async def fork_orchestrator(self, at_turn: int) -> str:
        """P2 — branch the orchestrator at ``at_turn`` into a **new** `Session`.

        Seeds via ``Orchestrator.fork_seed``, closes *this* session (persists
        it and — critically — releases the ``OrchestratorKernel`` singleton so
        the fork's ``run()`` can ``__enter__`` its own), then constructs a
        fresh `Session` under a new ``session_id`` and starts its orchestrator
        with the seed. The parent is evicted from ``server.sessions`` so
        navigating back to it triggers ``Session.load`` (fresh kernel, resume
        note) rather than hitting a closed in-memory husk.

        Registration into ``server.sessions`` is done here (lazy import — the
        reverse edge of the ``server → session`` module dependency) rather
        than returned to the caller, so the fork is reachable the instant the
        frontend's ``{t:"forked"}`` handler reconnects.
        """
        from workbench import server

        assert self.orchestrator is not None, "no orchestrator to fork"
        seed = self.orchestrator.fork_seed(at_turn)
        await self.close()
        if self.session_id is not None:
            server.sessions.pop(self.session_id, None)

        new_id = uuid()
        child = Session(new_id, self.store_dir)
        await child.start()
        server.sessions[new_id] = child
        await child.start_orchestrator(**seed)
        # Let ``run()`` reach ``self.state = state`` (CPU-only up to
        # ``await_step``) so the immediate ``save()`` persists the seeded
        # history — otherwise a crash before the fork's first ``record_turn``
        # would lose it.
        for _ in range(20):
            if child.orchestrator is not None and child.orchestrator.state is not None:
                break
            await asyncio.sleep(0)
        child.save()
        return new_id

    def notify(self, text: str) -> None:
        """Ship a lightweight `{"t":"notify"}` sys-chip to connected clients."""
        self.version += 1
        self._enqueue({"t": "notify", "v": self.version, "text": text})

    def emit(self, ev: Event, *, update: bool = False) -> None:
        """Emit an event onto the session's transcript (M0 pipe).

        Uses the session's transcript directly rather than the
        ``transcript()`` contextvar — an emit from a foreign context (WS
        handler, thread) would otherwise land on a fresh unsubscribed
        ``Transcript`` and be silently lost.

        A stable-id re-``display()`` (e.g. a gate proposal followed by its
        resolved ``Finding`` reusing the same ``display_id``) arrives with
        ``update=False`` but a uuid the transcript already holds; ``_event``
        would raise ``Duplicate event uuid``. The intent of a stable id *is*
        "same slot", so route those to ``_event_updated`` regardless.
        """
        if update or (ev.uuid is not None and ev.uuid in self.events):
            self.transcript._event_updated(ev)  # noqa: SLF001
        else:
            self.transcript._event(ev)  # noqa: SLF001

    def mark_rewound(self, span_id: str, from_uuid: str) -> None:
        """Flag every event in ``span_id``'s role bucket at/after ``from_uuid``.

        Events are never removed (wire is version-monotonic — M1-FEATURES §2);
        the frontend filters on the top-level ``rewound`` key. Broadcasts
        ``{"t":"rewound", span, from_uuid}`` so live clients can filter without
        a full re-fetch; reconnect reads the flag from ``push_full_state``.
        """
        key = self.span_role.get(span_id)
        if key is None:
            return
        ordered = self.by_role.get(key, [])
        try:
            idx = ordered.index(from_uuid)
        except ValueError:
            return
        for u in ordered[idx:]:
            if (e := self.events.get(u)) is not None:
                e["rewound"] = True
        self.version += 1
        self._enqueue(
            {
                "t": "rewound",
                "v": self.version,
                "span": span_id,
                "from_uuid": from_uuid,
            }
        )

    def retract_pending(self, span_id: str, uuid: str) -> None:
        """Drop a stuck ``pending=True`` `ModelEvent` after a cancelled generate.

        RACE-FIXES.md R3 / WS-race #15: `Branch.pause()` cancels
        ``_gen_scope`` mid-generate, so the pending event never gets its
        terminal update — the frontend's spinner would run forever, and the
        orphan event misaligns `eventsToTurns` on the next real generate.
        Flip ``pending=False`` + ``rewound=True`` on the stored dict, drop it
        from ``by_role`` (so `_auditor_timeline` skips it), and ship as
        ``{"t":"update"}`` so live clients replace it in place. The
        ``rewound`` flag is what the M0 columns filter on.
        """
        ev = self.events.get(uuid)
        if ev is None:
            return
        ev["pending"] = False
        ev["rewound"] = True
        key = self.span_role.get(span_id)
        if key is not None and uuid in (bucket := self.by_role.get(key, [])):
            bucket.remove(uuid)
        self.version += 1
        self._enqueue({"t": "update", "v": self.version, "event": ev})

    # -- persistence (#4) -----------------------------------------------------

    @property
    def seed(self) -> str:
        """First root branch's seed — the human-readable session label."""
        for b in self.branches.values():
            if b.parent_id is None:
                return b.meta.seed
        return ""

    def save(self, branch: Branch | None = None) -> None:
        """Persist under ``{store_dir}/{session_id}/`` (see `workbench.persist`)."""
        from workbench.persist import save_session

        save_session(self, branch)

    @classmethod
    async def load(cls, session_id: str, store_dir: Path) -> Session:
        """Reconstruct from ``{store_dir}/{session_id}/`` (see `workbench.persist`)."""
        from workbench.persist import load_session

        return await load_session(session_id, store_dir)


async def _cancel_all(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel each task, then await it swallowing ``CancelledError`` / anything.

    Shared by ``Session.close`` and ``server._stop_running_branches`` — cancel
    first, await second (so a task's ``finally`` sees siblings already
    cancelling), tolerate whatever the awaited task raises on the way out.
    """
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        try:  # noqa: SIM105
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
            pass
