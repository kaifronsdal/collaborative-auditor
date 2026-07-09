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
import shutil
import signal
from contextlib import suppress
from pathlib import Path
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
from workbench.m1.wire import DisplayEvent, EvalRunPayload
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

#: P2 session fork — synthetic user note appended to a forked orchestrator's
#: ``resume_messages`` in place of ``KERNEL_RESTART_NOTE``. The kernel is fresh
#: (same as a resume), but the parent's ``run_log_dirs`` were absolutized and
#: seeded into ``wb.DEFAULTS["parent_runs"]`` so ``wb.attach()`` on them still
#: resolves; new ``bash("… --log-dir runs/X")`` writes to the fork's own dir.
FORK_NOTE = (
    "[forked from session {parent} at turn {turn} — kernel is fresh; parent "
    "runs re-attached via absolute paths in wb.DEFAULTS['parent_runs']]"
)

#: File suffixes copied from ``parent.session_dir`` into a fork's own
#: ``session_dir`` (top-level only — ``runs/`` is a directory and skipped).
_FORK_COPY_SUFFIXES = frozenset({".json", ".yaml", ".txt", ".py", ".md"})

#: P3 prompt/seed versioning — ``span_id`` → ``file_hashes`` carried from
#: :func:`~workbench.m1.persist.load_orchestrator` to the ``Orchestrator``
#: it seeds. ``session.start_orchestrator`` takes explicit kwargs (and is
#: out of scope here), so the restored dict can't ride in the returned meta;
#: ``__init__`` pops it below by ``span_id`` — same handoff shape as
#: ``queued`` in ``workbench.persist``.
_restored_file_hashes: dict[str, dict[str, str]] = {}

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
        parent_session_dir: Path | None = None,
        parent_session_id: str | None = None,
        fork_at_turn: int | None = None,
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
        #: P3 prompt/seed versioning — ``path`` → ``sha256(content)[:12]``
        #: for every ``write_file`` this orchestrator has done. Snapshotted
        #: onto each ``Finding`` at ``review_finding`` time so a signed claim
        #: names the exact seed/prompt version it was derived from. Restored
        #: from ``orchestrator.eval`` metadata via ``_restored_file_hashes``.
        self.file_hashes: dict[str, str] = _restored_file_hashes.pop(span_id or "", {})
        #: P2 session fork — when this orchestrator was seeded from a
        #: ``fork_seed()``, ``_initial_messages`` swaps ``KERNEL_RESTART_NOTE``
        #: for ``FORK_NOTE`` and ``_seed_user_ns`` exposes the (absolutized)
        #: parent ``run_log_dirs`` as ``wb.DEFAULTS["parent_runs"]``.
        self._parent_session_id = parent_session_id
        self._fork_at_turn = fork_at_turn

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
        # P2 fork: copy top-level artifacts the parent's model wrote (seed
        # lists, notes, helper .py) so the fork's ``read_file``/``bash`` sees
        # them. ``runs/`` is a directory and skipped — the fork attaches to
        # parent runs via absolute path, and writes its own under ``./runs``.
        if parent_session_dir is not None:
            for f in Path(parent_session_dir).iterdir():
                if f.is_file() and f.suffix in _FORK_COPY_SUFFIXES:
                    shutil.copy2(f, self.session_dir / f.name)
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
            file_hashes=self.file_hashes,
        )
        self._seed_user_ns(wb)
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
        #: P0.5 — every live ``bash`` subprocess (fg + bg), keyed by a
        #: 6-hex job id (== the ``bg-XXXXXX`` handle for background procs).
        #: Each entry is ``{proc, cmd, started_at, background}`` so
        #: ``_bg_jobs()`` can render the P2 panel row without re-parsing the
        #: tool result. ``bash_tool`` adds on spawn and pops on
        #: ``proc.wait()``; ``close()`` sends SIGTERM to each live process
        #: group so a server restart doesn't orphan an ``inspect eval``
        #: that's still burning tokens.
        self._bash_procs: dict[str, dict[str, Any]] = {}
        #: The bash-driven ``eval_run`` accumulator — was a
        #: ``make_bash_tool`` closure local; lifted here so ``_bg_jobs()``
        #: can read task/log_dir/finished for the P2 panel. Keyed by
        #: ``eval_id``; ``_fold_eval`` folds each ``{"wb":"eval_*"}`` protocol
        #: line into the snapshot in place.
        self._bash_evals: dict[str, EvalRunPayload] = {}
        #: ``eval_id`` → ``time.time()`` at ``eval_start`` — the panel's
        #: elapsed column (``EvalRunPayload`` carries no wall-clock stamp).
        self._eval_started: dict[str, float] = {}

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

    def _seed_user_ns(self, wb: Workbench) -> None:
        """Populate ``kernel.shell.user_ns`` with ``wb`` + the analysis names.

        Factored out of ``__init__`` so ``restart_kernel`` can rebuild the
        namespace identically after ``user_ns.clear()``. ``wb.DEFAULTS`` and
        the scanner library are re-derived from ``self`` (not preserved from
        the old ``user_ns``) so a settings change picked up mid-session is
        honoured on restart.
        """
        wb.DEFAULTS = dict(self.audit_defaults)
        if self._parent_session_id is not None:
            wb.DEFAULTS["parent_runs"] = list(self.run_log_dirs)
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

    def restart_kernel(self) -> None:
        """P2 — drop ``user_ns``, keep ``state.messages`` (Jupyter "restart kernel").

        Resets the IPython shell (``user_ns`` cleared, ``_prewarm``/template/
        seeding re-run — same as construction) without touching the agent's
        chat history, so the model keeps its plan and prior tool results but
        every Python binding is gone. A ``[kernel restarted …]`` note is
        queued into the next agent input pointing at ``run_log_dirs`` (same
        note the resume path uses) so the model knows to re-``wb.attach``
        rather than reference dead names. ``kernel.outputs`` and the turn
        counter are preserved — restart is about namespace state, not the
        display record.
        """
        self.kernel.shell.reset(new_session=False)
        _prewarm()
        install_template()
        wb = Workbench(
            self.gate,
            session_dir=str(self.session_dir),
            session_id=self.session.session_id or "",
            file_hashes=self.file_hashes,
        )
        self._seed_user_ns(wb)
        # ``_seeded`` is the baseline ``_ns_summary`` filters against; without
        # re-snapshotting, the fresh ``__builtins__``/IPython names ``reset()``
        # installed would leak into the next ``cell_done`` tooltip.
        self.kernel._seeded = set(self.kernel.shell.user_ns)  # noqa: SLF001
        dirs = ", ".join(self.run_log_dirs) or "(none)"
        self.kernel.notify(KERNEL_RESTART_NOTE.format(dirs=dirs))

    def _broadcast_status_soon(self) -> None:
        """``Gate.on_change`` hook — push ``status`` the moment a gate opens/closes.

        Without this the ``"waiting"`` overlay only surfaces on the next
        ``push_full_state``. Called sync from inside ``Gate.__call__`` (event
        loop is running); guarded on ``self.task`` so a gate that fires before
        ``run()`` is spawned doesn't broadcast into a half-built session.
        """
        if self.task is not None:
            asyncio.create_task(self.session.broadcast_status())  # noqa: RUF006

    def dirty(self) -> None:
        """A4-partial (ARCHITECTURE-RACES.md): enqueue a ``{t:"orch"}`` delta.

        Called at each mutation of process-only state the JobsPanel reads
        (``_bash_procs`` add/pop, ``run_log_dirs.append``) so ``bg_jobs``
        refreshes without waiting for the next full ``push_full_state``.
        Replaces the ``fbc8ab6`` ``_broadcast_status_soon()`` calls at those
        sites — ``{t:"status"}`` doesn't carry ``bg_jobs``, so the panel
        never picked up the new proc. Guarded on ``self.task``: before
        ``run()`` is spawned, ``start_orchestrator``'s :meth:`~workbench
        .session.Session.broadcast_orch` ships the initial snapshot instead.
        """
        if self.task is not None:
            self.session.broadcast_orch_dirty(self)

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

    async def close(self) -> None:
        """Reap every tracked ``bash`` subprocess and bg cell task.

        Called from ``Session.close()`` after the ``run()`` task is cancelled
        (so nothing spawns more). Each proc was started with
        ``start_new_session=True`` → its pgid == its pid, so ``killpg``
        reaches the whole subtree (``inspect eval`` → docker sandboxes).

        STRESS-V2 H4: ``kernel.bg`` cell tasks are separate ``asyncio.Task``s
        — cancelling ``run()`` doesn't reach them, and ``kernel.__exit__`` is
        sync (can't await) and runs inside the being-cancelled ``run()``'s
        ``with`` unwind, so it can't own this either. Cancel-then-gather here
        with the same ``move_on_after`` bound as ``rewind()`` (H7): a cell in
        blocking sync code never observes the cancel; don't let it hold up
        session teardown.
        """
        for entry in list(self._bash_procs.values()):
            proc = entry["proc"]
            if proc.returncode is None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGTERM)
        self._bash_procs.clear()
        bg_tasks = list(self.kernel.bg.values())
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            with anyio.move_on_after(1.0):
                await asyncio.gather(*bg_tasks, return_exceptions=True)

    def cancel_bg(self, job_id: str) -> bool:
        """Kill one tracked ``bash`` subprocess (``{t:"cancel_bg"}`` handler).

        Same ``killpg`` mechanics as ``close()``. Returns ``False`` if the id
        is unknown or the process had already exited. The ``_pump`` task's
        ``proc.wait()`` returns naturally, which pops the entry and emits the
        usual ``bg_done`` card — so the model sees ``[bg-{id} done · exit N]``.
        """
        entry = self._bash_procs.get(job_id)
        if entry is None:
            return False
        proc = entry["proc"]
        if proc.returncode is not None:
            return False
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        self.dirty()
        return True

    def _initial_messages(self) -> list[ChatMessage]:
        if self._resume_messages is not None:
            if self._parent_session_id is not None:
                text = FORK_NOTE.format(
                    parent=self._parent_session_id, turn=self._fork_at_turn
                )
            else:
                dirs = ", ".join(self.run_log_dirs) or "(none)"
                text = KERNEL_RESTART_NOTE.format(dirs=dirs)
            return [*self._resume_messages, ChatMessageUser(content=text)]
        return [ChatMessageSystem(content=self.system_prompt)]

    # -- P2 session fork ------------------------------------------------------

    def fork_seed(self, at_turn: int) -> dict[str, Any]:
        """``start_orchestrator`` kwargs to seed a fresh session from turn N.

        ``resume_messages`` is ``messages_for_save()`` (so a mid-tool save's
        synthetic result pairing applies) truncated at ``_turn_msg[at_turn]``
        — same slice as ``rewind(at_turn)``. ``run_log_dirs`` is absolutized
        against *this* ``session_dir`` so the fork's ``wb.attach("runs/r1")``
        (which resolves against the *fork's* dir) can't reach them, but
        ``wb.attach(wb.DEFAULTS["parent_runs"][i])`` can. Fresh ``span_id`` →
        the fork gets its own ``session_dir``; ``parent_session_dir`` is
        passed so ``__init__`` copies top-level artifacts across.
        """
        messages = self.messages_for_save()
        msg_id = self._turn_msg.get(at_turn)
        if msg_id is not None:
            idx = next((i for i, m in enumerate(messages) if m.id == msg_id), None)
            if idx is not None:
                messages = messages[:idx]
        run_log_dirs = [
            d if os.path.isabs(d) else str(self.session_dir / d)
            for d in self.run_log_dirs
        ]
        return {
            "model": self.model_name,
            "system_prompt": self.system_prompt,
            "model_args": dict(self.model_args),
            "generate_config": dict(self.generate_config),
            "audit_defaults": dict(self.audit_defaults),
            "resume_messages": messages,
            "run_log_dirs": run_log_dirs,
            "parent_session_dir": self.session_dir,
            "parent_session_id": self.session.session_id,
            "fork_at_turn": at_turn,
        }

    # -- view (for Session.view()) --------------------------------------------

    #: PRODUCT-GAPS P2 context gauge — inspect's ``get_model()`` doesn't expose
    #: a context-window size, so hardcode a conservative default (Claude-class
    #: models are 200k+). Chars ≠ tokens; the frontend gauge is coarse anyway.
    CONTEXT_LIMIT_CHARS = 200_000

    def view(self) -> dict[str, Any]:
        messages = self.state.messages if self.state is not None else []
        # A4-partial: ``pending_gates`` is transcript-derivable (gate cards'
        # ``bundle[WB_MIME].pending`` flag) — folded on the frontend by
        # ``usePendingGates()`` instead of shipped here. ``generating`` stays
        # on ``{t:"status"}`` (adversarial-review mis-partition #1: the fold
        # would lose the TTFB shimmer). ``notifications`` stays for the
        # connect-time ``push_full_state``; ``broadcast_orch_dirty`` strips it
        # so mid-session ``{t:"orch"}`` doesn't clobber ``{t:"notify"}`` chips.
        return {
            "span_id": self.span_id,
            "status": self.status,
            "model": self.model_name,
            "bg_cells": sorted(self.kernel.bg),
            "bg_jobs": self._bg_jobs(),
            "notifications": list(self.kernel.notifications),
            "context_chars": sum(len(m.text) for m in messages),
            "context_limit": self.CONTEXT_LIMIT_CHARS,
        }

    def _bg_jobs(self) -> list[dict[str, Any]]:
        """P2 bg-job panel — one row per ``bash(background=True)`` + eval run.

        Bash rows come from ``_bash_procs`` (running-only; entries are popped
        on ``proc.wait()``). Eval rows come from the live ``_bash_evals``
        accumulator (task/status/log_dir), falling back to bare
        ``run_log_dirs`` entries for resumed sessions where the accumulator
        didn't survive restart.
        """
        jobs: list[dict[str, Any]] = []
        for job_id, entry in self._bash_procs.items():
            if not entry.get("background"):
                continue
            proc = entry["proc"]
            jobs.append(
                {
                    "kind": "bash",
                    "id": job_id,
                    "cmd_or_task": entry["cmd"],
                    "status": "running" if proc.returncode is None else "done",
                    "pid": proc.pid,
                    "started_at": entry["started_at"],
                }
            )
        seen_dirs: set[str] = set()
        for eid, p in self._bash_evals.items():
            log_dir = p.get("log_dir") or ""
            if log_dir:
                seen_dirs.add(log_dir)
            status = (
                "error"
                if p.get("error")
                else ("done" if p.get("finished") else "running")
            )
            jobs.append(
                {
                    "kind": "eval",
                    "id": eid,
                    "cmd_or_task": p.get("task") or "eval",
                    "status": status,
                    "log_dir": log_dir,
                    "started_at": self._eval_started.get(eid),
                }
            )
        for d in self.run_log_dirs:
            if d in seen_dirs:
                continue
            jobs.append(
                {
                    "kind": "eval",
                    "id": d,
                    "cmd_or_task": d.rstrip("/").rsplit("/", 1)[-1] or d,
                    "status": "attached",
                    "log_dir": d,
                    "started_at": None,
                }
            )
        return jobs

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
        # in-flight tool result. P10 (OVERNIGHT-SWEEP): debounced/off-loop —
        # a full sync ``save()`` here was ~2GB writes / 100 turns on the hot
        # path. No-op when ``store_dir``/``session_id`` unset.
        self.session.schedule_save()

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
        # STRESS-V2 H7: a bg cell in blocking sync code (``time.sleep``,
        # pandas, C-ext) never observes the cancel, so ``gather`` would block
        # indefinitely while ``_dispatch_lock`` is held. The ``move_on_after``
        # lets dispatch return; the cell eventually finishes and its output
        # is dropped by the rewound-uuid filter (``mark_rewound`` below).
        tasks = list(self.kernel.bg.values())
        for tid in list(self.kernel.bg):
            self.kernel.cancel(tid)
        if tasks:
            with anyio.move_on_after(2.0):
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
                    orch.kernel.notify(f"[orchestrator error at turn {turn}: {exc}]")
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
