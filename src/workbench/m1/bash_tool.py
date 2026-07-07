"""M1 hybrid ``bash`` tool — subprocess shell with WB_MIME card streaming.

Extracted from ``tools.make_tools`` (Batch L2): the ~230-LOC bash pipeline
(``_stream``/``_fold_eval``/``_card``/``_pump``/``_tail``/``bash``) as one
``make_bash_tool(orch) -> Tool`` plus module-level helpers that take the
per-orchestrator state (``kernel``, ``evals`` accumulator) explicitly.
``tools.make_tools`` becomes the file/review-tool assembler and imports this.

stdout streams to the frontend as ``DisplayEvent`` s on the same pipe as
``python`` cell output; lines matching ``^{"wb":…}`` are parsed and emitted as
WB_MIME cards (so ``WORKBENCH_DISPLAY=1`` progress lands as a live
``ProgressCard``, not text).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from asyncio.subprocess import PIPE, STDOUT
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from inspect_ai.tool import Tool, tool
from inspect_ai.tool._tools._execute import code_viewer

from workbench.m1.handles import first_numeric
from workbench.m1.wire import (
    MODEL_TEXT_CAP,
    STREAM_MIME,
    BgDonePayload,
    DisplayEvent,
    EvalRunPayload,
    SampleRowPayload,
    wb_bundle,
)

if TYPE_CHECKING:
    from workbench.m1.kernel import OrchestratorKernel
    from workbench.m1.orchestrator import Orchestrator

#: Strip ANSI CSI/SGR sequences from subprocess output. ``TERM=dumb`` in the
#: bash env should suppress them at the source (rich, click, aisitools all
#: honour it), but anything that hard-codes escapes still leaks through — and
#: we render to HTML, not a terminal, so escapes are always noise here.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@contextmanager
def _turn(orch: Orchestrator) -> Iterator[int]:
    """Allocate a kernel turn for a non-``python`` tool that emits displays.

    ``kernel.emit`` stamps ``DisplayEvent.turn_id`` from the ``_current_turn``
    contextvar. ``bash`` and the review tools emit outside any cell, so
    without this their outputs land at ``turn_id=-1`` and the frontend can't
    group them under the tool call that produced them (nor can ``rewind()``
    find them). ``kernel.turn()`` bumps the shared counter so numbering stays
    contiguous across ``python`` and non-``python`` calls; ``record_turn``
    mirrors ``python_tool`` so rewind covers these turns too.
    """
    with orch.kernel.turn() as tid:
        orch.record_turn(tid)
        yield tid


# -- module-level pipeline helpers -------------------------------------------


def _stream(kernel: OrchestratorKernel, line: str) -> None:
    kernel.emit(
        DisplayEvent(
            id=uuid4().hex,
            bundle={STREAM_MIME: {"name": "stdout", "text": line}},
        )
    )


def _fold_eval(evals: dict[str, EvalRunPayload], line: dict[str, Any]) -> EvalRunPayload:
    """Fold one ``eval_*`` protocol line into its accumulated snapshot.

    ``evals`` is the per-orchestrator accumulator keyed by ``eval_id``; each
    ``{"wb":"eval_*"}`` line folds into it and re-emits the *whole* snapshot,
    so the frontend's ``ProgressCard`` sees the same ``kind:"eval_run"`` shape
    as ``AttachedRun`` produces (M1-HYBRID.md step 5: one payload shape).
    """
    eid = line["eval_id"]
    p = evals.setdefault(
        eid,
        {
            "kind": "eval_run",
            "id": eid,
            "task": line.get("task", "eval"),
            "description": "",
            "log_dir": line.get("log_dir", ""),
            "log": None,
            "total": line.get("total", 0),
            "done": 0,
            "finished": False,
            "error": None,
            "rows": {"running": [], "done": []},
        },
    )
    rows = p["rows"]
    wb = line["wb"]
    if wb == "eval_start":
        p["task"] = line["task"]
        p["total"] = line["total"]
        p["log_dir"] = line.get("log_dir", "")
        # ``location`` is known at start (``profile.log_location``) — fold
        # it now so a finished-row click can ``{t:"import"}`` before the
        # overall ``eval_done`` fires.
        p["log"] = line.get("location") or p["log"]
        p["description"] = str(line.get("model") or "")
    elif wb == "eval_progress":
        p["done"] = line["done"]
        p["elapsed"] = f"{line['elapsed']:.0f}s"
        # ``running`` rows have no ``input`` in the protocol — the
        # display driver only sees id/epoch/turns/tokens. Leave it
        # empty; the ``.ar-seed`` column just collapses.
        rows["running"] = [
            SampleRowPayload(
                id=str(r["id"]),
                status="running",
                input="",
                turns=r.get("turns"),
                scores={},
            )
            for r in line.get("running", [])
        ]
    elif wb == "eval_sample_done":
        sid = str(line["id"])
        rows["done"].append(
            SampleRowPayload(
                id=sid,
                status="error" if line.get("error") else "done",
                input="",
                turns=None,
                error=line.get("error"),
                scores=line.get("scores") or {},
            )
        )
        # Prune from ``running`` immediately so the sample doesn't render
        # in both lists for the tick before the next ``eval_progress``
        # (React duplicate-key warning; step-8 finding 2).
        rows["running"] = [r for r in rows["running"] if r["id"] != sid]
    elif wb == "eval_done":
        p["finished"] = True
        p["done"] = line["done"]
        p["log"] = line.get("location")
        rows["running"] = []
        # Match ``AttachedRun._repr_mimebundle_`` so ``ProgressCard``'s §8
        # histogram works for bash-driven runs too (Batch C convergence).
        p["scores"] = [first_numeric(r.get("scores") or {}) for r in rows["done"]]
        if line.get("errors") and not rows["done"]:
            p["error"] = f"{line['errors']} errors"
    return p


def _card(orch: Orchestrator, line: dict[str, Any], seen: set[str]) -> None:
    """One ``{"wb":…}`` line → a WB_MIME ``DisplayEvent``.

    ``eval_*`` lines are folded into a per-``eval_id`` ``EvalRunPayload``
    snapshot (``kind:"eval_run"``) so successive updates replace the
    same stable card. Non-eval lines (``file``/``ref``/``bg_done``)
    pass through under a fresh id with ``kind = wb``.

    Takes ``orch`` (not just ``kernel``) so an ``eval_start`` can register
    the run in ``run_log_dirs`` / ``_eval_started`` for the P2 bg-job panel
    and the kernel-restart note.
    """
    kernel = orch.kernel
    wb = str(line["wb"])
    if wb.startswith("eval_"):
        payload = _fold_eval(orch._bash_evals, line)  # noqa: SLF001
        did = line["eval_id"]
        if wb == "eval_start":
            orch._eval_started.setdefault(did, time.time())  # noqa: SLF001
            log_dir = payload.get("log_dir")
            if log_dir and log_dir not in orch.run_log_dirs:
                orch.run_log_dirs.append(log_dir)
    else:
        # Non-eval protocol lines (``file``/``ref``) aren't in ``WbPayload``
        # yet — pass through untyped so ``WbFallback`` renders the raw dict.
        payload = cast("EvalRunPayload", {"kind": wb, **line})
        did = line.get("id") or uuid4().hex
    update = did in seen
    seen.add(did)
    kernel.emit(
        DisplayEvent(
            id=did,
            bundle=wb_bundle(f"<{wb} · {json.dumps(line)[:80]}>", payload),
            stable=True,
            update=update,
        )
    )


async def _pump(
    orch: Orchestrator,
    proc: asyncio.subprocess.Process,
    plain: list[str],
    seen: set[str],
) -> int:
    """Read ``proc.stdout`` to EOF, routing each line to stream or card."""
    assert proc.stdout is not None
    while raw := await proc.stdout.readline():
        line = _ANSI_RE.sub("", raw.decode(errors="replace"))
        if line.startswith('{"wb":'):
            try:
                _card(orch, json.loads(line), seen)
                continue
            except (json.JSONDecodeError, KeyError):
                pass  # not valid protocol — fall through to plain stream
        plain.append(line)
        _stream(orch.kernel, line)
    return await proc.wait()


def _tail(plain: list[str], code: int | str) -> str:
    text = "".join(plain)
    cap = MODEL_TEXT_CAP()
    if len(text) > cap:
        text = f"[… {len(text) - cap} bytes elided …]\n" + text[-cap:]
    return f"{text.rstrip()}\n[exit {code}]" if text.strip() else f"[exit {code}]"


# -- the tool ----------------------------------------------------------------


def make_bash_tool(orch: Orchestrator) -> Tool:
    """The ``bash`` hybrid tool, closing over ``orch`` for kernel/session_dir.

    The per-orchestrator ``evals`` accumulator (``_fold_eval`` state) lives
    on ``orch._bash_evals`` so ``Orchestrator.view()`` can read it for the
    P2 bg-job panel; the pipeline helpers above take ``orch`` and read it
    from there.
    """
    kernel = orch.kernel
    session_dir = orch.session_dir
    # Read at orchestrator-start (not import) so ``PATCH /settings`` applies to
    # the next session. The literal shows in the tool signature the LLM sees.
    from workbench.config import settings

    default_timeout = settings.bash_timeout

    @tool(viewer=code_viewer("bash", "cmd"))
    def bash() -> Tool:
        async def execute(
            cmd: str, timeout: int = default_timeout, background: bool = False
        ) -> str:
            """Run a shell command in the orchestrator's session directory.

            stdout/stderr stream to the frontend as they arrive; lines
            starting ``{"wb":…}`` render as rich cards (eval progress,
            file refs). ``INSPECT_DISPLAY=workbench`` is set so any
            ``inspect eval`` inside ``cmd`` emits those lines automatically.

            Args:
                cmd: Shell command line.
                timeout: Seconds before the process is killed.
                background: If True, return immediately with a ``[bg-{id}]``
                    handle; a ``[bg-{id} done · exit N]`` note arrives later.
            """
            # ``INSPECT_DISPLAY=workbench`` can't work — CLI's ``--display``
            # is a click.Choice bound to that env var, so click rejects it
            # before Python runs. ``wb_display.register()`` (via the
            # ``inspect_ai`` entry point) monkeypatches the active display
            # when ``WORKBENCH_DISPLAY`` is set instead. ``NO_COLOR`` +
            # ``INSPECT_HOOKS_QUIET`` suppress the aisitools ANSI banner that
            # otherwise leaks into ``.out-stream`` via ``stderr=STDOUT``.
            env = {
                **os.environ,
                "WORKBENCH_DISPLAY": "1",
                # inspect's ``resolve_tasks`` chdir's into the *task file*'s
                # parent (loader.py:527) before calling ``@task audit(...)``,
                # so a relative ``-T seeds_file=seeds.json`` resolves in
                # ``src/workbench/m1/`` — not here where the model wrote it.
                # ``_audit_task.py`` reads this to re-anchor relative paths.
                "WORKBENCH_SESSION_DIR": str(session_dir),
                # Kill ANSI at the source — rich/click/aisitools all honour
                # ``TERM=dumb``; ``NO_COLOR`` alone doesn't (04b regression).
                # ``_ANSI_RE`` in ``_pump`` strips anything that slips through.
                "TERM": "dumb",
                "NO_COLOR": "1",
                "FORCE_COLOR": "0",
                "INSPECT_HOOKS_QUIET": "1",
            }
            with _turn(orch):
                # ``start_new_session=True`` → proc is its own process-group
                # leader, so ``Orchestrator.close()`` can ``os.killpg`` the
                # whole subtree (P0.5). Tracked on ``orch._bash_procs`` (keyed
                # by ``job_id`` — the ``bg-XXXXXX`` handle for background
                # procs) from spawn until ``proc.wait()`` returns; on a
                # cancelled fg await the proc stays tracked so ``close()``
                # reaps it. The entry carries ``cmd``/``started_at`` so
                # ``Orchestrator._bg_jobs()`` can render the P2 panel row.
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=session_dir,
                    env=env,
                    stdout=PIPE,
                    stderr=STDOUT,
                    start_new_session=True,
                )
                job_id = uuid4().hex[:6]
                orch._bash_procs[job_id] = {  # noqa: SLF001
                    "proc": proc,
                    "cmd": cmd,
                    "started_at": time.time(),
                    "background": background,
                }
                # ``bg_jobs`` only ships in ``{t:"state"}`` pushes — kick one so
                # the JobsPanel picks up the new proc without waiting for the
                # next gate/status change.
                orch._broadcast_status_soon()  # noqa: SLF001
                plain: list[str] = []
                seen: set[str] = set()

                if background:
                    bg_id = job_id

                    async def _bg() -> None:
                        try:
                            code = await _pump(orch, proc, plain, seen)
                            done: BgDonePayload = {
                                "kind": "bg_done",
                                "id": bg_id,
                                "pid": proc.pid,
                                "exit": code,
                            }
                            kernel.emit(
                                DisplayEvent(
                                    id=uuid4().hex,
                                    bundle=wb_bundle(
                                        f"[bg-{bg_id} done · exit {code}]", done
                                    ),
                                )
                            )
                            kernel.notify(
                                f"[bg-{bg_id} done · exit {code} · {cmd[:60]!r}]"
                            )
                        finally:
                            orch._bash_procs.pop(job_id, None)  # noqa: SLF001

                    # ``_bg`` copies context at creation, so the ``_turn``
                    # contextvar carries into the detached pump even though
                    # the with-block exits immediately below.
                    asyncio.create_task(_bg())  # noqa: RUF006
                    return f"[bg-{bg_id} started · pid {proc.pid}]"

                try:
                    code = await asyncio.wait_for(
                        _pump(orch, proc, plain, seen), timeout
                    )
                    orch._bash_procs.pop(job_id, None)  # noqa: SLF001
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    orch._bash_procs.pop(job_id, None)  # noqa: SLF001
                    return _tail(plain, f"timeout after {timeout}s")
                return _tail(plain, code)

        return execute

    return bash()
