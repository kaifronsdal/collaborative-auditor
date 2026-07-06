"""Inspect display driver that emits ``{"wb":...}`` JSON lines to stdout.

Registration
------------
Inspect has no plugin mechanism for displays: ``_display.core.active.display()``
is a hardcoded ``if/elif`` over the built-in ``DisplayType`` literals, and the
``inspect eval`` CLI's ``--display`` option (``envvar=INSPECT_DISPLAY``) is a
``click.Choice`` over that same list — ``INSPECT_DISPLAY=workbench`` is
rejected by click before any Python runs. So the driver is gated on its own
env var, ``WORKBENCH_DISPLAY=1``, and installed by monkeypatching the cached
``_active_display`` module global.

The module is registered as an ``inspect_ai`` entry point (``pyproject.toml``:
``[project.entry-points.inspect_ai] workbench = "workbench.m1.wb_display"``).
Entry points load during model-provider lookup inside ``eval_async`` — after
``display()`` has already cached a ``RichDisplay`` (whose ``run_task_app`` is
a bare ``anyio.run``, so harmless) but *before* ``display().task_screen()`` /
``.task()`` fire. Import-time ``register()`` swaps the cached instance for
:class:`WorkbenchDisplay` and pins ``_display_type = "log"`` so
``display_type_plain()`` stays true and no rich panels leak through.

``_audit_task.py`` also calls ``register()`` on import — belt-and-suspenders
for an editable install whose entry point hasn't been re-synced.

Per-sample detail (``eval_sample_done``) doesn't flow through the ``Display``
protocol — ``TaskDisplay.sample_complete`` only receives ``(complete, total)``.
That line is emitted from a companion ``Hooks`` subscriber
(:class:`_WorkbenchSampleHook`) whose ``on_sample_end`` sees the full
``EvalSample``. The hook is gated on the same env var via ``enabled()``.

Usage (from the ``bash`` tool)::

    WORKBENCH_DISPLAY=1 inspect eval workbench.m1._audit_task:audit \\
        -T seeds_file=... --model ... --log-dir ...
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any

import anyio
from inspect_ai._display.core.display import (
    TR,
    Display,
    Progress,
    TaskDisplay,
    TaskDisplayMetric,
    TaskError,
    TaskProfile,
    TaskResult,
    TaskScreen,
    TaskSpec,
    TaskSuccess,
)
from inspect_ai._util._async import configured_async_backend, run_coroutine
from inspect_ai._util.platform import running_in_notebook
from inspect_ai.hooks import Hooks, SampleEnd, TaskStart, hooks
from inspect_ai.log._samples import active_samples
from inspect_ai.util._throttle import throttle

from workbench.m1.wire import _finite


def _wb(kind: str, **fields: Any) -> None:
    """Emit one ``{"wb": kind, ...}`` line to stdout, flushed."""
    print(json.dumps({"wb": kind, **fields}, default=str), flush=True)


def _enabled() -> bool:
    return os.environ.get("WORKBENCH_DISPLAY", "") not in ("", "0")


# ---------------------------------------------------------------------------
# Display driver
# ---------------------------------------------------------------------------


class WorkbenchDisplay(Display):
    """Text-only ``Display`` that writes structured JSON lines.

    One ``_WBTaskDisplay`` per task; the display keeps no cross-task state
    (the ``bash`` tool consumer keys everything on ``eval_id``).
    """

    def print(self, message: str) -> None:
        # Suppress free-form prints — everything the frontend renders comes
        # from ``{"wb":...}`` lines, and stray text would land in
        # ``.out-stream`` noise.
        pass

    @contextlib.contextmanager
    def progress(self, total: int) -> Iterator[Progress]:
        yield _NullProgress()

    def run_task_app(self, main: Callable[[], Coroutine[None, None, TR]]) -> TR:
        if running_in_notebook():
            return run_coroutine(main())
        return anyio.run(main, backend=configured_async_backend())

    @contextlib.contextmanager
    def suspend_task_app(self) -> Iterator[None]:
        yield

    @contextlib.asynccontextmanager
    async def task_screen(
        self, tasks: list[TaskSpec], parallel: bool
    ) -> AsyncIterator[TaskScreen]:
        yield TaskScreen()

    @contextlib.contextmanager
    def task(self, profile: TaskProfile) -> Iterator[TaskDisplay]:
        td = _WBTaskDisplay(profile)
        _by_task_id[profile.task_id] = td
        try:
            yield td
        finally:
            _by_task_id.pop(profile.task_id, None)
            _by_eval_id.pop(td.eval_id, None)

    def display_counter(self, caption: str, value: str) -> None:
        pass


class _NullProgress(Progress):
    def update(self, n: int = 1) -> None:
        pass

    def complete(self) -> None:
        pass


#: Live task displays keyed by ``EvalSpec.task_id`` so the hook (which sees
#: ``eval_id``, not ``task_id``) can find its display via ``on_task_start``.
_by_task_id: dict[str, _WBTaskDisplay] = {}
#: …and by ``eval_id`` once ``on_task_start`` has bridged the two.
_by_eval_id: dict[str, _WBTaskDisplay] = {}


class _WBTaskDisplay(TaskDisplay):
    """Emits ``eval_start`` on entry, ``eval_progress`` on ticks,
    ``eval_done`` on completion. ``eval_sample_done`` comes from the hook.

    ``TaskProfile`` carries ``task_id`` but not ``eval_id``; the hook path
    carries ``eval_id`` but not ``task_id``. ``on_task_start`` (which sees
    both via ``EvalSpec``) bridges them by writing ``eval_id`` back onto
    this instance before any samples run. Until then ``eval_id`` is
    ``task_id`` — good enough for the initial ``eval_start`` since the
    hook fires immediately after ``display().task()`` is entered.
    """

    def __init__(self, profile: TaskProfile) -> None:
        self.profile = profile
        self.eval_id = profile.task_id
        self.started = time.monotonic()
        self.done = 0
        self.errors = 0
        self.total = profile.samples

    def start(self, eval_id: str) -> None:
        self.eval_id = eval_id
        _wb(
            "eval_start",
            eval_id=eval_id,
            task=self.profile.name,
            total=self.total,
            model=str(self.profile.model),
            log_dir=os.path.dirname(self.profile.log_location),
            location=self.profile.log_location,
        )

    @contextlib.contextmanager
    def progress(self) -> Iterator[Progress]:
        yield _WBProgress(self._emit_progress_throttled)

    def sample_complete(self, complete: int, total: int) -> None:
        self.done = complete
        self.total = total
        # Called once with (0, total) at start, then once per finished
        # sample — always emit (the ``bash`` line parser coalesces via
        # ``dh.update`` on the same ``eval_id``).
        self._emit_progress()

    def update_metrics(self, scores: list[TaskDisplayMetric]) -> None:
        pass

    def complete(self, result: TaskResult) -> None:
        done = (
            result.samples_completed
            if isinstance(result, (TaskSuccess, TaskError))
            else self.done
        )
        _wb(
            "eval_done",
            eval_id=self.eval_id,
            location=self.profile.log_location,
            done=done,
            errors=self.errors,
        )

    # -- helpers ----------------------------------------------------------

    @throttle(1)
    def _emit_progress_throttled(self) -> None:
        self._emit_progress()

    def _emit_progress(self) -> None:
        running = [
            {
                "id": str(s.sample.id),
                "epoch": s.epoch,
                "turns": s.total_messages,
                "tokens": s.total_tokens,
            }
            for s in active_samples()
            if s.eval_id == self.eval_id
        ]
        _wb(
            "eval_progress",
            eval_id=self.eval_id,
            done=self.done,
            running=running,
            elapsed=round(time.monotonic() - self.started, 1),
        )


class _WBProgress(Progress):
    """Step-progress hook — the only signal we get *during* a sample."""

    def __init__(self, on_tick: Callable[[], None]) -> None:
        self._on_tick = on_tick

    def update(self, n: int = 1) -> None:
        self._on_tick()

    def complete(self) -> None:
        self._on_tick()


# ---------------------------------------------------------------------------
# Per-sample hook — the Display protocol has no per-sample-with-scores seam.
# ---------------------------------------------------------------------------


@hooks(name="workbench-display", description="emit {'wb':'eval_sample_done'}")
class _WorkbenchSampleHook(Hooks):
    def enabled(self) -> bool:
        return _enabled()

    async def on_task_start(self, data: TaskStart) -> None:
        # Bridge ``eval_id`` (hook world) ↔ ``task_id`` (display world).
        # ``display().task(profile)`` is entered just before this fires, so
        # the ``_WBTaskDisplay`` is already registered under ``task_id``.
        td = _by_task_id.get(data.spec.task_id)
        if td is not None:
            _by_eval_id[data.eval_id] = td
            td.start(data.eval_id)

    async def on_sample_end(self, data: SampleEnd) -> None:
        td = _by_eval_id.get(data.eval_id)
        if td is not None and data.sample.error is not None:
            td.errors += 1
        sample = data.sample
        scores = (
            {name: _finite(s.value) for name, s in sample.scores.items()}
            if sample.scores
            else None
        )
        _wb(
            "eval_sample_done",
            eval_id=data.eval_id,
            id=str(sample.id),
            epoch=sample.epoch,
            scores=scores,
            error=sample.error.message if sample.error else None,
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register() -> None:
    """Install :class:`WorkbenchDisplay` as the active inspect display.

    Idempotent. No-op unless ``WORKBENCH_DISPLAY`` is set. See module
    docstring for why this monkeypatches rather than extending
    ``INSPECT_DISPLAY``.
    """
    if not _enabled():
        return
    from inspect_ai._display.core import active
    from inspect_ai.util import _display as display_type_mod

    if isinstance(active._active_display, WorkbenchDisplay):  # noqa: SLF001
        return
    active._active_display = WorkbenchDisplay()  # noqa: SLF001
    # Pin a plain type so ``display_type_plain()`` is True and no code path
    # tries to re-``init_display_type("workbench")`` → warn → "full".
    display_type_mod._display_type = "log"  # noqa: SLF001


# Entry-point import: install eagerly.
register()
