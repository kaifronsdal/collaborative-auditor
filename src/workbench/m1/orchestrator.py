"""M1.1 — the orchestrator agent + kernel↔Session wire integration.

An `Orchestrator` is to the M1 orchestrator column what `Branch` is to the M0
auditor column: it owns a span registered in `session.span_role`, a step gate,
and a `run()` coroutine that drives an inspect `Agent` loop. The agent has one
tool — ``python(code, background)`` — whose body is
``kernel.run_turn(code).text``.

The kernel↔wire bridge is ``_on_display``: every ``DisplayEvent`` becomes an
``InfoEvent(source="orchestrator", data={bundle, …})`` emitted via
``transcript()._event()`` (or ``_event_updated`` for ``dh.update()``). That
puts kernel outputs on the *same* pipe as M0's ``ModelEvent``/``ToolEvent``
stream — ``Session._on_event`` dumps them into ``session.events`` and ships
``{"t":"event"|"update", "v":…}`` — so reconnect (``push_full_state``),
persistence, and version-monotonicity all work with zero changes to the M0
event plumbing. For stable displays we set ``InfoEvent.uuid = display_id`` so
``dh.update()`` reuses M0's existing ``is_update = ev.uuid in self.events``
path and lands as ``{"t":"update"}`` on the wire.

We use ``InfoEvent`` as the carrier rather than a bespoke ``DisplayEvent``
subclass because inspect's ``Event`` union is closed and discriminated for
``.eval`` deserialization — a foreign discriminator would break
``Session.load``. If/when a first-class display event is upstreamed the
change is one function.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import anyio
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.event import InfoEvent
from inspect_ai.log import transcript
from inspect_ai.log._transcript import init_transcript  # noqa: PLC2701
from inspect_ai.model import (
    ChatMessage,
    ChatMessageSystem,
    ChatMessageUser,
    Model,
    execute_tools,
    get_model,
)
from inspect_ai.tool import Tool, tool
from inspect_ai.util import span
from shortuuid import uuid

from workbench.m1.kernel import DisplayEvent, OrchestratorKernel
from workbench.view import Status

if TYPE_CHECKING:
    from workbench.session import Session

logger = logging.getLogger(__name__)

ORCH_SOURCE = "orchestrator"

#: Process-wide guard (M1-KERNEL-NOTES.md §3): the ``InteractiveShell``
#: singleton and ``sys.stdout`` tee mean at most one live kernel per process.
_LIVE: "Orchestrator | None" = None


class Orchestrator:
    """One M1 orchestrator: kernel + agent loop + span, owned by a `Session`."""

    def __init__(
        self,
        session: "Session",
        *,
        model: str,
        system_prompt: str = "",
        model_args: dict[str, Any] | None = None,
        max_turns: int = 10_000,
    ) -> None:
        global _LIVE
        if _LIVE is not None:
            raise RuntimeError(
                "one M1 orchestrator per process (InteractiveShell singleton)"
            )
        _LIVE = self

        self.session = session
        self.model_name = model
        self.model_args = model_args or {}
        self.system_prompt = system_prompt
        self.max_turns = max_turns

        self.span_id = uuid()
        session.span_role[self.span_id] = ("orch", "orch")

        self.kernel = OrchestratorKernel(
            extra_ns={"SESSION": session}, on_display=self._on_display
        )
        # Step gate — same shape as `Branch._gate`: the agent loop awaits it
        # each turn, `play()` self-re-arms.
        self._gate = anyio.Event()
        self._free_running = False
        self.status: Status = "idle"
        self.queued: list[ChatMessage] = []

    # -- kernel → wire bridge -------------------------------------------------

    def _on_display(self, ev: DisplayEvent) -> None:
        """Ship a kernel output through the M0 event pipe.

        Called synchronously from ``kernel._emit`` inside the cell task, so
        ``transcript()`` and ``current_span_id()`` resolve to this
        orchestrator's context. Notifications (``meta.sys``) are *not*
        routed here — they're agent-input chips, not display cards, and the
        done-callback that emits them runs outside the cell task's context.
        """
        if ev.meta.get("sys"):
            self.session._enqueue({"t": "notify", "text": ev.bundle["text/plain"]})  # noqa: SLF001
            return
        ie = InfoEvent(
            source=ORCH_SOURCE,
            data={
                "id": ev.id,
                "turn": ev.turn_id,
                "bundle": ev.bundle,
                "meta": ev.meta,
                "stable": ev.stable,
            },
        )
        if ev.stable:
            # ``dh.update()`` re-emits with the same ``display_id`` → same
            # ``uuid`` → ``session._on_event`` sees ``is_update=True`` and
            # ships ``{"t":"update"}`` — the M0 path, unchanged.
            ie.uuid = ev.id
        if ev.update:
            transcript()._event_updated(ie)  # noqa: SLF001
        else:
            transcript()._event(ie)  # noqa: SLF001

    # -- step gate (identical shape to Branch) --------------------------------

    def step(self) -> None:
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def play(self) -> None:
        self._free_running = True
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def pause(self) -> None:
        self._free_running = False
        if self.status != "ended":
            self.status = "paused"

    # -- run ------------------------------------------------------------------

    async def run(self) -> None:
        """Drive the orchestrator agent inside its span.

        Mirrors `Branch.run()`: install the session's transcript in *this*
        task's context (so cell tasks — which copy the context at creation —
        emit onto it), open the orchestrator span (so every ``InfoEvent``
        auto-resolves to it via ``current_span_id()``), and run the agent
        loop until ``max_turns`` or cancellation.
        """
        init_transcript(self.session.transcript)
        self.status = "paused"
        try:
            model = get_model(self.model_name, **self.model_args)
            agent_fn = orchestrator_agent(self, model)
            async with span(ORCH_SOURCE, type=ORCH_SOURCE, id=self.span_id):
                state = AgentState(messages=self._initial_messages())
                await agent_fn(state)
        except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
            pass
        except Exception as exc:
            logger.exception("orchestrator run failed")
            await self.session.broadcast(
                {"t": "error", "v": self.session.version, "message": str(exc)}
            )
        finally:
            self.status = "ended"
            self.kernel.restore_streams()
            global _LIVE
            if _LIVE is self:
                _LIVE = None

    def _initial_messages(self) -> list[ChatMessage]:
        msgs: list[ChatMessage] = []
        if self.system_prompt:
            msgs.append(ChatMessageSystem(content=self.system_prompt))
        return msgs

    # -- view (for Session.view()) --------------------------------------------

    def view(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "status": self.status,
            "pending_gates": list(self.kernel.pending),
            "bg_cells": sorted(self.kernel.bg),
            "notifications": list(self.kernel.notifications),
        }


# -- the agent ---------------------------------------------------------------


def python_tool(orch: Orchestrator) -> Tool:
    @tool
    def python() -> Tool:
        async def execute(code: str, background: bool = False) -> str:
            """Run a Python cell in the orchestrator kernel.

            The kernel is a persistent IPython shell: names bound in one
            cell are visible in later cells. The last expression's value
            (and anything passed to ``display()``) is returned as text.
            ``background=True`` returns immediately; a ``[cell-N done]``
            note appears in a later turn's input when it finishes.

            Args:
                code: Python source. Top-level ``await`` is allowed.
                background: If True, don't wait for the cell to finish.
            """
            notes = orch.kernel.drain_notifications()
            r = await orch.kernel.run_turn(code, background=background)
            head = ("\n".join(notes) + "\n\n") if notes else ""
            return head + r.text

        return execute

    return python()


def orchestrator_agent(orch: Orchestrator, model: Model) -> Agent:
    """The M1 orchestrator's agent loop — ``generate → execute_tools`` gated
    per turn, with human-queued messages drained before each generate."""
    tools = [python_tool(orch)]

    @agent
    def _factory() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            for _ in range(orch.max_turns):
                await orch._gate.wait()  # noqa: SLF001
                orch._gate = anyio.Event()  # noqa: SLF001
                state.messages.extend(orch.queued)
                orch.queued.clear()

                state.output = await model.generate(input=state.messages, tools=tools)
                state.messages.append(state.output.message)
                if orch._free_running:  # noqa: SLF001
                    orch._gate.set()  # noqa: SLF001

                if state.output.message.tool_calls:
                    messages, _ = await execute_tools(
                        messages=state.messages, tools=tools
                    )
                    state.messages.extend(messages)
                elif not orch.queued:
                    # No tool call and nothing queued — park until the human
                    # sends something (composer → `orch_send`).
                    orch.status = "paused"
                    orch._free_running = False  # noqa: SLF001
            return state

        return execute

    return _factory()


def send(orch: Orchestrator, text: str) -> None:
    """Composer → orchestrator: enqueue a user message and release one turn.

    Also detaches the current foreground cell if one is running — per
    M1-NOTEBOOK.md §Background execution, typing in the composer while a
    cell runs means "background it and deliver this".
    """
    orch.kernel.detach()
    orch.queued.append(ChatMessageUser(content=text))
    orch.step()
