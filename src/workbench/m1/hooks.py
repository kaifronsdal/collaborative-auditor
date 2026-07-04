"""IPython output-side hooks for the M1 orchestrator kernel.

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

import contextvars
import io
from collections.abc import Callable
from typing import Any, override
from uuid import uuid4

from IPython.core.displayhook import DisplayHook
from IPython.core.displaypub import DisplayPublisher
from IPython.core.interactiveshell import InteractiveShell

from workbench.m1.wire import STREAM_MIME, DisplayEvent


class WorkbenchDisplayPublisher(DisplayPublisher):
    """Route every ``display()`` / ``dh.update()`` to ``emit``."""

    emit: Callable[[DisplayEvent], None]

    @override
    def clear_output(self, wait: bool = False) -> None:  # noqa: FBT001, FBT002
        # Base writes ``\033[2K\r`` to stdout. Emit a marker instead so
        # ``<Output>`` can drop prior events for this turn; the model-facing
        # render honours it by truncating.
        self.emit(DisplayEvent(id=uuid4().hex, bundle={}, meta={"clear_output": True}))

    @override
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
        self.emit(
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

    @override
    def quiet(self) -> bool:
        # Base reads ``history_manager.input_hist_parsed[-1]`` — the most
        # recently *submitted* cell's source, not the currently-executing
        # one — so under concurrent cells a later cell ending in ``;`` would
        # silently suppress an earlier cell's last-expr. The agent doesn't
        # need ``;``-suppression; disable it.
        return False

    @override
    def write_output_prompt(self) -> None:
        pass

    @override
    def write_format_data(  # type: ignore[override]
        self, format_dict: dict[str, Any], md_dict: dict[str, Any] | None = None
    ) -> None:
        assert (
            self.shell is not None
        )  # set at construction; traitlets types it Optional
        self.shell.display_pub.publish(
            format_dict, md_dict, transient={"execute_result": True}
        )

    @override
    def log_output(self, *_: Any) -> None:
        pass

    @override
    def update_user_ns(self, result: Any) -> None:
        # Base writes ``_`` / ``_N`` / ``_oh[N]`` keyed on the shared
        # ``execution_count`` — collides under concurrent cells. The agent
        # is told not to rely on ``_`` / ``Out[]``; drop the write.
        pass

    @override
    def finish_displayhook(self) -> None:
        # Skip the base's ``sys.stdout.write("\n")``; keep the
        # ``_is_active`` reset so the flag doesn't stick ``True`` forever.
        self._is_active = False


class _CellStream(io.TextIOBase):
    """A line-buffered stdout/stderr tee that emits stream ``DisplayEvent`` s.

    Attribution uses the ``current_turn`` ``ContextVar``: each cell task sets
    it before awaiting ``run_cell_async``, so a ``print()`` from concurrent
    cell A resolves to A's turn even while cell B is also live. Buffering is
    per-turn so interleaved partial writes from concurrent cells don't
    concatenate. Writes from outside any cell (server logs, etc.) fall
    through to the real stream unchanged.
    """

    encoding = "utf-8"

    @override
    def writable(self) -> bool:
        return True

    @override
    def fileno(self) -> int:
        # Subprocess/C-level output writes to the real fd and bypasses
        # capture (M1-KERNEL-NOTES.md); at least don't break callers that
        # probe ``fileno()``.
        return self._real.fileno()

    def __init__(
        self,
        emit: Callable[[DisplayEvent], None],
        current_turn: contextvars.ContextVar[int | None],
        name: str,
        real: io.TextIOBase,
    ) -> None:
        self._emit_fn = emit
        self._current_turn = current_turn
        self._name = name
        self._real = real
        self._buf: dict[int, str] = {}

    @override
    def write(self, s: str) -> int:
        turn = self._current_turn.get()
        if turn is None:
            self._real.write(s)
            return len(s)
        buf = self._buf.pop(turn, "") + s
        *lines, rest = buf.split("\n")
        for line in lines:
            self._emit(line + "\n")
        if rest:
            self._buf[turn] = rest
        return len(s)

    @override
    def flush(self) -> None:
        turn = self._current_turn.get()
        if turn is not None and (rest := self._buf.pop(turn, "")):
            self._emit(rest)
        self._real.flush()

    def _emit(self, text: str) -> None:
        self._emit_fn(
            DisplayEvent(
                id=uuid4().hex,
                bundle={STREAM_MIME: {"name": self._name, "text": text}},
            )
        )


def install_workbench_hooks(
    shell: InteractiveShell, emit: Callable[[DisplayEvent], None]
) -> None:
    """Repoint ``shell``'s output hooks at ``emit``.

    Done post-hoc rather than via ``config=`` because ``.instance()`` may
    already exist (e.g. inspect's notebook util imported first) and a
    second ``instance(config=…)`` call is a no-op. Three places cache the
    displayhook at init: ``shell.displayhook``, ``shell.display_trap.hook``
    (entered by ``run_cell_async``), and ``sys.displayhook`` — all must
    point at the same ``WorkbenchDisplayHook`` instance.
    """
    pub = WorkbenchDisplayPublisher()
    pub.emit = emit
    shell.display_pub = pub
    # ``cache_size=0`` because ``_``/``_oh[N]`` are keyed on the shared
    # ``execution_count`` and collide under concurrent cells anyway; we
    # override ``update_user_ns`` to a no-op regardless.
    shell.displayhook = WorkbenchDisplayHook(shell=shell, cache_size=0)
    shell.display_trap.hook = shell.displayhook
    # Do NOT also assign ``sys.displayhook`` here — ``display_trap`` is
    # entered on every cell and installs the hook; if it's *already*
    # installed, ``DisplayTrap.set()`` skips saving ``old_hook`` and
    # ``unset()`` then restores ``sys.displayhook = None``.
    #
    # Compact ``text/plain`` for figure types whose default repr is the
    # full data dict (multi-KB straight into the tool result). Registered
    # by name so plotly/mpl needn't be importable here.
    assert shell.display_formatter is not None
    plain = shell.display_formatter.formatters["text/plain"]
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
