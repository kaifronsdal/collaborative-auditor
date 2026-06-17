"""Branch lifecycle — petri's `run_audit` + `auditor_agent`, outside `inspect eval`, step-gated.

`Branch.run()` sets up the channel / controller / history / audit-tape via
`audit_context()`, registers its auditor and target span ids on the owning
`Session` for routing (STREAMING.md §B), and runs petri's `run_audit()`
(the auditor/target task group) alongside the session's `drain()` task. The
auditor is a stock `auditor_agent()` whose per-turn model call is gated by
`_gated_generate` — the only thing the workbench interposes.

The step gate is an `anyio.Event` the auditor's generate awaits each turn.
`step()` sets it (released for one turn). `play()` sets a free-running
flag; `_gated_generate` re-arms the gate itself after each turn while that
flag holds, so play self-perpetuates without a polling pump task. `pause()`
clears the flag — the next turn waits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import anyio
from inspect_ai.model import (
    CachePolicy,
    ChatMessage,
    ModelOutput,
    get_model,
)
from inspect_ai.tool import Tool
from inspect_petri._auditor import audit_context, auditor_agent, run_audit
from inspect_petri.target import (
    Channel,
    Controller,
    History,
    Tape,
    target_agent,
)
from shortuuid import uuid

from workbench.view import Role, Status

if TYPE_CHECKING:
    from workbench.session import Session


class Branch:
    def __init__(
        self,
        session: Session,
        branch_id: str,
        *,
        seed: str,
        auditor_model: str,
        target_model: str,
        max_turns: int,
    ) -> None:
        self.session = session
        self.branch_id = branch_id
        self.seed = seed
        self.auditor_model = auditor_model
        self.target_model = target_model
        self.max_turns = max_turns

        # petri target plumbing
        self.channel = Channel(seed_instructions=seed)
        self.controller = Controller(self.channel)
        self.history = History()
        self.audit_tape = Tape()

        # step gate — the loop awaits `_gate.wait()` each turn then clears it.
        # `play()` sets `_free_running`; the loop re-sets the gate itself after
        # each turn while that flag holds, so play self-perpetuates without a
        # polling pump and `pause()` takes effect at the next turn boundary.
        self._gate = anyio.Event()
        self._free_running = False

        # user-injected messages awaiting the next turn boundary (STREAMING.md §B).
        self.queued: dict[Role, list[ChatMessage]] = {"auditor": [], "target": []}
        self.status: Status = "idle"
        self.generating: Role | None = None

        # span ids — registered on the session so events route to this branch.
        self.auditor_span_id = uuid()
        self.target_span_id = uuid()
        session.span_role[self.auditor_span_id] = (branch_id, "auditor")
        session.span_role[self.target_span_id] = (branch_id, "target")

    # -- step gate ------------------------------------------------------------

    def step(self) -> None:
        """Release one auditor turn."""
        self._gate.set()

    def play(self) -> None:
        """Run freely: each turn re-arms the gate itself until `pause()`."""
        self._free_running = True
        self._gate.set()

    def pause(self) -> None:
        """Stop after the current turn; the next gate wait blocks."""
        self._free_running = False

    async def _await_turn(self) -> None:
        await self._gate.wait()
        self._gate = anyio.Event()  # anyio.Event is one-shot; replace to re-arm

    # -- run ------------------------------------------------------------------

    async def run(self) -> None:
        # models — force streaming so provider partial-output flushes fire
        # (default "auto" only streams with reasoning or large max_tokens).
        auditor_model = get_model(self.auditor_model, streaming=True)
        target_model = get_model(self.target_model, streaming=True)

        # petri's full auditor agent, with the per-turn generate gated by our
        # step gate. `auditor_agent` owns the system/user prompt, tools, the
        # turn loop, eager-resume and end_conversation; we only interpose the
        # gate + queued-message injection via the `generate=` hook (which now
        # receives petri's tape-wrapped generate, so the call still records
        # onto the audit tape).
        auditor = auditor_agent(
            generate=self._gated_generate,
            max_turns=self.max_turns,
            compaction=False,
            realism_filter=False,
            eager_resume=True,
        )

        self.status = "running"

        # F2: every contextvar children inherit must be set in THIS parent
        # context, before create_task_group(). `audit_context()` is petri's
        # single CM for that. The session installed `transcript` already (it
        # owns and subscribes to it across branches), so we don't pass it here.
        with audit_context(
            controller=self.controller,
            audit_tape=self.audit_tape,
            active_model=target_model,
            model_roles={"auditor": auditor_model, "target": target_model},
        ):
            async with anyio.create_task_group() as outer:
                outer.start_soon(self.session.drain)

                await run_audit(
                    auditor=auditor,
                    target=target_agent(),
                    channel=self.channel,
                    history=self.history,
                    audit_tape=self.audit_tape,
                    auditor_span_id=self.auditor_span_id,
                    target_span_id=self.target_span_id,
                )

                self.status = "ended"
                self.generating = None
                self._free_running = False
                # let drain flush anything still queued from the final events,
                # then cancel it so run() returns. (Closing the send stream
                # would be cleaner but `drain` is Session-owned and shared.)
                await anyio.sleep(0.1)
                outer.cancel_scope.cancel()

    async def _gated_generate(
        self,
        generate: Callable[..., Awaitable[ModelOutput]],
        messages: list[ChatMessage],
        tools: list[Tool],
        cache: bool | CachePolicy,
    ) -> ModelOutput:
        """Per-turn auditor generate, gated by the step gate (STREAMING.md §B).

        Awaits the gate (one release per `step()`, self-perpetuating under
        `play()`), folds any user-injected messages in before the model sees
        them — the injected `id` survives into `ModelEvent.input` → pool so the
        frontend reconciles the ghost bubble by id — then calls petri's
        tape-wrapped `generate` so the output still lands on the audit tape.
        """
        await self._await_turn()  # ← the step gate
        messages.extend(self.queued["auditor"])
        self.queued["auditor"].clear()
        self.generating = "auditor"
        try:
            return await generate(input=messages, tools=tools, cache=cache)
        finally:
            self.generating = None
            # free-running: re-arm the gate so the next turn proceeds
            # immediately. A `pause()` between turns clears the flag and the
            # next `_await_turn` blocks.
            if self._free_running:
                self._gate.set()
