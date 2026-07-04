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
import json
import os
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Literal, Self

import httpx
from inspect_ai._control.discovery import (  # noqa: PLC2701
    DiscoveredControlServer,
    list_discovered_servers,
)
from inspect_ai.agent._acp.discovery import (  # noqa: PLC2701
    DiscoveredEval,
    list_discovered_evals,
)
from inspect_ai.log import EvalSampleSummary, list_eval_logs
from inspect_ai.log._file import (  # noqa: PLC2701
    read_eval_log_async,
    read_eval_log_sample_summaries_async,
)
from IPython.display import display

from workbench.m1.handles import (
    SampleRow,
    _finite_or_none,  # noqa: PLC2701
    _first_numeric,  # noqa: PLC2701
    _PollingHandle,  # noqa: PLC2701
)
from workbench.m1.kernel import WB_MIME


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
    #: ACP discovery entry for this eval (``--acp-server``); ``False`` once
    #: we've decided none exists. Same tri-state as ``_ctl``.
    _acp: DiscoveredEval | None | bool = field(default=None, repr=False)

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
        try:
            listing = await _acp_request(reader, writer, 1, "inspect/list_samples", {})
            session_id = next(
                (
                    s["sessionId"]
                    for s in listing.get("samples", [])
                    if str(s.get("sampleId")) == str(sample_id) and s.get("sessionId")
                ),
                None,
            )
            if session_id is None:
                return False
            await _acp_request(
                reader,
                writer,
                2,
                "session/load",
                {"sessionId": session_id, "cwd": "/", "mcpServers": []},
            )
            await _acp_request(
                reader,
                writer,
                3,
                "inspect/cancel_sample",
                {"sessionId": session_id, "action": action},
            )
            return True
        except (_AcpError, OSError):
            return False
        finally:
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


# -- ACP JSON-RPC over UDS ----------------------------------------------------


class _AcpError(Exception):
    """A JSON-RPC error response, timeout, or EOF on the ACP connection."""


async def _acp_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    req_id: int,
    method: str,
    params: dict[str, Any],
) -> Any:
    """One JSON-RPC 2.0 request/response over a newline-delimited stream.

    Inspect's ACP server (and the underlying ``acp.Connection``) frames
    messages as one JSON object per line — no LSP ``Content-Length:``
    headers. The server also pushes ``session/update`` notifications and
    (post-bind) transcript replay unsolicited, so read lines until the
    matching response arrives: a message with our ``id`` and no
    ``method`` field. Everything else (notifications have ``method`` but
    no ``id``; server→client requests have both) is skipped.
    """
    writer.write(
        (
            json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
            + "\n"
        ).encode()
    )
    await writer.drain()
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except asyncio.TimeoutError as exc:
            raise _AcpError(f"{method}: timeout") from exc
        if not line:
            raise _AcpError(f"{method}: connection closed")
        msg = json.loads(line)
        if "method" in msg or msg.get("id") != req_id:
            continue
        if "error" in msg:
            raise _AcpError(f"{method}: {msg['error']}")
        return msg.get("result", {})
