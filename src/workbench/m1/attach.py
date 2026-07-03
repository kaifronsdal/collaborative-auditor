"""M1-HYBRID §``wb.attach(log_dir)`` — the read-only half of ``RunHandle``.

An ``AttachedRun`` polls a ``log_dir`` that some *other* process is writing
(``bash("inspect eval … --log-dir runs/r1")``) and renders the same
``ProgressCard`` that the in-process ``RunHandle`` did — same
``_repr_mimebundle_`` payload, same ``dh.update`` tick loop — without
launching, cancelling, or otherwise touching the eval.

Two data sources per poll:

- **done rows** — ``read_eval_log_sample_summaries_async`` on the ``.eval``
  file (samples flush per completion with ``--log-buffer 1``). Same
  ``BadZipFile`` guard as ``RunHandle._poll``.
- **running rows** — the eval's ctl server, discovered via
  ``list_discovered_servers()`` and matched on ``log_location``. If no
  server is found (the eval finished before we attached, or ctl was
  disabled) ``running`` stays empty and the handle is log-only.

``.wait()`` polls until the ``.eval`` header's ``status`` leaves
``"started"``. ``.audits`` is ``audits_df(location)`` once finished.
"""

from __future__ import annotations

import asyncio
import os
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Self

import httpx
from inspect_ai._control.discovery import (  # noqa: PLC2701
    DiscoveredControlServer,
    list_discovered_servers,
)
from inspect_ai.log import EvalSampleSummary, list_eval_logs
from inspect_ai.log._file import (  # noqa: PLC2701
    read_eval_log_async,
    read_eval_log_sample_summaries_async,
)
from IPython.display import display

from workbench.m1.kernel import WB_MIME
from workbench.m1.run import (
    SampleRow,
    _finite_or_none,  # noqa: PLC2701
    _first_numeric,  # noqa: PLC2701
    _PollingHandle,  # noqa: PLC2701
)


