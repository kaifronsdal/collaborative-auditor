"""M1.1 — the orchestrator agent + kernel↔Session wire integration.

An `Orchestrator` is to the M1 orchestrator column what `Branch` is to the M0
auditor column: it owns a span registered in `session.span_role`, a step gate,
and a `run()` coroutine that drives an inspect `Agent` loop. The agent has
eight tools: ``python(code, background)`` (whose body is
``kernel.run_turn(code).text``) plus the seven from ``make_tools`` — ``bash``
(subprocess evals), ``read_file``/``write_file``/``edit_file``, and
``ask_human``/``review_seeds``/``review_finding`` (gate cards).

The kernel↔wire bridge is ``_on_display``: every ``DisplayEvent`` becomes an
``InfoEvent(source="orchestrator", data={bundle, …})`` emitted via
``session.emit()``. That puts kernel outputs on the *same* pipe as M0's
``ModelEvent``/``ToolEvent`` stream — ``Session._on_event`` dumps them into
``session.events`` and ships ``{"t":"event"|"update", "v":…}`` — so reconnect
(``push_full_state``), persistence, and version-monotonicity all work with
zero changes to the M0 event plumbing. For stable displays we set
``InfoEvent.uuid = display_id`` so ``dh.update()`` reuses ``session.emit``'s
``ev.uuid in self.events`` update path and lands as ``{"t":"update"}`` on the
wire.

We use ``InfoEvent`` as the carrier rather than a bespoke ``DisplayEvent``
subclass because inspect's ``Event`` union is closed and discriminated for
``.eval`` deserialization — a foreign discriminator would break
``Session.load``. If/when a first-class display event is upstreamed the
change is one function.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import anyio
from inspect_ai._util.json import jsonable_python
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.event import InfoEvent
from inspect_ai.log._transcript import init_transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    execute_tools,
    get_model,
)
from inspect_ai.tool import Tool, tool
from inspect_ai.tool._tools._execute import code_viewer
from inspect_ai.util import span
from inspect_ai.util._display import init_display_type
from shortuuid import uuid

from workbench import config
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.plots import install_template
from workbench.m1.prompt import FALLBACK_AUDIT_DEFAULTS, build_system_prompt
from workbench.m1.proposals import Gate
from workbench.m1.tools import make_tools
from workbench.m1.wb import Workbench
from workbench.m1.wire import DisplayEvent
from workbench.step import StepGated
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


#: Appended to a resumed agent's history (M1.3 persistence): the kernel's
#: ``user_ns`` doesn't survive ``Session.save``/``load`` — same as a Jupyter
#: kernel restart — so any names the pre-save cells bound are gone. The note
#: points at eval ``log_dir``s seen in the saved event stream so the agent
#: can re-read results without re-running the evals.
KERNEL_RESTART_NOTE = (
    "[kernel restarted — previous Python bindings lost. Eval logs at: "
    "{dirs}. Re-read via wb.attach(log_dir).audits or audits_df(log_dir).]"
)

#: P0.4 — synthetic tool_result for a tool_call that was mid-flight (gate
#: pending, subprocess running) when the session was saved. Injected by
#: ``messages_for_save`` so a resumed ``generate`` doesn't hit the provider's
#: unpaired-tool_call error; the text tells the agent nothing happened.
RESTART_TOOL_RESULT = (
    "[session restarted before this completed — please re-propose if still relevant]"
)


class Orchestrator(StepGated):
    """One M1 orchestrator: kernel + agent loop + span, owned by a `Session`."""

    def __init__(
        self,
        session: Session,
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
    ) -> None:
        self.session = session
        self.model_name = model
        self.model_args = model_args or {}
        #: P1.1/P1.5 — open ``GenerateConfig`` dict from the orchestrator
        #: ``ModelPicker`` (reasoning_effort, temperature, or any other key
        #: ``GenerateConfig`` accepts). ``**``'d into ``GenerateConfig`` at
        #: ``model.generate`` time so unknown keys surface as a construction
        #: error rather than silently vanishing.
        self.generate_config = generate_config or {}
        #: P1.2 — per-session audit-role defaults (target/auditor/judge/
        #: max_turns/judge_dimensions + optional per-role ``*_config``).
        #: Interpolated into the system prompt so the LLM sees concrete ids,
        #: and exposed as ``wb.DEFAULTS`` in ``user_ns`` for ``python`` cells.
        self.audit_defaults = {**FALLBACK_AUDIT_DEFAULTS, **(audit_defaults or {})}
        self.system_prompt = (
            build_system_prompt(self.audit_defaults)
            if system_prompt is None
            else system_prompt
        )
        self.max_turns = max_turns
        #: Persistence (M1.3): pre-save chat history to prepend on resume,
        #: and any ``eval_run`` log dirs the pre-save turns produced.
        self._resume_messages = resume_messages
        self.run_log_dirs: list[str] = list(run_log_dirs or [])

        self.span_id = span_id or uuid()
        session.span_role[self.span_id] = ("orch", "orch")
        #: cwd for the ``bash``/file tools (M1-HYBRID §bash) and the base
        #: for relative ``wb.attach("runs/…")`` paths — same dir both sides
        #: so ``bash("… --log-dir runs/r1")`` and ``wb.attach("runs/r1")``
        #: agree without the agent thinking about paths. Rooted at
        #: `config.sessions_dir()` (P0.3) so ``--store-dir`` / ``WORKBENCH_STORE``
        #: governs eval logs and ``write_file`` artifacts, not just the M0
        #: branch JSON.
        self.session_dir = config.sessions_dir() / self.span_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        # Align the ``python`` kernel's cwd with ``bash``/file tools —
        # otherwise a file the agent writes in a python cell lands in the
        # server's cwd (repo root), not ``session_dir``, and its next
        # ``bash("cat that_file")`` misses (M1-HYBRID e2e-v1 root cause).
        os.chdir(self.session_dir)

        # Pay inspect's cold-start cost (display type, hooks banner) once,
        # before the first cell runs — the hooks banner is idempotent but
        # would otherwise land in ``_CellStream`` on the first ``wb.scan``.
        _prewarm()
        install_template()
        self.gate = Gate(on_change=self._broadcast_status_soon)
        self.kernel = OrchestratorKernel(
            extra_ns={"SESSION": session}, on_display=self._on_display
        )
        wb = Workbench(
            self.gate,
            session_dir=str(self.session_dir),
            session_id=session.session_id or "",
        )
        wb.DEFAULTS = dict(self.audit_defaults)
        # P1.8(a): expose the scanner library + named groups so cells (and
        # the prompt block below) can list what ``wb.scan(logs, "name")``
        # resolves to. Fail-soft — a broken user scanner file shouldn't
        # block orchestrator construction.
        try:
            from workbench.m1 import scanners as _scanners

            wb.SCANNERS = sorted(_scanners.load_library())
            wb.SCANNER_GROUPS = _scanners.load_groups()
        except Exception:
            logger.exception("scanner library load failed")
        self.kernel.shell.user_ns["wb"] = wb
        self.kernel.shell.user_ns.update(_seed_analysis_ns())
        self._init_gate()
        self._status: Status = "idle"
        self.queued: list[ChatMessage] = []
        #: kernel ``turn_id`` → the assistant ``ChatMessage.id`` whose
        #: ``python`` tool_call produced it. ``rewind(N)`` uses this to find
        #: where in ``state.messages`` (and ``session.events``) turn N starts.
        self._turn_msg: dict[int, str] = {}
        #: Set by ``rewind()``; ``_apply_rewind`` truncates ``state.messages``
        #: at the top of the next agent-loop iteration (deferred so the
        #: cancelled cell's tool-result extend, which happens in the orch task
        #: after ``rewind()`` returns, is dropped too).
        self._rewind_to: int | None = None
        #: The live agent state; set once ``run()`` enters its span. Read by
        #: ``m1.persist.save_orchestrator`` for the resume ``messages``.
        self.state: AgentState | None = None
        #: The detached ``run()`` task; set by ``Session.start_orchestrator``.
        self.task: asyncio.Task[None] | None = None
        #: P0.5 — every live ``bash`` subprocess (fg + bg). ``bash_tool``
        #: adds on spawn and discards on ``proc.wait()``; ``close()`` sends
        #: SIGTERM to each process group so a server restart doesn't orphan
        #: an ``inspect eval`` that's still burning tokens.
        self._bash_procs: set[asyncio.subprocess.Process] = set()

    @property
    def status(self) -> Status:
        # ``"waiting"`` is a UI-facing overlay on the underlying ``_status``:
        # ``session.broadcast_status()`` reads this attribute directly, so the
        # overlay must live here (not just in ``view()``) for the header to
        # flip on gate open/close. Preserve ``"ended"`` so ``StepGated``'s
        # ``!= "ended"`` guards still hold if a bg cell's gate outlives run().
        if self._status != "ended" and self.gate.pending:
            return "waiting"
        return self._status

    @status.setter
    def status(self, value: Status) -> None:
        self._status = value

    # -- kernel → wire bridge -------------------------------------------------

    def _on_display(self, ev: DisplayEvent) -> None:
        """Ship a kernel output through the M0 event pipe.

        Called synchronously from ``kernel.emit`` inside the cell task, so
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

    def _broadcast_status_soon(self) -> None:
        """``Gate.on_change`` hook — push ``status`` the moment a gate opens/closes.

        Without this the ``"waiting"`` overlay only surfaces on the next
        ``push_full_state``. Called sync from inside ``Gate.__call__`` (event
        loop is running); guarded on ``self.task`` so a gate that fires before
        ``run()`` is spawned doesn't broadcast into a half-built session.
        """
        if self.task is not None:
            asyncio.create_task(self.session.broadcast_status())  # noqa: RUF006

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
        # ``send()``/``play()`` may have already set ``running`` before this
        # task reached here (same guard as ``Branch.run()``); only fall back
        # to paused if we're still at the constructor default.
        if self.status == "idle":
            self.status = "paused"
        # Kernel-as-CM: ``__enter__`` seizes the process-global resources
        # (``InteractiveShell`` hooks, ``sys.stdout/stderr``) and enforces the
        # one-per-process guard; ``__exit__`` restores + releases. Paired
        # lexically here so an exception before the agent loop still restores.
        with self.kernel:
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

    def messages_for_save(self) -> list[ChatMessage]:
        """``state.messages`` with any pending ``rewind()`` truncation applied.

        A ``rewind()`` that hasn't been ``_apply_rewind``-ed yet (save landed
        between the WS-task ``rewind`` and the orch-task loop tick) still has
        the discarded turns in ``state.messages`` — truncate here so the
        resumed agent doesn't re-read them.
        """
        messages = list(self.state.messages) if self.state is not None else []
        if self._rewind_to is not None:
            msg_id = self._turn_msg.get(self._rewind_to)
            idx = next((i for i, m in enumerate(messages) if m.id == msg_id), None)
            if idx is not None:
                messages = messages[:idx]
        # P0.4: a save mid-``execute_tools`` (gate pending, bash running,
        # server SIGTERM'd) leaves the last assistant's tool_calls without
        # paired ``ChatMessageTool`` results. The resumed ``generate`` would
        # 400 on that. Synthesise a placeholder result per unpaired call —
        # preserves the model's reasoning in the assistant turn and tells it
        # explicitly that the call never ran.
        last_a = next(
            (
                (i, m)
                for i, m in reversed(list(enumerate(messages)))
                if isinstance(m, ChatMessageAssistant)
            ),
            None,
        )
        if last_a is not None and last_a[1].tool_calls:
            i, a = last_a
            paired = {
                m.tool_call_id
                for m in messages[i + 1 :]
                if isinstance(m, ChatMessageTool)
            }
            for tc in a.tool_calls:
                if tc.id not in paired:
                    messages.append(
                        ChatMessageTool(
                            content=RESTART_TOOL_RESULT,
                            tool_call_id=tc.id,
                            function=tc.function,
                        )
                    )
        return messages

    def close(self) -> None:
        """Reap every tracked ``bash`` subprocess (P0.5).

        Called from ``Session.close()`` after the ``run()`` task is cancelled
        (so nothing spawns more). Each proc was started with
        ``start_new_session=True`` → its pgid == its pid, so ``killpg``
        reaches the whole subtree (``inspect eval`` → docker sandboxes).
        """
        for proc in list(self._bash_procs):
            if proc.returncode is None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGTERM)
        self._bash_procs.clear()

    def _initial_messages(self) -> list[ChatMessage]:
        if self._resume_messages is not None:
            dirs = ", ".join(self.run_log_dirs) or "(none)"
            note = ChatMessageUser(content=KERNEL_RESTART_NOTE.format(dirs=dirs))
            return [*self._resume_messages, note]
        return [ChatMessageSystem(content=self.system_prompt)]

    # -- view (for Session.view()) --------------------------------------------

    def view(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "status": self.status,
            "model": self.model_name,
            "pending_gates": list(self.gate.pending),
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

    def record_turn(self, tid: int) -> None:
        """Map a kernel turn id to the assistant message currently executing.

        Called from ``python_tool`` and ``tools._turn`` while ``execute_tools``
        is servicing ``state.output``; ``rewind()`` uses the mapping to find
        where in ``state.messages`` (and ``session.events``) turn N starts.
        """
        if (
            self.state is not None
            and self.state.output is not None
            and (mid := self.state.output.message.id) is not None
        ):
            self._turn_msg[tid] = mid
        # P0.1: a pure-M1 session's only other save trigger is
        # ``Branch.run()``'s finally, which never fires without an M0 branch.
        # Persist per orchestrator turn so a server restart loses at most the
        # in-flight tool result. No-op when ``store_dir``/``session_id`` unset.
        self.session.save()

    # -- rewind (M1-FEATURES §2) ---------------------------------------------

    async def rewind(self, turn: int) -> None:
        """Discard turn ``N`` onward: cancel cells, mark events, park.

        Event marking is immediate (frontend filters on ``rewound``); the
        ``state.messages`` truncate is deferred to ``_apply_rewind`` at the top
        of the next agent-loop iteration so the cancelled cell's tool-result
        (which the orch task appends *after* this returns) is dropped too.
        Kernel ``user_ns`` is *not* rolled back — same as Jupyter "run all
        above".
        """
        self.pause()
        # Cancel every running cell and let the orch task drain ``_settle``'s
        # emits (traceback / ``cell_done``) so ``mark_rewound`` catches them.
        tasks = list(self.kernel.bg.values())
        for tid in list(self.kernel.bg):
            self.kernel.cancel(tid)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            for _ in range(5):
                await asyncio.sleep(0)
        self.queued.clear()
        msg_id = self._turn_msg.get(turn)
        if msg_id is None:
            logger.warning("rewind(%d): no assistant message mapping", turn)
            return
        from_uuid = self._find_model_event_uuid(msg_id)
        if from_uuid is not None:
            self.session.mark_rewound(self.span_id, from_uuid)
        marker = InfoEvent(
            source=ORCH_SOURCE, data={"kind": "rewind_marker", "to_turn": turn}
        )
        # Emitted from the WS task, outside the orch span — set span_id
        # explicitly so ``session._resolve`` routes it to ``("orch","orch")``.
        marker.span_id = self.span_id
        self.session.emit(marker)
        self._rewind_to = turn
        await self.session.broadcast_status()

    def _apply_rewind(self) -> None:
        assert self._rewind_to is not None and self.state is not None
        turn, self._rewind_to = self._rewind_to, None
        msg_id = self._turn_msg.get(turn)
        if msg_id is not None:
            idx = next(
                (i for i, m in enumerate(self.state.messages) if m.id == msg_id), None
            )
            if idx is not None:
                del self.state.messages[idx:]
        for t in [t for t in self.kernel.outputs if t >= turn]:
            del self.kernel.outputs[t]
        for t in [t for t in self._turn_msg if t >= turn]:
            del self._turn_msg[t]

    def _find_model_event_uuid(self, msg_id: str) -> str | None:
        """The ``ModelEvent.uuid`` whose output message id is ``msg_id``."""
        for ev_uuid in self.session.by_role.get(("orch", "orch"), []):
            e = self.session.events.get(ev_uuid)
            if e is None or e.get("event") != "model":
                continue
            out = e.get("output") or {}
            choices = out.get("choices") or [{}]
            if (choices[0].get("message") or {}).get("id") == msg_id:
                return ev_uuid
        return None


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
            orch.record_turn(r.turn_id)
            head = ("\n".join(notes) + "\n\n") if notes else ""
            tail = "" if r.detached else f"\n[{r.duration:.1f}s]"
            return head + r.text + tail

        return execute

    return python()


def orchestrator_agent(orch: Orchestrator, model: Model) -> Agent:
    """The M1 orchestrator's agent loop — ``generate → execute_tools`` gated
    per turn, with human-queued messages drained before each generate."""
    tools = [python_tool(orch), *make_tools(orch)]
    gen_config = GenerateConfig(**orch.generate_config)

    @agent
    def _factory() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            for turn in range(orch.max_turns):
                await orch.await_step()
                if orch._rewind_to is not None:  # noqa: SLF001
                    orch._apply_rewind()  # noqa: SLF001
                state.messages.extend(orch.queued)
                orch.queued.clear()

                # A transient API error (500, overloaded, connection reset)
                # during ``generate`` must not tear down ``run()`` — surface it
                # as a sys chip and park so the human can ``orch_send`` to
                # retry. Construction-time errors (bad model name, etc.) are
                # still caught by ``run()``'s outer ``except Exception``.
                try:
                    state.output = await model.generate(
                        input=state.messages, tools=tools, config=gen_config
                    )
                except Exception as exc:
                    logger.exception("orchestrator turn %d failed", turn)
                    orch.kernel.notify(
                        f"[orchestrator error at turn {turn}: {exc}]"
                    )
                    orch.pause()
                    continue
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


# -- seeded namespace (M1-NOTEBOOK.md §Environment) --------------------------


def _seed_analysis_ns() -> dict[str, Any]:
    """Analysis names the prompt promises are pre-bound in ``user_ns``.

    Imported lazily and guarded so a missing optional (e.g. ``inspect_scout``
    on a lean install) degrades to "not seeded" rather than blocking
    orchestrator construction.
    """
    import json

    ns: dict[str, Any] = {"json": json, "get_model": get_model}
    try:
        import numpy as np
        import pandas as pd
        import plotly.express as px
        import plotly.graph_objects as go

        # Bound the model-facing text/plain (a 50-col DataFrame's default
        # repr is multi-KB) and the frontend's ``text/html`` width. The
        # ``show_dimensions`` footer tells the auditor the true shape when
        # elided; ``max_colwidth`` matches M1's per-row seed truncation.
        pd.set_option("display.max_columns", 12)
        pd.set_option("display.max_rows", 20)
        pd.set_option("display.max_colwidth", 80)
        pd.set_option("display.show_dimensions", True)
        ns.update(pd=pd, np=np, px=px, go=go)
    except ImportError:
        pass
    try:
        from inspect_petri import audit_scanner

        ns["audit_scanner"] = audit_scanner
    except ImportError:
        pass
    try:
        from inspect_scout import llm_scanner, scanner

        ns.update(llm_scanner=llm_scanner, scanner=scanner)
    except ImportError:
        pass
    return ns


# -- pre-warm (call once at kernel init) --------------------------------------


def _prewarm() -> None:
    """Suppress inspect's progress display and pay the hooks-banner once.

    ``init_display_type("none")`` stops any in-process inspect call writing
    progress to stdout (which ``_CellStream`` would capture as stream events).
    ``platform_init()`` prints the aisitools hooks banner idempotently — do
    it here so the first in-cell ``wb.scan`` doesn't leak it as stream lines.
    """
    init_display_type("none")
    from inspect_ai._util.platform import platform_init

    platform_init()
    # scout has its own display registry (SCOUT_DISPLAY); silence it too so
    # ``wb.scan`` doesn't leak a rich progress bar into ``_CellStream``.
    with suppress(ImportError):
        from inspect_scout._display._display import (
            init_display_type as scout_init_display,
        )

        scout_init_display("none")
    # plotly's default IPython renderer inlines the full ``plotly.min.js``
    # (~4.8 MB, twice) into ``text/html`` on every figure. The workbench
    # frontend loads ``plotly.js-basic-dist-min`` once (M1-PLOTTING.md), so
    # figures should emit only the ~9 KB div + data. ``_wire_bundle``'s
    # 128 KB cap is the safety net; this is the real fix.
    with suppress(ImportError):
        import plotly.io as pio

        r = pio.renderers["notebook_connected"]
        r.connected = True  # CDN <script src>, not inline bundle
        pio.renderers.default = "notebook_connected"
