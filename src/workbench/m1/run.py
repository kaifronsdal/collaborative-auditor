"""M1.2 — ``wb.run_audits`` / ``wb.run_eval`` shared launcher (M1-RUN-AUDITS.md).

Both are ``eval_async(task, log_dir=…, log_buffer=1)`` under the hood; the
petri specialization lives entirely in ``RunProposal`` / ``AuditRunHandle``.
The ``RunHandle`` polls the ``.eval`` log (samples flush per-completion with
``log_buffer=1``) and ``dh.update(self)`` s so the card ticks live.

Steering: a process-local ``CONTROL[sample_id]`` registry that a cooperating
solver drains before each generate — same 3-line pattern as M0's
``workbench_auditor.queued``, keyed by sample instead of branch. ``stop()``
also uses ``active_samples()[i].interrupt("score")`` so it works on any task
without cooperation.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from inspect_ai import Task, eval_async
from inspect_ai.log import EvalSampleSummary, list_eval_logs
from inspect_ai.log._file import (  # noqa: PLC2701
    read_eval_log_sample_summaries_async,
)
from inspect_ai.log._samples import active_samples  # noqa: PLC2701
from inspect_ai.model import ChatMessageUser
from inspect_ai.util._display import init_display_type  # noqa: PLC2701
from IPython.display import DisplayHandle, display

from workbench.m1.kernel import WB_MIME

# -- pre-warm (call once at kernel init; M1-RUN-AUDITS.md §Required) ----------


def prewarm() -> None:
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


# -- steering registry --------------------------------------------------------


@dataclass
class SampleControl:
    """Per-sample control state a cooperating solver drains each turn."""

    queued: list[str] = field(default_factory=list)
    stop: bool = False


#: sample_id → control. Cooperating solvers (``batch_auditor``) drain
#: ``queued`` into ``state.messages`` and honour ``stop`` before each
#: generate. In-process only — subprocess (``detached=True``) runs can't
#: reach this.
CONTROL: dict[str, SampleControl] = {}


def steer(ids: Iterable[str], message: str) -> None:
    for i in ids:
        CONTROL.setdefault(str(i), SampleControl()).queued.append(message)


async def adopt_running(sample_id: str, *, timeout: float = 5.0) -> tuple[Any, Any]:
    """Punch down into a running batch sample: stop it, then load its tape.

    ``interrupt("score")`` cancels the sample's task group; petri's
    ``run_audit`` finally dumps the live ``AuditTape.trajectories`` into the
    sample's ``Store``; the eval recorder flushes it to ``.eval`` (with
    ``log_buffer=1`` this is immediate). Then read it back via
    ``import_eval`` — same ``(History, BranchMeta)`` the existing
    ``server._dispatch("import")`` case consumes. Adopt semantics: the batch
    loses this sample; the desk picks it up at the exact turn it was on.

    v2 (snapshot without stopping) needs ``ActiveSample.store`` on the
    inspect fork — 3-line addition (M1-REFACTOR-NOTES.md).
    """
    from workbench.export import import_eval  # noqa: PLC0415

    s = next((s for s in active_samples() if str(s.sample.id) == str(sample_id)), None)
    if s is None:
        raise ValueError(f"sample {sample_id!r} not running")
    log = s.log_location
    s.interrupt("score")
    # Wait for the recorder to flush this sample.
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        summaries = await read_eval_log_sample_summaries_async(log)
        if any(str(x.id) == str(sample_id) for x in summaries):
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError(f"sample {sample_id!r} did not flush within {timeout}s")
    return import_eval(log, sample_id)


def stop(ids: Iterable[str], *, hard: bool = False) -> None:
    """Request each sample end at its next turn boundary.

    ``hard=True`` also fires ``ActiveSample.interrupt("score")`` — cancels
    the sample's task group immediately, no cooperation needed. Works on any
    task; the ``CONTROL`` flag is the graceful path for cooperating solvers.
    """
    want = {str(i) for i in ids}
    for i in want:
        CONTROL.setdefault(i, SampleControl()).stop = True
    if hard:
        for s in active_samples():
            if str(s.sample.id) in want:
                s.interrupt("score")


class BatchHooks:
    """``TurnHooks`` for ``wb.run_audits`` — free-running, drains ``CONTROL``.

    Resolves ``sample_id`` per call via ``sample_state()`` (the auditor
    ``Agent`` is constructed once and shared across all samples in the
    batch, so the hooks can't carry a fixed id).
    """

    async def pre_turn(self) -> tuple[list[ChatMessageUser], bool]:
        from inspect_ai.solver._task_state import sample_state  # noqa: PLC0415, PLC2701

        st = sample_state()
        return drain_control(st.sample_id if st else None)

    def post_generate(self) -> None:
        pass


def drain_control(sample_id: Any) -> tuple[list[ChatMessageUser], bool]:
    """The cooperating-solver hook: (messages to inject, stop-requested).

    A batch auditor calls this before each generate::

        injected, stop_now = drain_control(state.sample_id)
        state.messages.extend(injected)
        if stop_now:
            await channel.end_conversation(); break
    """
    ctl = CONTROL.get(str(sample_id))
    if ctl is None:
        return [], False
    msgs = [ChatMessageUser(content=m, source="operator") for m in ctl.queued]
    ctl.queued.clear()
    return msgs, ctl.stop


# -- proposals (gated cards) --------------------------------------------------


class Denied(Exception):  # noqa: N818
    """Raised by a gated launcher when the human denies the proposal."""


@dataclass
class RunProposal:
    """The pre-launch gate card for ``run_audits``.

    ``resolve()`` receives the WS ``approve`` verdict (``{"denied": bool,
    "seeds": list[str] | None, "reason": str | None}``); the human may have
    struck seeds. The resolved bundle shows ``approved by …`` and the
    surviving count; ``AuditRunHandle`` then re-uses ``self.id`` so the card
    flips proposal → live in place.
    """

    seeds: list[str]
    config: dict[str, Any]
    description: str
    n_per_seed: int = 1
    id: str = field(default_factory=lambda: uuid4().hex)
    verdict: dict[str, Any] | None = None

    @property
    def n(self) -> int:
        return len(self.seeds) * self.n_per_seed

    def resolve(self, verdict: Any) -> None:
        v = verdict or {}
        self.verdict = v
        if isinstance(v, dict) and (edited := v.get("seeds")) is not None:
            self.seeds = list(edited)

    @property
    def denied(self) -> bool:
        return bool(self.verdict and self.verdict.get("denied"))

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        pending = self.verdict is None
        text = (
            f"<RunProposal {self.id[:6]} · {self.n} audits · {self.description!r} · pending>"
            if pending
            else f"<RunProposal {self.id[:6]} · "
            f"{'DENIED' if self.denied else f'approved · {self.n} audits'}>"
        )
        return {
            "text/plain": text,
            WB_MIME: {
                "kind": "run_proposal",
                "id": self.id,
                "description": self.description,
                "n": self.n,
                "n_per_seed": self.n_per_seed,
                "seeds": [s[:200] for s in self.seeds],
                "config": self.config,
                "pending": pending,
                "verdict": self.verdict,
            },
        }


# -- run handles --------------------------------------------------------------


SampleStatus = Literal["running", "done", "error", "stopped"]


@dataclass
class SampleRow:
    id: str
    status: SampleStatus
    input: str = ""
    scores: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunHandle:
    """Live handle on one ``eval_async`` (or subprocess) run.

    ``_watch`` polls the ``.eval`` log via ``read_eval_log_sample_summaries``
    and ``dh.update(self)`` s on any change; the model-facing render collapses
    to the latest counters at the initial ``display()`` position.
    """

    task_name: str
    log_dir: str
    total: int
    id: str = field(default_factory=lambda: uuid4().hex)
    description: str = ""
    #: rows keyed by sample id — completed samples only (``log_buffer=1``
    #: flushes each on completion; running samples aren't in the summaries
    #: yet).
    rows: dict[str, SampleRow] = field(default_factory=dict)
    finished: bool = False
    error: str | None = None

    _task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _watcher: asyncio.Task[None] | None = field(default=None, repr=False)
    _dh: DisplayHandle | None = field(default=None, repr=False)
    _log_file: str | None = field(default=None, repr=False)
    _poll_interval: float = field(default=0.25, repr=False)

    kind: str = "eval_run"  # WB_MIME dispatch key

    @property
    def n_done(self) -> int:
        return sum(1 for r in self.rows.values() if r.status == "done")

    @property
    def location(self) -> str | None:
        return self._log_file

    async def wait(self) -> "RunHandle":
        """Await the eval *and* the watcher's final poll/update.

        ``_task`` alone isn't enough — ``_watch`` sets ``finished`` and fires
        the last ``dh.update`` after ``_task.done()``, so returning before it
        settles would hand back a stale card.
        """
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._watcher is not None:
            await self._watcher
        return self

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # -- polling ----------------------------------------------------------

    async def _watch(self, eval_task: asyncio.Task[Any]) -> None:
        last: tuple[int, ...] | None = None
        while not eval_task.done():
            await self._poll()
            sig = (len(self.rows), self.n_done)
            if sig != last:
                last = sig
                self._update()
            await asyncio.sleep(self._poll_interval)
        # final poll after the eval settles (last flush may land after done)
        await self._poll()
        self.finished = True
        if eval_task.cancelled():
            self.error = "cancelled"
        elif (exc := eval_task.exception()) is not None:
            self.error = f"{type(exc).__name__}: {exc}"
        self._update()
        # Drop this run's control entries — sample ids can collide across
        # runs, and ``_row`` reads ``CONTROL[id].stop`` to distinguish
        # stopped from errored.
        for sid in self.rows:
            CONTROL.pop(sid, None)

    async def _poll(self) -> None:
        if self._log_file is None:
            # ``list_eval_logs`` (not ``active_samples()[i].log_location``)
            # because the latter is set before the first flush — reading a
            # mid-write zip raises ``EOCD not found``. The directory walk
            # only returns files the recorder has actually copied out.
            self._log_file = next((i.name for i in list_eval_logs(self.log_dir)), None)
            if self._log_file is None:
                return
        summaries = await read_eval_log_sample_summaries_async(self._log_file)
        for s in summaries:
            self.rows[str(s.id)] = self._row(s)

    def _row(self, s: EvalSampleSummary) -> SampleRow:
        status: SampleStatus = "done"
        if s.error:
            status = (
                "stopped" if CONTROL.get(str(s.id), SampleControl()).stop else "error"
            )
        return SampleRow(
            id=str(s.id),
            status=status,
            input=str(s.input)[:80],
            scores={k: v.value for k, v in (s.scores or {}).items()},
        )

    def _update(self) -> None:
        if self._dh is not None:
            self._dh.update(self)

    def _start(self, coro: Any) -> "RunHandle":
        """Display the card, spawn the eval + watcher. Owns the field wiring
        so the ``display → task → watcher`` invariant lives with the class,
        not scattered across ``_launch``."""
        self._dh = display(self, display_id=self.id)
        self._task = asyncio.create_task(coro)
        self._watcher = asyncio.create_task(self._watch(self._task))
        return self

    # -- repr -------------------------------------------------------------

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        state = "done" if self.finished else "running"
        if self.error:
            state = self.error
        return {
            "text/plain": (
                f"<{type(self).__name__} {self.task_name} · "
                f"{self.n_done}/{self.total} · {state}>"
            ),
            WB_MIME: {
                "kind": self.kind,
                "id": self.id,
                "task": self.task_name,
                "description": self.description,
                "log_dir": self.log_dir,
                "log": self._log_file,
                "total": self.total,
                "done": self.n_done,
                "finished": self.finished,
                "error": self.error,
                "rows": [vars(r) for r in self.rows.values()],
            },
        }


@dataclass
class ScanHandle:
    """Live handle on one ``inspect_scout.aio.scan_async`` job.

    Same shape as ``RunHandle``: ``_watch`` polls ``scan_status_async`` on the
    scan's location (resolved via ``scan_list_async(scans_dir)`` — one scan per
    fresh temp dir) and ``dh.update(self)`` s on any change. ``.df`` is the
    ``ScanResultsDF.scanners`` mapping (name → DataFrame), populated by the
    watcher's final poll so ``await h.wait(); h.df["…"]`` works without a
    second async call.

    ``total`` stays 0 until the job settles (scout only exposes it via a
    PID-keyed KV store that concurrent in-process scans overwrite); the card
    reports ``N scanned`` while running and ``N/N`` once ``finished``.
    """

    scans_dir: str
    scanner_names: list[str]
    id: str = field(default_factory=lambda: uuid4().hex)
    description: str = ""
    total: int = 0
    #: name → {scans, results, errors} — mirrors ``Summary.scanners``.
    per_scanner: dict[str, dict[str, int]] = field(default_factory=dict)
    finished: bool = False
    error: str | None = None

    _task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _watcher: asyncio.Task[None] | None = field(default=None, repr=False)
    _dh: DisplayHandle | None = field(default=None, repr=False)
    _location: str | None = field(default=None, repr=False)
    _results: Any | None = field(default=None, repr=False)
    _poll_interval: float = field(default=0.25, repr=False)

    kind: str = "scan"

    @property
    def n_done(self) -> int:
        return sum(s.get("scans", 0) for s in self.per_scanner.values())

    @property
    def location(self) -> str | None:
        return self._location

    @property
    def df(self) -> Any:
        """``Mapping[str, DataFrame]`` of scanner results (post-``wait()``)."""
        if self._results is None:
            raise RuntimeError("scan not finished — await h.wait() first")
        return self._results.scanners

    async def wait(self) -> "ScanHandle":
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._watcher is not None:
            await self._watcher
        return self

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _watch(self, scan_task: asyncio.Task[Any]) -> None:
        from inspect_scout.aio import scan_results_df_async  # noqa: PLC0415

        last: tuple[int, ...] | None = None
        while not scan_task.done():
            await self._poll()
            sig = tuple(
                v
                for s in self.per_scanner.values()
                for v in sorted(s.items())  # type: ignore[misc]
            )
            if sig != last:
                last = sig
                self._update()
            await asyncio.sleep(self._poll_interval)
        await self._poll()
        self.finished = True
        self.total = self.total or self.n_done
        if scan_task.cancelled():
            self.error = "cancelled"
        elif (exc := scan_task.exception()) is not None:
            self.error = f"{type(exc).__name__}: {exc}"
        elif self._location is None:
            # ``scan_async`` returned a Status; take its location.
            self._location = scan_task.result().location
        if self._location is not None and self.error is None:
            self._results = await scan_results_df_async(self._location)
        self._update()

    async def _poll(self) -> None:
        from inspect_scout.aio import scan_list_async, scan_status_async  # noqa: PLC0415

        if self._location is None:
            listed = await scan_list_async(self.scans_dir)
            if not listed:
                return
            self._location = listed[0].location
        status = await scan_status_async(self._location)
        self.per_scanner = {
            name: {"scans": s.scans, "results": s.results, "errors": s.errors}
            for name, s in status.summary.scanners.items()
        }

    def _update(self) -> None:
        if self._dh is not None:
            self._dh.update(self)

    def _start(self, coro: Any) -> "ScanHandle":
        self._dh = display(self, display_id=self.id)
        self._task = asyncio.create_task(coro)
        self._watcher = asyncio.create_task(self._watch(self._task))
        return self

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        state = "done" if self.finished else "running"
        if self.error:
            state = self.error
        counter = f"{self.n_done}/{self.total}" if self.total else f"{self.n_done}"
        return {
            "text/plain": (
                f"<ScanHandle {'+'.join(self.scanner_names)} · "
                f"{counter} scanned · {state}>"
            ),
            WB_MIME: {
                "kind": self.kind,
                "id": self.id,
                "description": self.description,
                "scans_dir": self.scans_dir,
                "location": self._location,
                "done": self.n_done,
                "total": self.total,
                "finished": self.finished,
                "error": self.error,
                "per_scanner": self.per_scanner,
            },
        }


@dataclass
class AuditRunHandle(RunHandle):
    """``run_audits`` handle — per-audit rows are ``wb://audit/{id}`` links;
    ``.audits`` returns a per-dimension-scored DataFrame when done."""

    kind: str = "audit_run"

    @property
    def audits(self) -> Any:
        """DataFrame of completed audits with per-dimension score columns.

        ``inspect_petri.audits_df`` = ``samples_df`` + ``flat_score_values``,
        so ``audit_judge``'s dict-valued score becomes one column per
        dimension (``score_concerning``, ``score_deception``, …).
        """
        from inspect_petri import audits_df  # noqa: PLC0415

        return audits_df(self.log_dir)


# -- launcher -----------------------------------------------------------------


async def _launch(
    task: Task,
    *,
    handle_cls: type[RunHandle] = RunHandle,
    handle_id: str | None = None,
    log_dir: str | None = None,
    total: int,
    description: str = "",
    **eval_kw: Any,
) -> RunHandle:
    """Common backend for ``run_audits`` / ``run_eval`` (M1-RUN-AUDITS.md).

    ``display(h, display_id=h.id)`` first so the card appears before the
    first ``await``; then spawn ``eval_async`` and a poller. Returns
    immediately — callers ``await h.wait()`` if they want the result inline.
    Each call gets its own ``log_dir`` (concurrent-``eval_async`` safety).
    """
    log_dir = log_dir or tempfile.mkdtemp(prefix="wb-run-")
    h = handle_cls(
        task_name=task.name or "task",
        log_dir=log_dir,
        total=total,
        description=description,
    )
    if handle_id is not None:
        h.id = handle_id
    return h._start(eval_async(task, log_dir=log_dir, log_buffer=1, **eval_kw))
