"""M1 orchestrator kernel spike (M1-NOTEBOOK.md v4).

An in-process ``InteractiveShell`` whose one output hook — ``display_pub`` —
emits ``DisplayEvent`` s. Every orchestrator turn is a notebook cell run as
its own ``asyncio.Task``; foreground is just ``run_turn`` awaiting that task,
backgrounding is stopping the await. Gated helpers are ``display(proposal)``
+ ``await Future`` (resolved by the WS handler) + ``dh.update(resolved)`` on
IPython's native ``display_id`` machinery.

The kernel is transport-agnostic: it appends events to ``outputs[turn_id]``
and forwards each through an ``on_display`` callback. ``Orchestrator``
hooks that to ``session.transcript._event(InfoEvent(...))`` so kernel
outputs ride the M0 event pipe (reconnect/persistence for free) — see
``m1/orchestrator.py``.

The IPython-side plumbing (``display_pub`` / ``displayhook`` / stdout tee)
lives in ``m1/hooks.py``; ``__enter__`` installs it and swaps the process
streams, ``__exit__`` restores them.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import functools
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, Self
from uuid import uuid4

from IPython.core.interactiveshell import ExecutionResult, InteractiveShell
from IPython.display import HTML, Markdown, display

WB_MIME = "application/vnd.workbench.v1+json"
STREAM_MIME = "application/vnd.jupyter.stream+json"

#: MIME preference for the model-facing rendering. ``text/markdown`` first so
#: ``display(Markdown(f"…"))`` (the ``wb.report`` replacement) shows the
#: computed prose, not ``<IPython.core.display.Markdown object>``.
_MODEL_MIME_PREF = ("text/markdown", "text/latex", "text/plain")
_MODEL_TEXT_CAP = 4000


# -- events -------------------------------------------------------------------


@dataclass(slots=True)
class DisplayEvent:
    """One kernel output, in emission order.

    ``bundle`` is the IPython MIME dict (``text/plain`` is what the model
    reads; ``application/vnd.workbench.v1+json`` is what ``<Output>``
    renders). ``update=True`` means "patch the earlier event with this
    ``id``" — the proposal→live and progress-tick cases.
    """

    id: str
    bundle: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    update: bool = False
    #: caller passed ``display_id=`` — later ``update=True`` events with the
    #: same id replace this one in the model-facing render.
    stable: bool = False
    turn_id: int = -1

    @property
    def text(self) -> str:
        """The model-facing rendering of this output.

        Picks the first available of ``text/markdown`` → ``text/latex`` →
        ``text/plain`` and caps length — the safety net for rich objects
        (e.g. a ``go.Figure`` whose ``text/plain`` we forgot to register a
        compact formatter for) so one plot can't blow the tool result.
        """
        if (s := self.bundle.get(STREAM_MIME)) is not None:
            return str(s["text"])
        for mime in _MODEL_MIME_PREF:
            if t := self.bundle.get(mime):
                return _truncate(str(t), _MODEL_TEXT_CAP)
        return ""


@dataclass(slots=True)
class TurnResult:
    """What ``run_turn`` hands back to the orchestrator loop."""

    turn_id: int
    text: str  #: model-facing tool-result string
    outputs: list[DisplayEvent]
    detached: bool = False
    success: bool = True
    error: BaseException | None = None
    #: names newly bound in ``user_ns`` by this cell (for the ``[done]`` chip)
    new_names: list[str] = field(default_factory=list)


# -- gating -------------------------------------------------------------------


class Proposal(Protocol):
    """A gated card: renders pending, awaits a verdict, then renders resolved."""

    id: str

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]: ...

    def resolve(self, verdict: Any) -> None: ...


@dataclass
class Prompt:
    """The minimal working gated helper for the spike (``wb.ask_human``)."""

    question: str
    options: list[str] | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    answer: str | None = None

    def resolve(self, verdict: Any) -> None:
        self.answer = str(verdict)

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        pending = self.answer is None
        return {
            "text/plain": (
                f"<Prompt {self.id[:6]} · {self.question!r} · pending>"
                if pending
                else f"<Prompt {self.id[:6]} → {self.answer!r}>"
            ),
            WB_MIME: {
                "kind": "prompt",
                "id": self.id,
                "question": self.question,
                "options": self.options,
                "answer": self.answer,
                "pending": pending,
            },
        }


class Gate:
    """``display(proposal)`` → ``await Future`` → ``dh.update(resolved)``.

    The WS handler resolves via ``.resolve()``. Extracted from the kernel so
    ``Workbench`` can hold the gate directly instead of reaching through
    ``kernel``.
    """

    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[Any]] = {}

    async def __call__(self, proposal: Proposal) -> Any:
        dh = display(proposal, display_id=proposal.id)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[proposal.id] = fut
        try:
            verdict = await fut
        finally:
            self.pending.pop(proposal.id, None)
        proposal.resolve(verdict)
        dh.update(proposal)
        return verdict

    def resolve(self, display_id: str, verdict: Any) -> bool:
        """Resolve a pending gate. Returns ``False`` if ``display_id`` unknown."""
        fut = self.pending.get(display_id)
        if fut is None or fut.done():
            return False
        fut.set_result(verdict)
        return True


# -- kernel -------------------------------------------------------------------


class OrchestratorKernel:
    """One in-process IPython shell driving the M1 orchestrator turn loop.

    Owns the ``InteractiveShell``, the per-turn output lists, the ``Gate``,
    and the background-cell task registry. The ``Session`` integration is a
    single ``on_display`` callback: ``Orchestrator`` hooks it to
    ``Orchestrator._on_display → session.emit(InfoEvent)`` so every kernel
    output lands on the wire in emission order alongside the M0 event stream.

    Construction is side-effect-free; the process-global hooks (IPython
    ``display_pub``/``displayhook``, ``sys.stdout``/``stderr``) are
    installed on ``__enter__`` and restored on ``__exit__``. The
    ``InteractiveShell`` singleton is a hard *process* boundary, so at most
    one kernel may be entered at a time (guarded by ``_instance``).
    """

    _instance: ClassVar["OrchestratorKernel | None"] = None

    def __init__(
        self,
        *,
        extra_ns: dict[str, Any] | None = None,
        on_display: Callable[[DisplayEvent], None] | None = None,
    ) -> None:
        # Singleton shell — ``IPython.display.display`` resolves the
        # publisher via ``InteractiveShell.instance().display_pub``, so a
        # non-singleton shell makes ``display()`` inside user code fall back
        # to ``print(repr(...))``. That makes the singleton a hard *process*
        # boundary (not per-Session as M1-NOTEBOOK.md v4 originally framed
        # it); multi-session M1 means subprocess-per-orchestrator.
        self.shell = InteractiveShell.instance()

        self.on_display = on_display
        self.outputs: dict[int, list[DisplayEvent]] = {}
        self.notifications: list[str] = []
        self.bg: dict[int, asyncio.Task[ExecutionResult]] = {}
        #: turns whose ``[done]`` chip should be enqueued (backgrounded or
        #: detached mid-run — not fg cells the agent already saw settle).
        self._detached: set[int] = set()

        self._turn_counter = 0
        self._current_turn: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            "wb_current_turn", default=None
        )
        self._current_detach: asyncio.Event | None = None
        #: source of each still-running cell, for ``shadow_warning``.
        self._bg_code: dict[int, str] = {}

        self.gate = Gate()
        # Compat shims so existing callers (``server.py``, ``wb.py``,
        # ``Session.view``) keep working while the ``Gate`` is threaded
        # through — ``kernel.gate(prop)`` / ``kernel.resolve(id, v)`` /
        # ``kernel.pending`` all forward.
        self.resolve = self.gate.resolve

        # Refs only — no swap. ``__enter__`` re-captures and swaps; keeping
        # a valid ``_real_stderr`` here means ``_forward``/``_showtraceback``
        # are safe on a never-entered kernel (e.g. a resumed orchestrator
        # that never runs a cell before ``close()``).
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr

        seeded: dict[str, Any] = dict(
            KERNEL=self,
            asyncio=asyncio,
            display=display,
            Markdown=Markdown,
            HTML=HTML,
            **(extra_ns or {}),
        )
        self.shell.user_ns.update(seeded)

    @property
    def pending(self) -> dict[str, asyncio.Future[Any]]:
        return self.gate.pending

    # -- context manager ------------------------------------------------------

    def __enter__(self) -> Self:
        if OrchestratorKernel._instance is not None:
            raise RuntimeError(
                "an OrchestratorKernel is already active in this process "
                "(InteractiveShell is a singleton)"
            )
        OrchestratorKernel._instance = self
        # Local import: ``hooks`` imports ``DisplayEvent``/``STREAM_MIME``
        # from this module, so a top-level import would be circular.
        from workbench.m1.hooks import _CellStream, install_workbench_hooks  # noqa: PLC0415

        install_workbench_hooks(self.shell, self._emit)
        # Tracebacks: in-cell, suppress IPython's own print — ``_settle``
        # formats ``r.error`` once for the model, ``<Traceback>`` renders it
        # for the human. Out-of-cell (``run_code`` can leak
        # ``shell.excepthook`` as ``sys.excepthook`` when concurrent cells
        # finish out-of-order — unrefcounted swap), write plain text to the
        # real stderr so uncaught server-side exceptions still surface.
        self.shell._showtraceback = self._showtraceback  # type: ignore[method-assign]

        # stdout/stderr tee — contextvar-gated per write.
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr
        sys.stdout = _CellStream(self._emit, self._current_turn, "stdout", self._real_stdout)  # type: ignore[assignment,arg-type]
        sys.stderr = _CellStream(self._emit, self._current_turn, "stderr", self._real_stderr)  # type: ignore[assignment,arg-type]
        return self

    def __exit__(self, *exc: object) -> None:
        # No-op if this kernel was never entered (or already exited) — the
        # compat ``restore_streams()`` alias may be called from
        # ``Orchestrator.run()`` finally on a resumed kernel that never ran.
        if OrchestratorKernel._instance is not self:
            return
        sys.stdout = self._real_stdout
        sys.stderr = self._real_stderr
        OrchestratorKernel._instance = None

    def restore_streams(self) -> None:
        """Compat alias; callers migrate to ``with kernel:``."""
        self.__exit__(None, None, None)

    def _showtraceback(
        self, etype: type, evalue: BaseException, stb: list[str]
    ) -> None:
        if self._current_turn.get() is None:
            print("\n".join(stb), file=self._real_stderr)

    # -- emit -----------------------------------------------------------------

    def _emit(self, ev: DisplayEvent) -> None:
        turn = self._current_turn.get()
        ev.turn_id = turn if turn is not None else -1
        self.outputs.setdefault(ev.turn_id, []).append(ev)
        self._forward(ev)

    def _forward(self, ev: DisplayEvent) -> None:
        # A broken wire callback (e.g. WS enqueue on a closed stream) must
        # not turn every ``print()`` into a cell error — the callback runs
        # inline in ``_CellStream.write`` / ``publish``, so an unguarded
        # exception surfaces as ``error_in_exec``.
        if self.on_display is None:
            return
        try:
            self.on_display(ev)
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=self._real_stderr)

    def notify(self, msg: str) -> None:
        """Queue a sys-chip line to be drained into the agent's next input."""
        self.notifications.append(msg)
        self._forward(
            DisplayEvent(id=uuid4().hex, bundle={"text/plain": msg}, meta={"sys": True})
        )

    def drain_notifications(self) -> list[str]:
        out, self.notifications = self.notifications, []
        return out

    # -- turn execution (M1-NOTEBOOK.md §Background execution) ----------------

    async def run_turn(self, code: str, *, background: bool = False) -> TurnResult:
        """Run one cell as its own task; await it unless backgrounded/detached.

        Returns immediately with ``detached=True`` if ``background`` is set or
        the human calls ``detach()`` mid-await; the cell task keeps running
        and on completion enqueues a ``[cell-N done · bound: … · result: …]``
        notification. Otherwise returns the settled ``TurnResult`` with the
        full model-facing ``text`` (concatenated ``text/plain`` of every
        output, in emission order, then the traceback if any).
        """
        self._turn_counter += 1
        turn_id = self._turn_counter
        self.outputs[turn_id] = []
        detach = asyncio.Event()
        self._current_detach = detach

        cell_task: asyncio.Task[ExecutionResult] = asyncio.create_task(
            self._run_cell(turn_id, code)
        )
        self.bg[turn_id] = cell_task
        self._bg_code[turn_id] = code

        cell_task.add_done_callback(
            functools.partial(self._on_cell_done, turn_id, code)
        )

        if background:
            self._detached.add(turn_id)
            return TurnResult(
                turn_id=turn_id,
                text=f"<cell-{turn_id} backgrounded>",
                outputs=self.outputs[turn_id],
                detached=True,
            )

        detach_task = asyncio.create_task(detach.wait())
        done, _ = await asyncio.wait(
            {cell_task, detach_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if cell_task in done:
            detach_task.cancel()
            r = cell_task.result()
            return self._settle(turn_id, code, r)

        # detached mid-run
        self._detached.add(turn_id)
        return TurnResult(
            turn_id=turn_id,
            text=(
                f"<cell-{turn_id} detached — running; outputs so far:\n"
                f"{self._render_outputs(turn_id)}>"
            ),
            outputs=self.outputs[turn_id],
            detached=True,
        )

    async def _run_cell(self, turn_id: int, code: str) -> ExecutionResult:
        # Set the contextvar *inside* the task so it propagates to every
        # coroutine the cell awaits (each ``asyncio.Task`` copies the current
        # context at creation, so concurrent cells don't clobber each other).
        self._current_turn.set(turn_id)
        # Run input transformation ourselves so ``%time`` / ``obj?`` work
        # (M1-NOTEBOOK.md §Kernel lists them as free wins) while still
        # passing ``transformed_cell=`` to silence the deprecation path.
        exc_tuple: Any = None
        try:
            transformed = self.shell.transform_cell(code)
        except Exception:  # noqa: BLE001 — surfaces as error_before_exec
            transformed, exc_tuple = code, sys.exc_info()
        # ``store_history=False`` — the default ``HistoryManager`` writes to
        # ``~/.ipython/profile_default/history.sqlite``; agent-generated
        # code shouldn't land there. ``quiet()`` (which reads history) is
        # overridden anyway.
        try:
            r = await self.shell.run_cell_async(
                code,
                transformed_cell=transformed,
                preprocessing_exc_tuple=exc_tuple,
                store_history=False,
            )
            # ``run_cell_async`` fires ``pre_execute``/``pre_run_cell`` but
            # not the post-events (only the sync ``run_cell`` wrapper does).
            # Without this ``matplotlib_inline``'s ``flush_figures`` never
            # runs.
            self.shell.events.trigger("post_execute")
            self.shell.events.trigger("post_run_cell", r)
            return r
        finally:
            # Emit any partial line still in ``_CellStream._buf[turn_id]``
            # (``print(..., end="")`` with no later newline) and drop the
            # buffer entry — otherwise the text is lost and the dict grows
            # unboundedly. Runs under this task's contextvar so ``flush()``
            # resolves the right turn.
            sys.stdout.flush()
            sys.stderr.flush()

    def _on_cell_done(
        self, turn_id: int, code: str, task: asyncio.Task[ExecutionResult]
    ) -> None:
        from workbench.m1.hooks import _CellStream  # noqa: PLC0415

        self.bg.pop(turn_id, None)
        self._bg_code.pop(turn_id, None)
        # Belt-and-braces buffer drop for the hard-cancel path where
        # ``_run_cell``'s ``finally`` never ran.
        for s in (sys.stdout, sys.stderr):
            if isinstance(s, _CellStream):
                s._buf.pop(turn_id, None)
        if turn_id not in self._detached:
            return  # fg cell — the agent already has the settled TurnResult
        self._detached.discard(turn_id)
        err: BaseException | None = None
        if not task.cancelled():
            r = task.result()
            err = r.error_before_exec or r.error_in_exec
        # IPython's ``run_code`` catches ``CancelledError`` and returns it
        # as ``error_in_exec``, so ``task.cancelled()`` alone isn't enough.
        if task.cancelled() or isinstance(err, asyncio.CancelledError):
            self.notify(f"[cell-{turn_id} cancelled]")
            return
        bound = _bound_names(code, self.shell.user_ns)
        # ``r.result`` races under concurrent cells (module docstring); read
        # the last displayed value from the event stream instead.
        tail = next(
            (
                _truncate(ev.text)
                for ev in reversed(self.outputs.get(turn_id, []))
                if STREAM_MIME not in ev.bundle
            ),
            "None",
        )
        result = f"{type(err).__name__}: {err}" if err else tail
        self.notify(
            f"[cell-{turn_id} done · bound: {', '.join(bound) or '—'} · "
            f"result: {result}]"
        )

    def _settle(self, turn_id: int, code: str, r: ExecutionResult) -> TurnResult:
        err = r.error_before_exec or r.error_in_exec
        new = _bound_names(code, self.shell.user_ns)
        text = self._render_outputs(turn_id)
        if err is not None:
            text += ("\n" if text else "") + "".join(
                traceback.format_exception(type(err), err, err.__traceback__)
            )
        elif not text:
            text = f"<ok · bound: {', '.join(new)}>" if new else "<no output>"
        return TurnResult(
            turn_id=turn_id,
            text=text,
            outputs=self.outputs[turn_id],
            detached=False,
            success=err is None,
            error=err,
            new_names=new,
        )

    def _render_outputs(self, turn_id: int) -> str:
        """Model-facing text: outputs in emission order, updates collapsed.

        A ``stable`` id's text is whatever its *last* event carried, rendered
        at its *first* event's position — so a ``RunHandle`` that ticks 20×
        appears once, at the point ``run_audits`` was called, showing the
        final counters. Anonymous outputs and stream lines render verbatim.
        """
        events = self.outputs.get(turn_id, [])
        latest = {ev.id: ev.text for ev in events if ev.stable}
        seen: set[str] = set()
        parts: list[str] = []
        for ev in events:
            if ev.meta.get("clear_output"):
                parts.clear()
                seen.clear()
                continue
            if ev.stable:
                if ev.id in seen:
                    continue
                seen.add(ev.id)
                parts.append(latest[ev.id])
            elif t := ev.text:
                parts.append(t.rstrip("\n") if STREAM_MIME in ev.bundle else t)
        return "\n".join(parts)

    # -- controls (WS handler / composer call these) --------------------------

    def detach(self) -> None:
        """Stop awaiting the current foreground cell (it keeps running)."""
        if self._current_detach is not None:
            self._current_detach.set()

    def cancel(self, turn_id: int) -> bool:
        task = self.bg.get(turn_id)
        if task is None:
            return False
        task.cancel()
        return True

    def shadow_warning(self, code: str) -> list[str]:
        """Names ``code`` targets that a still-running cell will also bind.

        The optional AST guardrail from M1-NOTEBOOK.md §Background execution
        — cheap and best-effort: intersects top-level assignment targets of
        ``code`` with those of every cell still in ``self.bg``.
        """
        if not self._bg_code:
            return []
        pending: set[str] = set()
        for src in self._bg_code.values():
            pending |= _assign_targets(src)
        return sorted(_assign_targets(code) & pending)


# -- helpers ------------------------------------------------------------------


def _bound_names(code: str, ns: dict[str, Any]) -> list[str]:
    """Top-level assignment targets in ``code`` that actually landed in ``ns``.

    Replaces the launch-time ``ns0`` snapshot diff, which under concurrent
    cells attributed *every* name bound between launch and settle to the
    settling cell. AST targets are exact for the cell's own top-level
    assigns and immune to concurrent siblings.
    """
    return sorted(n for n in _assign_targets(code) if n in ns)


def _truncate(s: str, n: int = 60) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _assign_targets(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
            elif isinstance(t, ast.Tuple):
                out.update(e.id for e in t.elts if isinstance(e, ast.Name))
    return out
