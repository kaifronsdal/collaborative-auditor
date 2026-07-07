"""M1 live-job handles — post M1-HYBRID step 6 / M1-REFACTOR Batch A.

The in-process ``eval_async`` launcher (``RunHandle.launch`` /
``AuditRunHandle.launch``), the ``CONTROL`` steer/stop registry,
``BatchHooks``, and ``snapshot_running``/``adopt_running`` are gone —
evals now run in subprocesses via the ``bash`` tool and are observed
via :class:`workbench.m1.attach.AttachedRun`. See ``M1-HYBRID.md`` and
``patches/CONCURRENT-EVAL-DESIGN.md`` for why concurrent in-process
``eval_async`` was abandoned.

What remains here is the polling-handle machinery ``AttachedRun`` and
``ScanHandle`` share:

- :class:`SampleRow` / :class:`_PollingHandle` — base for
  ``AttachedRun`` and ``ScanHandle``.
- :class:`ScanHandle` — scout runs in-process (no ``eval_async``, so no
  concurrent-eval hazard).

``RunProposal`` (the ``review_seeds`` gate card) lives in
:mod:`workbench.m1.proposals`.
"""

from __future__ import annotations

import asyncio
import html
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from uuid import uuid4

from IPython.display import DisplayHandle, display

from workbench.m1.wire import ScanPayload, wb_bundle

# -- run handles --------------------------------------------------------------


SampleStatus = Literal["running", "done", "error", "stopped"]


def first_numeric(scores: dict[str, Any]) -> float | None:
    """First numeric score value in ``scores`` (M1-FEATURES §8 histogram)."""
    for v in scores.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            return f if math.isfinite(f) else None
    return None


@dataclass
class SampleRow:
    id: str
    status: SampleStatus
    epoch: int = 1
    input: str = ""
    turns: int | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(kw_only=True)
class _PollingHandle:
    """Live handle on one background job with a polling watcher.

    ``_watch`` calls ``_poll()`` on an interval and ``dh.update(self)`` s
    whenever ``_signature()`` changes; after ``_done()`` flips it does one
    final poll, sets ``finished`` / ``error``, calls ``_settle()``, and
    fires a last update. Subclasses supply ``_poll`` / ``_signature`` /
    ``_settle`` / ``n_done`` / ``_repr_mimebundle_`` and their domain
    fields; a subclass with no ``_task`` (:class:`~.attach.AttachedRun`)
    overrides ``_done`` instead of the whole loop.
    """

    id: str = field(default_factory=lambda: uuid4().hex)
    description: str = ""
    total: int = 0
    finished: bool = False
    error: str | None = None
    kind: str = ""  # WB_MIME dispatch key

    _task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _watcher: asyncio.Task[None] | None = field(default=None, repr=False)
    _dh: DisplayHandle | None = field(default=None, repr=False)
    _poll_interval: float = field(default=0.25, repr=False)
    _started: float = field(default_factory=time.monotonic, repr=False)

    async def wait(self, timeout: float | None = None) -> Self:
        """Await the job *and* the watcher's final poll/update.

        ``_task`` alone isn't enough — ``_watch`` sets ``finished`` and fires
        the last ``dh.update`` after ``_task.done()``, so returning before it
        settles would hand back a stale card.

        ``timeout`` bounds the wait: on expiry the watcher is cancelled and
        the handle marked ``finished`` with ``error = "wait timeout after
        {timeout}s"`` — the agent's cell gets a settled handle it can inspect
        (``.error`` / ``.n_done``), not a traceback.
        """
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._watcher is not None:
            try:
                await asyncio.wait_for(self._watcher, timeout)
            except TimeoutError:
                self.error = f"wait timeout after {timeout}s"
                self.finished = True
                self._update()
        return self

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _update(self) -> None:
        if self._dh is not None:
            self._dh.update(self)

    def _start(self, coro: Any) -> Self:
        """Display the card, spawn the job + watcher. Owns the field wiring
        so the ``display → task → watcher`` invariant lives with the class."""
        self._dh = display(self, display_id=self.id)
        self._task = asyncio.create_task(coro)
        self._watcher = asyncio.create_task(self._watch())
        return self

    async def _watch(self) -> None:
        last: Any = None
        while not self._done():
            await self._poll()
            sig = self._signature()
            if sig != last:
                last = sig
                self._update()
            if self._done():
                break
            await asyncio.sleep(self._poll_interval)
        # final poll after the job settles (last flush may land after done)
        await self._poll()
        self.finished = True
        if self._task is not None:
            if self._task.cancelled():
                self.error = "cancelled"
            elif (exc := self._task.exception()) is not None:
                self.error = f"{type(exc).__name__}: {exc}"
        await self._settle(self._task)
        self._update()

    # -- hooks ------------------------------------------------------------

    def _done(self) -> bool:
        return self._task is not None and self._task.done()

    async def _poll(self) -> None:
        raise NotImplementedError

    def _signature(self) -> Any:
        raise NotImplementedError

    async def _settle(self, task: asyncio.Task[Any] | None) -> None:
        pass


