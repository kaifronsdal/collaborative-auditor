"""M1 shared run primitives — post M1-HYBRID step 6.

The in-process ``eval_async`` launcher (``RunHandle.launch`` /
``AuditRunHandle.launch``), the ``CONTROL`` steer/stop registry,
``BatchHooks``, and ``snapshot_running``/``adopt_running`` are gone —
evals now run in subprocesses via the ``bash`` tool and are observed
via :class:`workbench.m1.attach.AttachedRun`. See ``M1-HYBRID.md`` and
``patches/CONCURRENT-EVAL-DESIGN.md`` for why concurrent in-process
``eval_async`` was abandoned.

What remains here is what ``AttachedRun`` / ``ScanHandle`` /
``review_seeds`` still share:

- :class:`RunProposal` — the ``review_seeds`` gate card.
- :class:`SampleRow` / :class:`_PollingHandle` — base for
  ``AttachedRun`` and ``ScanHandle``.
- :class:`ScanHandle` — scout runs in-process (no ``eval_async``, so no
  concurrent-eval hazard).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from uuid import uuid4

from IPython.display import DisplayHandle, display

from workbench.m1.kernel import WB_MIME

# -- proposals (gated cards) --------------------------------------------------


@dataclass
class RunProposal:
    """The pre-launch gate card for ``review_seeds``.

    ``resolve()`` receives the WS ``approve`` verdict (``{"denied": bool,
    "seeds": list[str] | None, "reason": str | None}``); the human may have
    struck seeds. The resolved bundle shows ``approved by …`` and the
    surviving count.
    """

    seeds: list[str]
    config: dict[str, Any]
    description: str
    n_per_seed: int = 1
    #: Target-model id for the config line on the gate card (UI-AUDIT §A).
    model: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    verdict: dict[str, Any] | None = None

    @property
    def n(self) -> int:
        return len(self.seeds) * self.n_per_seed

    def resolve(self, verdict: Any) -> None:
        v = verdict or {}
        self.verdict = v
        if not isinstance(v, dict):
            return
        # Frontend (UI-AUDIT §A) sends ``surviving`` as a list of seed *ids*
        # (``s{i}`` — matching the ``seeds`` payload below); accept that, or
        # the older ``seeds`` shape (list of texts, or list of ``{id,text}``).
        if (surviving := v.get("surviving")) is not None:
            keep = set(surviving)
            self.seeds = [s for i, s in enumerate(self.seeds) if f"s{i}" in keep]
        elif (edited := v.get("seeds")) is not None:
            self.seeds = [
                e["text"] if isinstance(e, dict) else e for e in edited
            ]

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
                "seeds": [
                    {"id": f"s{i}", "text": s[:200]} for i, s in enumerate(self.seeds)
                ],
                "config": {
                    "model": self.model,
                    "n_per_seed": self.n_per_seed,
                    **self.config,
                },
                "pending": pending,
                "verdict": self.verdict,
            },
        }


# -- run handles --------------------------------------------------------------


SampleStatus = Literal["running", "done", "error", "stopped"]


def _finite_or_none(v: Any) -> Any:
    """Map non-finite floats (``nan`` / ``inf``) to ``None``.

    ``jsonable_python`` leaves them as-is and stdlib ``json.dumps`` then emits
    bare ``NaN`` / ``Infinity`` — invalid JSON that breaks the frontend's
    ``JSON.parse``. Applied to score values before they reach the WB_MIME
    payload (mockllm + petri judge yields ``float('nan')``).
    """
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _first_numeric(scores: dict[str, Any]) -> float | None:
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
    whenever ``_signature()`` changes; after ``_task`` settles it does one
    final poll, sets ``finished`` / ``error``, calls ``_settle()``, and fires
    a last update. Subclasses supply ``_poll`` / ``_signature`` / ``_settle``
    / ``n_done`` / ``_repr_mimebundle_`` and their domain fields.
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

    async def wait(self) -> Self:
        """Await the job *and* the watcher's final poll/update.

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

    def _update(self) -> None:
        if self._dh is not None:
            self._dh.update(self)

    def _start(self, coro: Any) -> Self:
        """Display the card, spawn the job + watcher. Owns the field wiring
        so the ``display → task → watcher`` invariant lives with the class."""
        self._dh = display(self, display_id=self.id)
        self._task = asyncio.create_task(coro)
        self._watcher = asyncio.create_task(self._watch(self._task))
        return self

    async def _watch(self, task: asyncio.Task[Any]) -> None:
        last: Any = None
        while not task.done():
            await self._poll()
            sig = self._signature()
            if sig != last:
                last = sig
                self._update()
            await asyncio.sleep(self._poll_interval)
        # final poll after the job settles (last flush may land after done)
        await self._poll()
        self.finished = True
        if task.cancelled():
            self.error = "cancelled"
        elif (exc := task.exception()) is not None:
            self.error = f"{type(exc).__name__}: {exc}"
        await self._settle(task)
        self._update()

    # -- hooks ------------------------------------------------------------

    async def _poll(self) -> None:
        raise NotImplementedError

    def _signature(self) -> Any:
        raise NotImplementedError

    async def _settle(self, task: asyncio.Task[Any]) -> None:
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

    def _signature(self) -> tuple[Any, ...]:
        return tuple(v for s in self.per_scanner.values() for v in sorted(s.items()))

    async def _settle(self, task: asyncio.Task[Any]) -> None:
        from inspect_scout.aio import scan_results_df_async  # noqa: PLC0415

        self.total = self.total or self.n_done
        if self.error is None and self._location is None:
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
                **({"df_head": self._df_head} if self.finished else {}),
            },
        }
