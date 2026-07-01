"""M1 orchestrator kernel spike (M1-NOTEBOOK.md v4).

An in-process ``InteractiveShell`` whose one output hook — ``display_pub`` —
emits ``DisplayEvent`` s. Every orchestrator turn is a notebook cell run as
its own ``asyncio.Task``; foreground is just ``run_turn`` awaiting that task,
backgrounding is stopping the await. Gated helpers are ``display(proposal)``
+ ``await Future`` (resolved by the WS handler) + ``dh.update(resolved)`` on
IPython's native ``display_id`` machinery.

This module is self-contained: it does **not** yet register ``DisplayEvent``
in inspect's ``Event`` union or call ``transcript()._event()``. Instead the
kernel appends events to ``outputs[turn_id]`` and forwards each one through
an ``on_display`` callback — the ``Session`` can hook that to ``_enqueue`` a
wire message. Upstreaming ``DisplayEvent`` into inspect is a follow-up.

What we don't get from ``InteractiveShell`` and have to supply:

- **Last-expr → display_pub.** IPython routes the last expression through
  ``sys.displayhook``, not ``display_pub``. ``WorkbenchDisplayHook`` publishes
  the formatted bundle through ``display_pub`` (same trick ``ipykernel``'s
  ``ZMQShellDisplayHook`` uses) so *every* output — explicit ``display()``,
  last-expr, and stdout — arrives as a ``DisplayEvent`` in emission order.
- **stdout/stderr capture.** ``_CellStream`` tees writes to a stream
  ``DisplayEvent`` when the write happens inside a cell task (tracked via a
  ``ContextVar`` so concurrent cells attribute their prints correctly), and
  passes through to the real stream otherwise.

Known concurrent-cell caveat (M1-NOTEBOOK.md §Background execution accepts
races): ``displayhook.exec_result`` is a single slot that ``run_cell_async``
overwrites per call, so ``ExecutionResult.result`` is unreliable when cells
overlap. We therefore never read ``r.result`` — the last-expr value reaches
the model via its ``DisplayEvent.bundle["text/plain"]`` instead, emitted
synchronously under the correct turn's contextvar.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import functools
import io
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher
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

    def wire(self) -> dict[str, Any]:
        return {
            "t": "display",
            "id": self.id,
            "turn": self.turn_id,
            "bundle": self.bundle,
            "meta": self.meta,
            "update": self.update,
        }


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


# -- IPython hooks ------------------------------------------------------------


class WorkbenchDisplayPublisher(DisplayPublisher):
    """Route every ``display()`` / ``dh.update()`` to ``kernel._emit``."""

    kernel: "OrchestratorKernel"

    def clear_output(self, wait: bool = False) -> None:  # noqa: FBT001, FBT002
        # Base writes ``\033[2K\r`` to stdout. Emit a marker instead so
        # ``<Output>`` can drop prior events for this turn; the model-facing
        # render honours it by truncating.
        self.kernel._emit(
            DisplayEvent(id=uuid4().hex, bundle={}, meta={"clear_output": True})
        )

    def publish(  # type: ignore[override]
        self,
        data: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        source: Any = None,
        *,
        transient: dict[str, Any] | None = None,
        update: bool = False,
        **_: Any,
    ) -> None:
        did = (transient or {}).get("display_id")
        self.kernel._emit(
            DisplayEvent(
                id=str(did or uuid4().hex),
                bundle=data,
                meta=metadata or {},
                update=update,
                stable=did is not None,
            )
        )


class WorkbenchDisplayHook(DisplayHook):
    """Route the last-expression bundle through ``display_pub``.

    ``DisplayHook.__call__`` computes ``format_dict`` via the shell's
    ``display_formatter`` and then calls the ``write_*`` hooks below, which
    by default print ``Out[N]: repr`` to stdout. We suppress the print and
    publish the bundle instead — so last-expr and explicit ``display()``
    take the same path and land in ``outputs[turn]`` in the right order.
    ``fill_exec_result`` still runs, so ``ExecutionResult.result`` is
    populated for a *foreground* cell (but see the module docstring for
    the concurrent-cell race).
    """

    def quiet(self) -> bool:
        # Base reads ``history_manager.input_hist_parsed[-1]`` — the most
        # recently *submitted* cell's source, not the currently-executing
        # one — so under concurrent cells a later cell ending in ``;`` would
        # silently suppress an earlier cell's last-expr. The agent doesn't
        # need ``;``-suppression; disable it.
        return False

    def write_output_prompt(self) -> None:  # ``Out[N]: `` → nothing
        pass

    def write_format_data(  # type: ignore[override]
        self, format_dict: dict[str, Any], md_dict: dict[str, Any] | None = None
    ) -> None:
        assert (
            self.shell is not None
        )  # set at construction; traitlets types it Optional
        self.shell.display_pub.publish(
            format_dict, md_dict, transient={"execute_result": True}
        )

    def log_output(self, *_: Any) -> None:
        pass

    def update_user_ns(self, result: Any) -> None:
        # Base writes ``_`` / ``_N`` / ``_oh[N]`` keyed on the shared
        # ``execution_count`` — collides under concurrent cells. The agent
        # is told not to rely on ``_`` / ``Out[]``; drop the write.
        pass

    def finish_displayhook(self) -> None:
        # Skip the base's ``sys.stdout.write("\n")``; keep the
        # ``_is_active`` reset so the flag doesn't stick ``True`` forever.
        self._is_active = False


class _CellStream(io.TextIOBase):
    """A line-buffered stdout/stderr tee that emits stream ``DisplayEvent`` s.

    Attribution uses ``kernel._current_turn`` (a ``ContextVar``): each cell
    task sets it before awaiting ``run_cell_async``, so a ``print()`` from
    concurrent cell A resolves to A's turn even while cell B is also live.
    Buffering is per-turn so interleaved partial writes from concurrent
    cells don't concatenate. Writes from outside any cell (server logs,
    etc.) fall through to the real stream unchanged.
    """

    encoding = "utf-8"

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        # Subprocess/C-level output writes to the real fd and bypasses
        # capture (M1-KERNEL-NOTES.md); at least don't break callers that
        # probe ``fileno()``.
        return self._real.fileno()

    def __init__(
        self, kernel: "OrchestratorKernel", name: str, real: io.TextIOBase
    ) -> None:
        self._kernel = kernel
        self._name = name
        self._real = real
        self._buf: dict[int, str] = {}  # turn_id → partial line

    def write(self, s: str) -> int:
        turn = self._kernel._current_turn.get()
        if turn is None:
            self._real.write(s)
            return len(s)
        buf = self._buf.get(turn, "") + s
        *lines, rest = buf.split("\n")
        for line in lines:
            self._emit(line + "\n")
        self._buf[turn] = rest
        return len(s)

    def flush(self) -> None:
        turn = self._kernel._current_turn.get()
        if turn is not None and (rest := self._buf.pop(turn, "")):
            self._emit(rest)
        self._real.flush()

    def _emit(self, text: str) -> None:
        self._kernel._emit(
            DisplayEvent(
                id=uuid4().hex,
                bundle={STREAM_MIME: {"name": self._name, "text": text}},
            )
        )


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


# -- kernel -------------------------------------------------------------------


class OrchestratorKernel:
    """One in-process IPython shell driving the M1 orchestrator turn loop.

    Owns the ``InteractiveShell``, the per-turn output lists, the pending-
    gate futures, and the background-cell task registry. The eventual
    ``Session`` integration is a single ``on_display`` callback: hook it to
    ``session._enqueue(ev.wire())`` and every kernel output lands on the
    wire in emission order alongside the M0 event stream.
    """

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
        self._install_hooks()

        self.on_display = on_display
        self.outputs: dict[int, list[DisplayEvent]] = {}
        self.pending: dict[str, asyncio.Future[Any]] = {}
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

        # stdout/stderr tee — installed once, contextvar-gated per write.
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr
        sys.stdout = _CellStream(self, "stdout", self._real_stdout)  # type: ignore[assignment,arg-type]
        sys.stderr = _CellStream(self, "stderr", self._real_stderr)  # type: ignore[assignment,arg-type]

        seeded: dict[str, Any] = dict(
            wb=_WbNamespace(self),
            KERNEL=self,
            asyncio=asyncio,
            display=display,
            Markdown=Markdown,
            HTML=HTML,
            **(extra_ns or {}),
        )
        self.shell.user_ns.update(seeded)
        #: names present before any turn — never reported in ``new_names``.
        self._ns_baseline: frozenset[str] = frozenset(self.shell.user_ns)

    def _install_hooks(self) -> None:
        """Repoint the shell's output hooks at our publisher.

        Done post-hoc rather than via ``config=`` because ``.instance()`` may
        already exist (e.g. inspect's notebook util imported first) and a
        second ``instance(config=…)`` call is a no-op. Three places cache the
        displayhook at init: ``shell.displayhook``, ``shell.display_trap.hook``
        (entered by ``run_cell_async``), and ``sys.displayhook`` — all must
        point at the same ``WorkbenchDisplayHook`` instance.
        """
        self.shell.display_pub = WorkbenchDisplayPublisher()
        self.shell.display_pub.kernel = self
        # ``cache_size=0`` because ``_``/``_oh[N]`` are keyed on the shared
        # ``execution_count`` and collide under concurrent cells anyway; we
        # override ``update_user_ns`` to a no-op regardless.
        self.shell.displayhook = WorkbenchDisplayHook(shell=self.shell, cache_size=0)
        self.shell.display_trap.hook = self.shell.displayhook
        # Do NOT also assign ``sys.displayhook`` here — ``display_trap`` is
        # entered on every cell and installs the hook; if it's *already*
        # installed, ``DisplayTrap.set()`` skips saving ``old_hook`` and
        # ``unset()`` then restores ``sys.displayhook = None``.
        #
        # Tracebacks: in-cell, suppress IPython's own print — ``_settle``
        # formats ``r.error`` once for the model, ``<Traceback>`` renders it
        # for the human. Out-of-cell (``run_code`` can leak
        # ``shell.excepthook`` as ``sys.excepthook`` when concurrent cells
        # finish out-of-order — unrefcounted swap), write plain text to the
        # real stderr so uncaught server-side exceptions still surface.
        self.shell._showtraceback = self._showtraceback  # type: ignore[method-assign]
        # Compact ``text/plain`` for figure types whose default repr is the
        # full data dict (multi-KB straight into the tool result). Registered
        # by name so plotly/mpl needn't be importable here.
        assert self.shell.display_formatter is not None
        plain = self.shell.display_formatter.formatters["text/plain"]
        plain.for_type_by_name(
            "plotly.graph_objs._figure",
            "Figure",
            lambda fig, p, cyc: p.text(
                f"<plotly.Figure · {len(fig.data)} trace(s) · rendered interactive>"
            ),
        )
        plain.for_type_by_name(
            "matplotlib.figure",
            "Figure",
            lambda fig, p, cyc: p.text(f"<matplotlib.Figure · {len(fig.axes)} axes>"),
        )

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
        if self.on_display is not None:
            self.on_display(ev)

    def notify(self, msg: str) -> None:
        """Queue a sys-chip line to be drained into the agent's next input."""
        self.notifications.append(msg)
        if self.on_display is not None:
            self.on_display(
                DisplayEvent(
                    id=uuid4().hex, bundle={"text/plain": msg}, meta={"sys": True}
                )
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
        ns0 = set(self.shell.user_ns)
        detach = asyncio.Event()
        self._current_detach = detach

        cell_task: asyncio.Task[ExecutionResult] = asyncio.create_task(
            self._run_cell(turn_id, code)
        )
        self.bg[turn_id] = cell_task
        self._bg_code[turn_id] = code

        cell_task.add_done_callback(functools.partial(self._on_cell_done, turn_id, ns0))

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
            return self._settle(turn_id, ns0, r)

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
        r = await self.shell.run_cell_async(
            code,
            transformed_cell=transformed,
            preprocessing_exc_tuple=exc_tuple,
            store_history=False,
        )
        # ``run_cell_async`` fires ``pre_execute``/``pre_run_cell`` but not
        # the post-events (only the sync ``run_cell`` wrapper does). Without
        # this ``matplotlib_inline``'s ``flush_figures`` never runs.
        self.shell.events.trigger("post_execute")
        self.shell.events.trigger("post_run_cell", r)
        return r

    def _on_cell_done(
        self, turn_id: int, ns0: set[str], task: asyncio.Task[ExecutionResult]
    ) -> None:
        self.bg.pop(turn_id, None)
        self._bg_code.pop(turn_id, None)
        if turn_id not in self._detached:
            return  # fg cell — the agent already has the settled TurnResult
        self._detached.discard(turn_id)
        if task.cancelled():
            self.notify(f"[cell-{turn_id} cancelled]")
            return
        r = task.result()
        err = r.error_before_exec or r.error_in_exec
        bound = _new_names(ns0, self.shell.user_ns, self._ns_baseline)
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

    def _settle(self, turn_id: int, ns0: set[str], r: ExecutionResult) -> TurnResult:
        err = r.error_before_exec or r.error_in_exec
        new = _new_names(ns0, self.shell.user_ns, self._ns_baseline)
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

    def resolve(self, display_id: str, verdict: Any) -> bool:
        """Resolve a pending gate. Returns ``False`` if ``display_id`` unknown."""
        fut = self.pending.get(display_id)
        if fut is None or fut.done():
            return False
        fut.set_result(verdict)
        return True

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

    # -- gating (M1-NOTEBOOK.md §Gating) --------------------------------------

    async def _gate(self, proposal: Proposal) -> Any:
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

    # -- teardown -------------------------------------------------------------

    def restore_streams(self) -> None:
        sys.stdout = self._real_stdout
        sys.stderr = self._real_stderr


# -- wb namespace stub --------------------------------------------------------


class _WbNamespace:
    """The spike's ``wb.*`` — just enough to exercise gating from user code."""

    def __init__(self, kernel: OrchestratorKernel) -> None:
        self._k = kernel

    async def ask_human(self, question: str, options: list[str] | None = None) -> str:
        p = Prompt(question, options)
        return str(await self._k._gate(p))


# -- helpers ------------------------------------------------------------------


def _new_names(
    ns0: set[str], ns1: dict[str, Any], baseline: Iterable[str]
) -> list[str]:
    added = set(ns1) - ns0 - set(baseline)
    return sorted(n for n in added if not n.startswith("_"))


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
