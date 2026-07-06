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
from typing import Any, Literal, Self, cast

import httpx
from acp import PROTOCOL_VERSION
from acp.connection import Connection
from acp.exceptions import RequestError
from acp.router import MessageRouter
from inspect_ai._control.discovery import (
    DiscoveredControlServer,
    list_discovered_servers,
)
from inspect_ai.agent._acp.discovery import (
    DiscoveredEval,
    list_discovered_evals,
)
from inspect_ai.log import EvalSampleSummary, list_eval_logs
from inspect_ai.log._file import (
    read_eval_log_async,
    read_eval_log_sample_summaries_async,
)
from IPython.display import display

from workbench.m1.handles import (
    SampleRow,
    _first_numeric,
    _PollingHandle,
)
from workbench.m1.wire import (
    EvalRunPayload,
    SampleRowPayload,
    _finite,
    wb_bundle,
)


@dataclass(kw_only=True)
class AttachedRun(_PollingHandle):
    """Read-only handle on an out-of-process eval writing to ``log_dir``.

    Polling only — no ``_task``. ``_done`` is ``_status != "started"``
    (read from the ``.eval`` header each poll); the base ``_watch`` loop
    and ``wait()`` work unchanged.
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
    #: ACP discovery entry for this eval (``--acp-server``); ``False`` once
    #: we've decided none exists. Same tri-state as ``_ctl``.
    _acp: DiscoveredEval | None | bool = field(default=None, repr=False)
    #: First-poll timestamp, for the "nothing ever appeared" early-bail.
    _t0: float = field(default_factory=time.monotonic, repr=False)
    #: Seconds to wait for *any* sign of life (a ``.eval`` file or a ctl
    #: server) before declaring the subprocess dead. e2e-v3: a crashed
    #: ``inspect eval`` left an empty ``log_dir`` and ``.wait()`` sat the
    #: full timeout.
    _grace: float = field(default=15.0, repr=False)

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
        from inspect_petri import audits_df

        return audits_df(self.location or self.log_dir)

    # -- hooks ------------------------------------------------------------

    def _done(self) -> bool:
        return self._status != "started"

    async def _settle(self, task: asyncio.Task[Any] | None) -> None:
        if self._status in ("cancelled", "error"):
            self.error = self.error or self._status
        self._running = []

    async def _poll(self) -> None:
        # done rows + status/total from the .eval on disk
        if self._log_file is None:
            self._log_file = next((i.name for i in list_eval_logs(self.log_dir)), None)
        if (
            self._log_file is None
            and not isinstance(self._ctl, tuple)
            and time.monotonic() - self._t0 > self._grace
        ):
            # No ``.eval`` on disk and no ctl server ever advertised this
            # ``log_dir`` — the subprocess almost certainly crashed before
            # writing its header. Bail now rather than sit the full timeout.
            self._status = "error"
            self.error = (
                f"no eval log appeared in {self.log_dir} after "
                f"{self._grace:g}s — subprocess may have crashed before "
                f"writing its header (check the bash output)"
            )
            return
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
        if not isinstance(self._ctl, tuple):
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

    # -- ACP (per-sample interrupt) ---------------------------------------

    async def _discover_acp(self) -> DiscoveredEval | None:
        """Find this eval's ``--acp-server`` socket via ACP discovery.

        The ACP discovery file's ``eval_id`` is the *run* id (what
        ``acp_server(eval_id=…)`` is called with — one server per
        ``inspect eval`` process), which is the same value ctl exposes
        as :attr:`DiscoveredControlServer.run_id`. So: reuse the ctl
        server we already found for this ``log_dir`` and match ACP
        entries on its ``run_id``. If ctl hasn't been discovered yet,
        try once now; without it there's no way to disambiguate
        concurrent evals, so return ``None``.
        """
        if not isinstance(self._ctl, tuple):
            self._ctl = await self._discover_ctl()
        if not isinstance(self._ctl, tuple):
            return None
        server, _ = self._ctl
        for entry in list_discovered_evals():
            if entry.eval_id == server.run_id:
                return entry
        return None

    async def interrupt_sample(
        self, sample_id: str, *, action: Literal["score", "error"] = "score"
    ) -> bool:
        """Cancel one running sample via ``inspect/cancel_sample`` over ACP.

        Opens a fresh connection to the eval's ACP UNIX socket, resolves
        ``sample_id`` → ``sessionId`` via ``inspect/list_samples``, binds
        with ``session/load`` (``inspect/cancel_sample`` requires a bound
        connection — the wire ``sessionId`` is validated against the
        binding), then issues the cancel. ``action="score"`` runs the
        scorer on whatever landed; the sample flushes to ``.eval`` under
        ``--log-buffer 1`` so ``import_eval`` can read it.

        Returns ``True`` on a successful cancel; ``False`` if no ACP
        server is discoverable (eval not started with ``--acp-server``,
        already exited, or the sample isn't running) — the caller falls
        back to whole-eval terminate or a plain log import.
        """
        if self._acp is None:
            self._acp = await self._discover_acp() or False
        if not isinstance(self._acp, DiscoveredEval):
            return False
        sock = self._acp.target.socket_path
        if sock is None:
            return False
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock))
        except OSError:
            self._acp = False
            return False
        # Bare ``MessageRouter`` — unsolicited ``session/update`` replay
        # notifications (post-``session/load``) route to nothing and are
        # dropped; we only need the request/response half.
        conn = Connection(handler=MessageRouter(), writer=writer, reader=reader)
        try:
            await conn.send_request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientInfo": {"name": "workbench-attach", "version": "1"},
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                },
            )
            listing: Any = await conn.send_request("inspect/list_samples", {})
            session_id = next(
                (
                    s["sessionId"]
                    for s in (listing or {}).get("samples", [])
                    if str(s.get("sampleId")) == str(sample_id) and s.get("sessionId")
                ),
                None,
            )
            if session_id is None:
                return False
            await conn.send_request(
                "session/load",
                {"sessionId": session_id, "cwd": "/", "mcpServers": []},
            )
            await conn.send_request(
                "inspect/cancel_sample",
                {"sessionId": session_id, "action": action},
            )
            return True
        except (RequestError, ConnectionError, OSError):
            return False
        finally:
            await conn.close()
            writer.close()
            try:  # noqa: SIM105
                await writer.wait_closed()
            except OSError:
                pass

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
            scores={k: _finite(v.value) for k, v in (s.scores or {}).items()},
            error=s.error,
        )

    # -- repr -------------------------------------------------------------

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        state = "done" if self.finished else "running"
        if self.error:
            state = self.error
        payload: EvalRunPayload = {
            "kind": "eval_run",
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
                "running": [cast("SampleRowPayload", vars(r)) for r in self._running],
                "done": [cast("SampleRowPayload", vars(r)) for r in self.rows.values()],
            },
        }
        if self.finished:
            payload["scores"] = [_first_numeric(r.scores) for r in self.rows.values()]
        return wb_bundle(
            f"<AttachedRun {self.task_name or '?'} · "
            f"{self.n_done}/{self.total} · {state}>",
            payload,
        )


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