@dataclass(kw_only=True)
class AttachedRun(_PollingHandle):
    """Read-only handle on an out-of-process eval writing to ``log_dir``.

    Polling only — no ``_task``. ``_watch`` runs until ``_status`` leaves
    ``"started"`` (read from the ``.eval`` header each poll); the base
    ``wait()`` then just awaits the watcher.
    """

    log_dir: str
    task_name: str = ""
    rows: dict[str, SampleRow] = field(default_factory=dict)
    kind: str = "eval_run"

    _running: list[SampleRow] = field(default_factory=list, repr=False)
    _log_file: str | None = field(default=None, repr=False)
    _status: str = field(default="started", repr=False)
    #: (server, eval_id) once a ctl server for this ``log_dir`` is found;
    #: ``False`` once we've decided none exists (stop rescanning every tick).
    _ctl: tuple[DiscoveredControlServer, str] | None | bool = field(
        default=None, repr=False
    )

    # -- construction -----------------------------------------------------

    @classmethod
    def attach(cls, log_dir: str, *, session_dir: str | None = None) -> Self:
        """Attach to ``log_dir`` and start the poll/display loop.

        Relative ``log_dir`` resolves against ``session_dir`` (the ``bash``
        tool's cwd, M1-HYBRID §bash) so ``wb.attach("runs/r1")`` matches
        ``bash("inspect eval … --log-dir runs/r1")``. Falls back to cwd
        while ``session_dir`` isn't wired yet.
        """
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(session_dir or os.getcwd(), log_dir)
        h = cls(log_dir=log_dir, description=log_dir)
        h._dh = display(h, display_id=h.id)
        h._watcher = asyncio.create_task(h._watch())
        return h

    # -- properties -------------------------------------------------------

    @property
    def n_done(self) -> int:
        return sum(1 for r in self.rows.values() if r.status == "done")

    @property
    def running_ids(self) -> list[str]:
        return [r.id for r in self._running]

    @property
    def location(self) -> str | None:
        return self._log_file

    @property
    def audits(self) -> Any:
        """``audits_df`` over this run's ``.eval`` (post-``wait()``)."""
        if not self.finished:
            raise RuntimeError(
                f"{self.task_name or self.log_dir}: .audits only valid after "
                f"`await handle.wait()` "
                f"({self.n_done}/{self.total} done, {len(self._running)} running)"
            )
        from inspect_petri import audits_df  # noqa: PLC0415

        return audits_df(self.location or self.log_dir)

    # -- watch loop (no _task) --------------------------------------------

    async def _watch(self) -> None:  # type: ignore[override]
        last: Any = None
        while self._status == "started":
            await self._poll()
            sig = self._signature()
            if sig != last:
                last = sig
                self._update()
            if self._status != "started":
                break
            await asyncio.sleep(self._poll_interval)
        # final poll after settle (last flush may land after the header does)
        await self._poll()
        self.finished = True
        if self._status == "cancelled":
            self.error = "cancelled"
        elif self._status == "error":
            self.error = "error"
        self._running = []
        self._update()

    # -- hooks ------------------------------------------------------------

    async def _poll(self) -> None:
        # done rows + status/total from the .eval on disk
        if self._log_file is None:
            self._log_file = next((i.name for i in list_eval_logs(self.log_dir)), None)
        if self._log_file is not None:
            try:
                summaries = await read_eval_log_sample_summaries_async(self._log_file)
                header = await read_eval_log_async(self._log_file, header_only=True)
            except (zipfile.BadZipFile, ValueError):
                pass
            else:
                for s in summaries:
                    self.rows[f"{s.id}#{s.epoch}"] = self._row(s)
                self._status = header.status
                self.task_name = self.task_name or header.eval.task
                if not self.total:
                    n = header.eval.dataset.samples or 0
                    self.total = n * (header.eval.config.epochs or 1)
        # running rows from the eval's ctl server (if one exists)
        self._running = await self._poll_ctl()

    async def _poll_ctl(self) -> list[SampleRow]:
        """In-flight rows via ``GET /evals/{id}/samples`` on the eval's ctl server.

        Discovery: scan ``list_discovered_servers()`` and, for each, ``GET
        /evals`` to find the one whose ``log_location`` sits under our
        ``log_dir``. Cached once found; re-tried while ``None``; abandoned
        (``False``) once the log itself has settled without a match.
        """
        if self._ctl is None:
            self._ctl = await self._discover_ctl()
            if self._ctl is None and self._status != "started":
                self._ctl = False
        if not self._ctl:
            return []
        server, eval_id = self._ctl
        samples = await _ctl_get(server, f"/evals/{eval_id}/samples")
        if samples is None:
            # server gone — the process exited. Log-only from here.
            self._ctl = False
            return []
        rows: list[SampleRow] = []
        for s in samples:
            if s.get("status") != "running":
                continue
            key = f"{s['sample_id']}#{s.get('epoch', 1)}"
            if key in self.rows:
                continue
            mc = s.get("message_count") or 0
            rows.append(
                SampleRow(
                    id=str(s["sample_id"]),
                    status="running",
                    epoch=int(s.get("epoch", 1)),
                    turns=mc // 2,
                )
            )
        return rows

    async def _discover_ctl(self) -> tuple[DiscoveredControlServer, str] | None:
        log_dir = os.path.realpath(self.log_dir)
        for server in list_discovered_servers():
            evals = await _ctl_get(server, "/evals")
            if not evals:
                continue
            for e in evals:
                loc = e.get("log_location") or ""
                # ``log_location`` is the ``.eval`` file path; match on its dir.
                if loc and os.path.realpath(os.path.dirname(loc)) == log_dir:
                    return server, str(e["eval_id"])
        return None

    def _signature(self) -> tuple[Any, ...]:
        return (
            len(self.rows),
            self.n_done,
            self._status,
            tuple((r.id, r.turns) for r in self._running),
        )

    def _row(self, s: EvalSampleSummary) -> SampleRow:
        turns = (s.metadata or {}).get("turns")
        if turns is None and s.message_count is not None:
            turns = s.message_count // 2
        return SampleRow(
            id=str(s.id),
            status="error" if s.error else "done",
            epoch=s.epoch,
            input=str(s.input)[:80],
            turns=turns,
            scores={k: _finite_or_none(v.value) for k, v in (s.scores or {}).items()},
            error=s.error,
        )

    # -- repr -------------------------------------------------------------

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        state = "done" if self.finished else "running"
        if self.error:
            state = self.error
        wb: dict[str, Any] = {
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
        }
        if self.finished:
            wb["scores"] = [_first_numeric(r.scores) for r in self.rows.values()]
        return {
            "text/plain": (
                f"<AttachedRun {self.task_name or '?'} · "
                f"{self.n_done}/{self.total} · {state}>"
            ),
            WB_MIME: wb,
        }


# -- ctl HTTP over UDS --------------------------------------------------------


async def _ctl_get(server: DiscoveredControlServer, path: str) -> list[Any] | None:
    """One ``GET`` against a control server's AF_UNIX socket.

    Returns the decoded JSON list on success; ``None`` on any transport
    error, timeout, non-2xx, or non-list body — the caller treats that as
    "server not (yet) reachable" and either retries next tick or drops to
    log-only mode.
    """
    transport = httpx.AsyncHTTPTransport(uds=str(server.socket_path))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", timeout=1.0
        ) as client:
            r = await client.get(path)
            r.raise_for_status()
            body = r.json()
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return body if isinstance(body, list) else None
