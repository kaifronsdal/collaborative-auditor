"""M1.2 — ``wb.run_audits`` / ``wb.run_eval`` shared launcher (M1-RUN-AUDITS.md).

Both are ``eval_async(task, log_dir=…, log_buffer=1)`` under the hood; the
petri specialization lives entirely in ``RunProposal`` / ``AuditRunHandle``.
The ``RunHandle`` polls the ``.eval`` log (samples flush per-completion with
``log_buffer=1``) and ``dh.update(self)`` s so the card ticks live.

Steering: a process-local ``CONTROL[sample_id]`` registry that a cooperating
solver drains before each generate — same 3-line pattern as M0's
``Branch.queued["auditor"]``, keyed by sample instead of branch. ``stop()``
also uses ``active_samples()[i].interrupt("score")`` so it works on any task
without cooperation.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Self
from uuid import uuid4

from inspect_ai import Task, eval_async
from inspect_ai.log import EvalSampleSummary, list_eval_logs
from inspect_ai.log._file import (  # noqa: PLC2701
    read_eval_log_sample_summaries_async,
)
from inspect_ai.log._samples import active_samples  # noqa: PLC2701
from inspect_ai.model import ChatMessageUser
from IPython.display import DisplayHandle, display

from workbench.m1.kernel import WB_MIME

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


async def snapshot_running(sample_id: str) -> tuple[Any, Any]:
    """Read a running sample's ``AuditTape`` directly from its live ``Store``.

    v2 of ``import_running`` (M1-RUN-AUDITS.md §Desk). ``ActiveSample.store``
    on the inspect fork (@ ``536002a8``) exposes the sample's ``Store``
    without a flush, so ``AuditTape(store=s.store)`` reads the tape
    in-process — the batch sample keeps running; the desk gets a fork at
    the current turn. Same ``(History, BranchMeta)`` shape as
    ``import_eval``.

    Petri's ``audit_solver`` only writes ``trajectories`` in its
    ``finally`` — the per-turn checkpoint that makes this readable
    mid-run comes from ``BatchHooks.post_generate``. If the store is
    unset or ``trajectories`` is still empty (before the first generate
    returns, or a non-``BatchHooks`` auditor), fall back to
    ``adopt_running`` (interrupt + flush + ``import_eval``).

    Model roles: ``ActiveSample`` only carries ``.model`` (the eval's
    primary model), not ``model_roles``, so both ``BranchMeta`` roles
    default to it. The desk can retarget on the imported ``Branch``.
    """
    from inspect_petri._auditor import AuditTape  # noqa: PLC0415
    from inspect_petri.target import History  # noqa: PLC0415

    from workbench.run import BranchMeta  # noqa: PLC0415

    s = next((s for s in active_samples() if str(s.sample.id) == str(sample_id)), None)
    if s is None:
        raise ValueError(f"sample {sample_id!r} not running")
    if s.store is None:
        return await adopt_running(sample_id)
    tape = AuditTape(store=s.store)
    if not tape.trajectories:
        return await adopt_running(sample_id)
    history = History.load(tape.trajectories)
    meta = BranchMeta(
        seed=tape.seed_instructions,
        auditor_model=s.model,
        target_model=s.model,
        max_turns=None,
    )
    return history, meta


async def adopt_running(sample_id: str, *, timeout: float = 5.0) -> tuple[Any, Any]:
    """Punch down into a running batch sample: stop it, then load its tape.

    ``interrupt("score")`` cancels the sample's task group; petri's
    ``run_audit`` finally dumps the live ``AuditTape.trajectories`` into the
    sample's ``Store``; the eval recorder flushes it to ``.eval`` (with
    ``log_buffer=1`` this is immediate). Then read it back via
    ``import_eval`` — same ``(History, BranchMeta)`` the existing
    ``server._dispatch("import")`` case consumes. Adopt semantics: the batch
    loses this sample; the desk picks it up at the exact turn it was on.

    Prefer ``snapshot_running`` (reads the live store without stopping);
    this remains as the ``snapshot=False`` opt-in and as the fallback when
    the store's ``trajectories`` haven't been checkpointed yet.
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
        # Checkpoint the L2 tape into the sample's ``Store`` after each
        # generate so ``snapshot_running`` can read it without
        # interrupting. Petri's ``audit_solver`` only dumps
        # ``trajectories`` in its ``finally`` — without this the store's
        # ``AuditTape.trajectories`` stays empty until the sample ends.
        # ``dump()`` returns a fresh list, so a concurrent snapshot read
        # sees a self-consistent value (replaced, never mutated in place).
        from inspect_petri._auditor import AuditTape, audit_trajectory  # noqa: PLC0415
        from inspect_petri.target import History  # noqa: PLC0415

        if (t := audit_trajectory()) is not None:
            AuditTape().trajectories = History().dump(root=t)


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
                # Decision-relevant config for the gate card (UI-AUDIT §A).
                # ``model`` isn't carried on the proposal (only on the
                # ``run_audits`` call); ``max_turns`` comes through
                # ``self.config`` if set.
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
class RunHandle(_PollingHandle):
    """Live handle on one ``eval_async`` (or subprocess) run.

    ``_watch`` polls the ``.eval`` log via ``read_eval_log_sample_summaries``
    and ``dh.update(self)`` s on any change; the model-facing render collapses
    to the latest counters at the initial ``display()`` position.
    """

    task_name: str
    log_dir: str
    #: rows keyed by sample id — completed samples only (``log_buffer=1``
    #: flushes each on completion). In-flight samples are polled separately
    #: from ``active_samples()`` into ``_running``.
    rows: dict[str, SampleRow] = field(default_factory=dict)
    kind: str = "eval_run"

    _running: list[SampleRow] = field(default_factory=list, repr=False)
    _log_file: str | None = field(default=None, repr=False)

    @property
    def n_done(self) -> int:
        return sum(1 for r in self.rows.values() if r.status == "done")

    @property
    def running_ids(self) -> list[str]:
        """Per-sample ids currently in flight — the ``ids`` for ``wb.steer``.

        Distinct from ``self.id`` (the batch/card id). Populated from
        ``active_samples()`` each poll; empty once every sample has flushed.
        """
        return [r.id for r in self._running]

    @property
    def location(self) -> str | None:
        return self._log_file

    @classmethod
    def launch(
        cls,
        task: Task,
        *,
        total: int,
        log_dir: str | None = None,
        description: str = "",
        id: str | None = None,  # noqa: A002
        **eval_kw: Any,
    ) -> Self:
        """``display(h)`` first so the card appears before the first
        ``await``; then spawn ``eval_async`` and a poller. Returns
        immediately — callers ``await h.wait()`` if they want the result
        inline. Each call gets its own ``log_dir`` (concurrent-``eval_async``
        safety)."""
        log_dir = log_dir or tempfile.mkdtemp(prefix="wb-run-")
        h = cls(
            task_name=task.name or "task",
            log_dir=log_dir,
            total=total,
            description=description,
        )
        if id is not None:
            h.id = id
        return h._start(eval_async(task, log_dir=log_dir, log_buffer=1, **eval_kw))

    # -- hooks ------------------------------------------------------------

    async def _poll(self) -> None:
        if self._log_file is None:
            # ``list_eval_logs`` (not ``active_samples()[i].log_location``)
            # because the latter is set before the first flush — reading a
            # mid-write zip raises ``EOCD not found``. The directory walk
            # only returns files the recorder has actually copied out.
            self._log_file = next((i.name for i in list_eval_logs(self.log_dir)), None)
        if self._log_file is not None:
            # The recorder copies its temp zip to ``_log_file`` per flush
            # (``log_buffer=1``); reading during that copy hits an
            # incomplete zip (``EOCD not found``). Next tick reads the
            # settled file.
            try:
                summaries = await read_eval_log_sample_summaries_async(self._log_file)
            except (zipfile.BadZipFile, ValueError):
                summaries = []
            for s in summaries:
                self.rows[f"{s.id}#{s.epoch}"] = self._row(s)
        # In-flight samples (not yet flushed) come from the process-local
        # registry; filter to this run's log_dir and skip any that already
        # landed in ``self.rows`` (brief overlap at completion).
        self._running = [
            SampleRow(
                id=str(s.sample.id),
                status="running",
                epoch=s.epoch,
                input=str(s.sample.input)[:80],
                turns=s.total_messages // 2,
            )
            for s in active_samples()
            if s.log_location.startswith(self.log_dir + os.sep)
            and f"{s.sample.id}#{s.epoch}" not in self.rows
        ]

    def _signature(self) -> tuple[Any, ...]:
        return (
            len(self.rows),
            self.n_done,
            tuple((r.id, r.turns) for r in self._running),
        )

    async def _settle(self, task: asyncio.Task[Any]) -> None:
        # Drop this run's control entries — sample ids can collide across
        # runs, and ``_row`` reads ``CONTROL[id].stop`` to distinguish
        # stopped from errored.
        for r in self.rows.values():
            CONTROL.pop(r.id, None)

    def _row(self, s: EvalSampleSummary) -> SampleRow:
        status: SampleStatus = "done"
        if s.error:
            status = (
                "stopped" if CONTROL.get(str(s.id), SampleControl()).stop else "error"
            )
        turns = (s.metadata or {}).get("turns")
        if turns is None and s.message_count is not None:
            turns = s.message_count // 2
        return SampleRow(
            id=str(s.id),
            status=status,
            epoch=s.epoch,
            input=str(s.input)[:80],
            turns=turns,
            scores={k: v.value for k, v in (s.scores or {}).items()},
            error=s.error,
        )

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
                "elapsed": f"{time.monotonic() - self._started:.0f}s",
                "rows": {
                    "running": [vars(r) for r in self._running],
                    "done": [vars(r) for r in self.rows.values()],
                },
            },
        }


@dataclass(kw_only=True)
class ScanHandle(_PollingHandle):
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


@dataclass(kw_only=True)
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

        Only valid once the run has settled — ``samples_df`` on a mid-write
        ``.eval`` raises an opaque ``KeyError: 'eval_id'``; fail with a
        useful message so the agent knows to ``await handle.wait()`` first.
        """
        if not self.finished:
            raise RuntimeError(
                f"{self.task_name}: .audits only valid after `await handle.wait()` "
                f"({self.n_done}/{self.total} done, {len(self._running)} running)"
            )
        from inspect_petri import audits_df  # noqa: PLC0415

        return audits_df(self.log_dir)
