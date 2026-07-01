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
    turn_id: int = -1

    @property
    def text(self) -> str:
        """The model-facing rendering of this output."""
        if (s := self.bundle.get(STREAM_MIME)) is not None:
            return s["text"]
        return self.bundle.get("text/plain", "")

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
        transient = transient or {}
        self.kernel._emit(
            DisplayEvent(
                id=str(transient.get("display_id") or uuid4().hex),
                bundle=data,
                meta=metadata or {},
                update=update,
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

    def write_output_prompt(self) -> None:  # ``Out[N]: `` → nothing
        pass

    def write_format_data(  # type: ignore[override]
        self, format_dict: dict[str, Any], md_dict: dict[str, Any] | None = None
    ) -> None:
        assert self.shell is not None  # set at construction; traitlets types it Optional
        self.shell.display_pub.publish(
            format_dict, md_dict, transient={"execute_result": True}
        )

    def log_output(self, *_: Any) -> None:
        pass

    def finish_displayhook(self) -> None:
        # Skip the base's ``sys.stdout.write("\n")`` / ``flush()``; keep the
        # ``_`` / ``__`` / ``___`` history update (``update_user_ns`` already
        # ran by the time we're called).
        pass


class _CellStream(io.TextIOBase):
    """A stdout/stderr tee that emits stream ``DisplayEvent`` s in-cell.

    Attribution uses ``kernel._current_turn`` (a ``ContextVar``): each cell
    task sets it before awaiting ``run_cell_async``, so a ``print()`` from
    concurrent cell A resolves to A's turn even while cell B is also live.
    Writes from outside any cell (server logs, etc.) fall through to the
    real stream unchanged.
    """

    def __init__(
        self, kernel: "OrchestratorKernel", name: str, real: io.TextIOBase
    ) -> None:
        self._kernel = kernel
        self._name = name
        self._real = real

    def write(self, s: str) -> int:
        if s and self._kernel._current_turn.get() is not None:
            self._kernel._emit(
                DisplayEvent(
                    id=uuid4().hex,
                    bundle={STREAM_MIME: {"name": self._name, "text": s}},
                )
            )
        else:
            self._real.write(s)
        return len(s)

    def flush(self) -> None:
        self._real.flush()


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

    #: names present in a fresh ``user_ns`` that we don't report as "bound"
    _NS_BASELINE: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        extra_ns: dict[str, Any] | None = None,
        on_display: Callable[[DisplayEvent], None] | None = None,
    ) -> None:
        # Singleton shell — ``IPython.display.display`` resolves the
        # publisher via ``InteractiveShell.instance().display_pub``, so a
        # non-singleton shell makes ``display()`` inside user code fall back
        # to ``print(repr(...))``. M1 has one orchestrator per process
        # (M1-NOTEBOOK.md §Risks), so the singleton is fine.
        self.shell = InteractiveShell.instance()
        self.shell.display_pub = WorkbenchDisplayPublisher()
        self.shell.display_pub.kernel = self
        # Last-expr routing: ``run_cell_async`` enters ``self.display_trap``
        # (a ``DisplayTrap`` context manager built at shell init with a
        # reference to the *original* ``displayhook``), which installs that
        # hook as ``sys.displayhook`` for the duration of the cell. So
        # replacing ``shell.displayhook`` alone is not enough — repoint the
        # trap's cached ``.hook`` too.
        self.shell.displayhook = WorkbenchDisplayHook(
            shell=self.shell, cache_size=self.shell.cache_size
        )
        self.shell.display_trap.hook = self.shell.displayhook
        sys.displayhook = self.shell.displayhook
        # Route tracebacks through the same stream capture instead of
        # IPython's coloured writer (the model wants plain text; the
        # frontend renders ``r.error`` via ``<Traceback>`` separately).
        self.shell.InteractiveTB.set_mode("Plain")

        self.on_display = on_display
        self.outputs: dict[int, list[DisplayEvent]] = {}
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.notifications: list[str] = []
        self.bg: dict[int, asyncio.Task[ExecutionResult]] = {}

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

        if not OrchestratorKernel._NS_BASELINE:
            OrchestratorKernel._NS_BASELINE = frozenset(self.shell.user_ns)
        self._seed_ns(extra_ns or {})

    # -- namespace ------------------------------------------------------------

    def _seed_ns(self, extra: dict[str, Any]) -> None:
        wb = _WbNamespace(self)
        ns: dict[str, Any] = dict(
            wb=wb,
            KERNEL=self,
            asyncio=asyncio,
            display=display,
            Markdown=Markdown,
            HTML=HTML,
        )
        ns.update(extra)
        self.shell.user_ns.update(ns)
        # Don't report the seeded names in ``new_names``.
        OrchestratorKernel._NS_BASELINE |= frozenset(ns)

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

        cell_task.add_done_callback(
            functools.partial(self._on_cell_done, turn_id, ns0)
        )

        if background:
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
        # ``transformed_cell=code`` — we don't use line/cell magics that
        # need input transformation, and passing it silences the
        # ``should_run_async`` deprecation path inside ``run_cell_async``.
        return await self.shell.run_cell_async(
            code, transformed_cell=code, store_history=True
        )

    def _on_cell_done(
        self, turn_id: int, ns0: set[str], task: asyncio.Task[ExecutionResult]
    ) -> None:
        self.bg.pop(turn_id, None)
        self._bg_code.pop(turn_id, None)
        if task.cancelled():
            self.notify(f"[cell-{turn_id} cancelled]")
            return
        r = task.result()
        bound = _new_names(ns0, self.shell.user_ns, self._NS_BASELINE)
        self.notify(
            f"[cell-{turn_id} done · bound: {', '.join(bound) or '—'} · "
            f"result: {_short_repr(r)}]"
        )

    def _settle(self, turn_id: int, ns0: set[str], r: ExecutionResult) -> TurnResult:
        err = r.error_before_exec or r.error_in_exec
        text = self._render_outputs(turn_id)
        if err is not None:
            # ``showtraceback`` already wrote the traceback to (captured)
            # stderr, so it's in ``outputs`` — but not always cleanly. Append
            # a compact one for the model regardless.
            text += "\n" + "".join(
                traceback.format_exception(type(err), err, err.__traceback__)
            )
        return TurnResult(
            turn_id=turn_id,
            text=text or "<no output>",
            outputs=self.outputs[turn_id],
            detached=False,
            success=err is None,
            error=err,
            new_names=_new_names(ns0, self.shell.user_ns, self._NS_BASELINE),
        )

    def _render_outputs(self, turn_id: int) -> str:
        parts: list[str] = []
        for ev in self.outputs.get(turn_id, []):
            if ev.update:
                # Updates replace earlier text in-place for the model too:
                # drop the prior line for this id and append the new one.
                parts = [p for p in parts if not p.startswith(f"[{ev.id}] ")]
            t = ev.text
            if not t:
                continue
            # Tag lines with the display id only when the id is stable
            # (``display_id=`` was passed) so update-replacement can match;
            # anonymous outputs render bare.
            parts.append(f"[{ev.id}] {t}" if _is_stable(ev) else t)
        return "\n".join(parts)

    # -- controls (WS handler / composer call these) --------------------------

    def detach(self, turn_id: int | None = None) -> None:
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

    def notify(self, msg: str) -> None:
        self._k.notify(msg)


# -- helpers ------------------------------------------------------------------


def _new_names(
    ns0: set[str], ns1: dict[str, Any], baseline: Iterable[str]
) -> list[str]:
    added = set(ns1) - ns0 - set(baseline)
    return sorted(n for n in added if not n.startswith("_"))


def _short_repr(r: ExecutionResult) -> str:
    if r.error_before_exec or r.error_in_exec:
        e = r.error_before_exec or r.error_in_exec
        return f"{type(e).__name__}: {e}"
    if r.result is None:
        return "None"
    s = repr(r.result)
    return s if len(s) <= 60 else s[:57] + "…"


def _is_stable(ev: DisplayEvent) -> bool:
    # ``display(x, display_id=…)`` yields a caller-chosen id (a Prompt/Run
    # id, ≤32 hex or arbitrary string); anonymous ``display(x)`` / stream
    # writes get a fresh ``uuid4().hex``. Only the former needs the ``[id]``
    # prefix for update-replacement in the model-facing text.
    return len(ev.id) != 32 or ev.update or WB_MIME in ev.bundle


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