@dataclass(kw_only=True)
class ScanHandle(_PollingHandle):
    """Live handle on one ``inspect_scout.aio.scan_async`` job.

    ``_watch`` polls ``scan_status_async`` on the scan's location (resolved
    via ``scan_list_async(scans_dir)`` — one scan per fresh temp dir) and
    ``dh.update(self)`` s on any change. ``.df`` is the
    ``ScanResultsDF.scanners`` mapping (name → DataFrame), populated by the
    watcher's final poll so ``await h.wait(); h.df["…"]`` works without a
    second async call.

    ``total`` stays 0 until the job settles (scout only exposes it via a
    PID-keyed KV store that concurrent in-process scans overwrite); the card
    reports ``N scanned`` while running and ``N/N`` once ``finished``.
    """

    scans_dir: str
    scanner_names: list[str]
    #: name → {scans, results, errors} — mirrors ``Summary.scanners``.
    per_scanner: dict[str, dict[str, int]] = field(default_factory=dict)
    kind: str = "scan"

    _location: str | None = field(default=None, repr=False)
    _results: Any | None = field(default=None, repr=False)
    _df_head: dict[str, str] = field(default_factory=dict, repr=False)

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

    # -- hooks ------------------------------------------------------------

    async def _poll(self) -> None:
        from inspect_scout.aio import (
            scan_list_async,
            scan_status_async,
        )

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

    def _signature(self) -> tuple[Any, ...]:
        return tuple(v for s in self.per_scanner.values() for v in sorted(s.items()))

    async def _settle(self, task: asyncio.Task[Any] | None) -> None:
        from inspect_scout.aio import scan_results_df_async

        self.total = self.total or self.n_done
        if self.error is None and self._location is None and task is not None:
            # ``scan_async`` returned a Status; take its location.
            self._location = task.result().location
        if self._location is not None and self.error is None:
            self._results = await scan_results_df_async(self._location)
            # ProgressCard renders df_head as HTML per scanner (UI-AUDIT §C).
            self._df_head = {
                name: df.head(3).to_html(
                    classes="dataframe scan-preview", border=0, index=False
                )
                for name, df in self._results.scanners.items()
            }

    # -- repr -------------------------------------------------------------

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        return wb_bundle(*self._payload())

    def _payload(self) -> tuple[str, ScanPayload]:
        state = "done" if self.finished else "running"
        if self.error:
            state = self.error
        counter = f"{self.n_done}/{self.total}" if self.total else f"{self.n_done}"
        payload: ScanPayload = {
            "kind": "scan",
            "id": self.id,
            "description": self.description,
            "scans_dir": self.scans_dir,
            "location": self._location,
            "done": self.n_done,
            "total": self.total,
            "finished": self.finished,
            "error": self.error,
            "per_scanner": self.per_scanner,
        }
        if self.finished:
            payload["df_head"] = self._df_head
        return (
            f"<ScanHandle {'+'.join(self.scanner_names)} · {counter} scanned · {state}>",
            payload,
        )


def branch_scan_payload(
    scan_id: str,
    description: str,
    names: list[str],
    results: dict[str, Any] | None = None,
    *,
    error: str | None = None,
) -> tuple[str, ScanPayload]:
    """Build the ``(text, ScanPayload)`` pair for an M0 branch scan (P1.8b).

    Matches :meth:`ScanHandle._payload`'s shape so ``ProgressCard``'s scan
    variant renders it unchanged. ``results`` is the ``scan_messages`` output
    (``{name: Result | Exception}``); ``None`` means "still running". There is
    no ``scans_dir`` / ``location`` — the scan ran on live in-memory messages,
    not a scout DB — so both are empty and ``df_head`` is a hand-built one-row
    ``value / explanation`` table per scanner.
    """
    finished = results is not None
    per_scanner: dict[str, dict[str, int]] = {}
    df_head: dict[str, str] = {}
    for name in names:
        r = (results or {}).get(name)
        is_err = isinstance(r, Exception)
        per_scanner[name] = {
            "scans": 1 if finished else 0,
            "results": 1 if finished and not is_err else 0,
            "errors": 1 if is_err else 0,
        }
        if finished:
            if is_err:
                val, expl = type(r).__name__, str(r)
            else:
                val = getattr(r, "value", r)
                expl = getattr(r, "explanation", None) or getattr(r, "answer", "") or ""
            df_head[name] = (
                '<table class="dataframe scan-preview"><thead><tr>'
                "<th>value</th><th>explanation</th></tr></thead><tbody><tr>"
                f"<td>{html.escape(str(val))}</td>"
                f"<td>{html.escape(str(expl))}</td>"
                "</tr></tbody></table>"
            )
    done = sum(s["scans"] for s in per_scanner.values())
    payload: ScanPayload = {
        "kind": "scan",
        "id": scan_id,
        "description": description,
        "scans_dir": "",
        "location": None,
        "done": done,
        "total": len(names),
        "finished": finished,
        "error": error,
        "per_scanner": per_scanner,
    }
    if finished:
        payload["df_head"] = df_head
    state = error or ("done" if finished else "running")
    return f"<scan {'+'.join(names)} · {done}/{len(names)} · {state}>", payload
