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
from inspect_ai._util.json import jsonable_python  # noqa: PLC2701
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.event import InfoEvent
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
from inspect_ai.tool._tools._execute import code_viewer  # noqa: PLC2701
from inspect_ai.util import span
from inspect_ai.util._display import init_display_type  # noqa: PLC2701
from shortuuid import uuid

from workbench.gate import StepGated
from workbench.m1.kernel import DisplayEvent, OrchestratorKernel
from workbench.m1.wb import Workbench
from workbench.view import Status

if TYPE_CHECKING:
    from workbench.session import Session

logger = logging.getLogger(__name__)

ORCH_SOURCE = "orchestrator"

#: Cap on any single MIME payload shipped to the wire. Plotly's default
#: renderer inlines ``plotly.min.js`` (~5 MB, twice) into ``text/html``;
#: without a cap one figure is ~10 MB in ``session.events`` + WS. The real
#: fix is ``include_plotlyjs=False`` in the workbench pio template
#: (M1-PLOTTING.md); this is the safety net for anything else.
_WIRE_MIME_CAP = 128 * 1024


def _wire_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Cap oversize MIME strings and coerce to JSON-safe.

    ``jsonable_python`` recurses (so a ``numpy.int64`` *inside* the vendor-
    MIME dict is coerced too — a top-level ``isinstance`` check would let it
    through and ``InfoEvent(data=…)`` would ``ValidationError``).
    """
    out: dict[str, Any] = jsonable_python(bundle) or {}
    for mime, v in list(out.items()):
        if isinstance(v, str) and len(v) > _WIRE_MIME_CAP:
            out[mime] = v[:_WIRE_MIME_CAP] + f"\n<!-- truncated {len(v)} bytes -->"
    return out


#: Process-wide guard (M1-KERNEL-NOTES.md §3): the ``InteractiveShell``
#: singleton and ``sys.stdout`` tee mean at most one live kernel per process.
_LIVE: "Orchestrator | None" = None

#: Appended to a resumed agent's history (M1.3 persistence): the kernel's
#: ``user_ns`` doesn't survive ``Session.save``/``load`` — same as a Jupyter
#: kernel restart — so any names the pre-save cells bound are gone. The note
#: points at ``RunHandle.log_dir``s seen in the saved event stream so the
#: agent can re-read results without re-running the evals.
KERNEL_RESTART_NOTE = (
    "[kernel restarted — previous Python bindings lost. RunHandle logs at: "
    "{dirs}. Re-read via audits_df(log_dir) or wb.run_eval results.]"
)


class Orchestrator(StepGated):
    """One M1 orchestrator: kernel + agent loop + span, owned by a `Session`."""

    def __init__(
        self,
        session: "Session",
        *,
        model: str,
        system_prompt: str = "",
        model_args: dict[str, Any] | None = None,
        max_turns: int = 10_000,
        resume_messages: list[ChatMessage] | None = None,
        span_id: str | None = None,
        run_log_dirs: list[str] | None = None,
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
        #: Persistence (M1.3): pre-save chat history to prepend on resume,
        #: and any ``RunHandle.log_dir``s the pre-save cells produced.
        self._resume_messages = resume_messages
        self.run_log_dirs: list[str] = list(run_log_dirs or [])

        self.span_id = span_id or uuid()
        session.span_role[self.span_id] = ("orch", "orch")

        # Pay inspect's cold-start cost (display type, hooks banner) once,
        # before the first cell runs — otherwise the first in-cell
        # ``eval_async`` leaks ~8 stream events (M1-RUN-AUDITS.md §Required).
        _prewarm()
        self.kernel = OrchestratorKernel(
            extra_ns={"SESSION": session}, on_display=self._on_display
        )
        self.kernel.shell.user_ns["wb"] = Workbench(self.kernel, session)
        self._init_gate()
        self.status: Status = "idle"
        self.queued: list[ChatMessage] = []
        #: The live agent state; set once ``run()`` enters its span. Read by
        #: ``m1.persist.save_orchestrator`` for the resume ``messages``.
        self.state: AgentState | None = None
        #: The detached ``run()`` task; set by ``Session.start_orchestrator``.
        self.task: asyncio.Task[None] | None = None

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
            self.session.notify(ev.bundle["text/plain"])
            return
        ie = InfoEvent(
            source=ORCH_SOURCE,
            data={
                "id": ev.id,
                "turn": ev.turn_id,
                "bundle": _wire_bundle(ev.bundle),
                "meta": ev.meta,
                "stable": ev.stable,
            },
        )
        if ev.stable:
            # ``dh.update()`` re-emits with the same ``display_id`` → same
            # ``uuid`` → ``session._on_event`` sees ``is_update=True`` and
            # ships ``{"t":"update"}`` — the M0 path, unchanged.
            ie.uuid = ev.id
        self.session.emit(ie, update=ev.update)

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
                self.state = state
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
            await self.session.broadcast_status()
            self.kernel.restore_streams()
            global _LIVE
            if _LIVE is self:
                _LIVE = None

    def _initial_messages(self) -> list[ChatMessage]:
        if self._resume_messages is not None:
            dirs = ", ".join(self.run_log_dirs) or "(none)"
            note = ChatMessageUser(content=KERNEL_RESTART_NOTE.format(dirs=dirs))
            return [*self._resume_messages, note]
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

    # -- composer → orchestrator ---------------------------------------------

    def send(self, text: str) -> None:
        """Composer → orchestrator: enqueue a user message and release one turn.

        Also detaches the current foreground cell if one is running — per
        M1-NOTEBOOK.md §Background execution, typing in the composer while a
        cell runs means "background it and deliver this".
        """
        self.kernel.detach()
        self.queued.append(ChatMessageUser(content=text))
        self.step()


# -- the agent ---------------------------------------------------------------


def python_tool(orch: Orchestrator) -> Tool:
    @tool(viewer=code_viewer("python", "code"))
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
                await orch.await_step()
                state.messages.extend(orch.queued)
                orch.queued.clear()

                state.output = await model.generate(input=state.messages, tools=tools)
                state.messages.append(state.output.message)
                orch.rearm()

                if state.output.message.tool_calls:
                    messages, _ = await execute_tools(
                        messages=state.messages, tools=tools
                    )
                    state.messages.extend(messages)
                elif not orch.queued:
                    # No tool call and nothing queued — park until the human
                    # sends something (composer → `orch_send`).
                    orch.pause()
            return state

        return execute

    return _factory()


# -- pre-warm (call once at kernel init; M1-RUN-AUDITS.md §Required) ----------


def _prewarm() -> None:
    """Suppress inspect's progress display and pay the hooks-banner once.

    ``init_display_type("none")`` stops ``eval_async`` writing progress to
    stdout (which ``_CellStream`` would capture as stream events).
    ``platform_init()`` prints the aisitools hooks banner idempotently — do
    it here so the first in-cell ``eval_async`` doesn't leak 8 stream lines.
    """
    init_display_type("none")
    from inspect_ai._util.platform import platform_init  # noqa: PLC0415, PLC2701

    platform_init()
    # scout has its own display registry (SCOUT_DISPLAY); silence it too so
    # ``wb.scan`` doesn't leak a rich progress bar into ``_CellStream``.
    try:
        from inspect_scout._display._display import (  # noqa: PLC0415, PLC2701
            init_display_type as scout_init_display,
        )

        scout_init_display("none")
    except ImportError:
        pass
    # plotly's default IPython renderer inlines the full ``plotly.min.js``
    # (~4.8 MB, twice) into ``text/html`` on every figure. The workbench
    # frontend loads ``plotly.js-basic-dist-min`` once (M1-PLOTTING.md), so
    # figures should emit only the ~9 KB div + data. ``_wire_bundle``'s
    # 128 KB cap is the safety net; this is the real fix.
    try:
        import plotly.io as pio  # noqa: PLC0415

        r = pio.renderers["notebook_connected"]
        r.connected = True  # CDN <script src>, not inline bundle
        pio.renderers.default = "notebook_connected"
    except ImportError:
        pass
