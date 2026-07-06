"""M1 hybrid tool surface (M1-HYBRID.md §Tool surface, migration step 2).

Seven inspect ``Tool`` s the orchestrator agent gets alongside ``python``:

- ``bash`` — subprocess shell in the per-orchestrator ``session_dir``.
  stdout streams to the frontend as ``DisplayEvent`` s on the same pipe as
  ``python`` cell output; lines matching ``^{"wb":…}`` are parsed and
  emitted as WB_MIME cards (so ``INSPECT_DISPLAY=workbench`` progress lands
  as a live ``ProgressCard``, not text).
- ``read_file`` / ``write_file`` / ``edit_file`` — resolved relative to
  ``session_dir``; plain string returns, no display side-effects.
- ``ask_human`` / ``review_seeds`` / ``review_finding`` — thin wrappers
  around ``orch.gate(proposal)`` (same ``Gate`` primitive as in-cell
  ``wb.ask_human`` etc., just exposed at the tool level).

All eight are closures over the ``Orchestrator`` (for ``.kernel`` and
``.span_id``); ``make_tools(orch)`` returns the configured list.
Registration on ``orchestrator_agent`` is migration step 4.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from asyncio.subprocess import PIPE, STDOUT
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from inspect_ai.tool import Tool, tool
from inspect_ai.tool._tools._execute import code_viewer

from workbench.m1 import proposals
from workbench.m1.handles import _first_numeric
from workbench.m1.proposals import Prompt
from workbench.m1.wire import (
    STREAM_MIME,
    BgDonePayload,
    DisplayEvent,
    EvalRunPayload,
    SampleRowPayload,
    wb_bundle,
)

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator

_MODEL_TEXT_CAP = 4000

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


def make_tools(orch: Orchestrator) -> list[Tool]:  # noqa: PLR0915
    """The 7 non-``python`` hybrid tools, each a closure over ``orch``:
    ``bash`` / ``read_file`` / ``write_file`` / ``edit_file`` /
    ``ask_human`` / ``review_seeds`` / ``review_finding``. The 8th tool,
    ``python``, lives in ``orchestrator.python_tool``."""
    kernel = orch.kernel
    session_dir = orch.session_dir

    # -- bash -----------------------------------------------------------------

    def _stream(line: str) -> None:
        kernel.emit(
            DisplayEvent(
                id=uuid4().hex,
                bundle={STREAM_MIME: {"name": "stdout", "text": line}},
            )
        )

    #: Accumulated ``EvalRunPayload`` per ``eval_id`` — each ``{"wb":"eval_*"}``
    #: line folds into this and re-emits the *whole* snapshot, so the
    #: frontend's ``ProgressCard`` sees the same ``kind:"eval_run"`` shape as
    #: ``AttachedRun`` produces (M1-HYBRID.md step 5: one payload shape).
    _evals: dict[str, EvalRunPayload] = {}

    def _fold_eval(line: dict[str, Any]) -> EvalRunPayload:
        """Fold one ``eval_*`` protocol line into its accumulated snapshot."""
        eid = line["eval_id"]
        p = _evals.setdefault(
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
            p["scores"] = [_first_numeric(r.get("scores") or {}) for r in rows["done"]]
            if line.get("errors") and not rows["done"]:
                p["error"] = f"{line['errors']} errors"
        return p

    def _card(line: dict[str, Any], seen: set[str]) -> None:
        """One ``{"wb":…}`` line → a WB_MIME ``DisplayEvent``.

        ``eval_*`` lines are folded into a per-``eval_id`` ``RunPayload``
        snapshot (``kind:"eval_run"``) so successive updates replace the
        same stable card. Non-eval lines (``file``/``ref``/``bg_done``)
        pass through under a fresh id with ``kind = wb``.
        """
        wb = str(line["wb"])
        if wb.startswith("eval_"):
            payload = _fold_eval(line)
            did = line["eval_id"]
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
        proc: asyncio.subprocess.Process, plain: list[str], seen: set[str]
    ) -> int:
        """Read ``proc.stdout`` to EOF, routing each line to stream or card."""
        assert proc.stdout is not None
        while raw := await proc.stdout.readline():
            line = _ANSI_RE.sub("", raw.decode(errors="replace"))
            if line.startswith('{"wb":'):
                try:
                    _card(json.loads(line), seen)
                    continue
                except (json.JSONDecodeError, KeyError):
                    pass  # not valid protocol — fall through to plain stream
            plain.append(line)
            _stream(line)
        return await proc.wait()

    def _tail(plain: list[str], code: int | str) -> str:
        text = "".join(plain)
        if len(text) > _MODEL_TEXT_CAP:
            text = f"[… {len(text) - _MODEL_TEXT_CAP} bytes elided …]\n" + text[-_MODEL_TEXT_CAP:]
        return f"{text.rstrip()}\n[exit {code}]" if text.strip() else f"[exit {code}]"

    @tool(viewer=code_viewer("bash", "cmd"))
    def bash() -> Tool:
        async def execute(
            cmd: str, timeout: int = 300, background: bool = False
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
                proc = await asyncio.create_subprocess_shell(
                    cmd, cwd=session_dir, env=env, stdout=PIPE, stderr=STDOUT
                )
                plain: list[str] = []
                seen: set[str] = set()

                if background:
                    bg_id = uuid4().hex[:6]

                    async def _bg() -> None:
                        code = await _pump(proc, plain, seen)
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
                        kernel.notify(f"[bg-{bg_id} done · exit {code} · {cmd[:60]!r}]")

                    # ``_bg`` copies context at creation, so the ``_turn``
                    # contextvar carries into the detached pump even though
                    # the with-block exits immediately below.
                    asyncio.create_task(_bg())  # noqa: RUF006
                    return f"[bg-{bg_id} started · pid {proc.pid}]"

                try:
                    code = await asyncio.wait_for(_pump(proc, plain, seen), timeout)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return _tail(plain, f"timeout after {timeout}s")
                return _tail(plain, code)

        return execute

    # -- file tools -----------------------------------------------------------

    def _resolve(path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else session_dir / p

    @tool
    def read_file() -> Tool:
        async def execute(path: str, offset: int = 0, limit: int = 2000) -> str:
            """Read a file as numbered lines.

            Args:
                path: Path (relative to the session directory, or absolute).
                offset: 0-based line to start from.
                limit: Maximum number of lines to return.
            """
            p = _resolve(path)
            if not p.exists():
                return f"[error: {path} not found]"
            lines = p.read_text().splitlines()
            end = offset + limit
            body = "\n".join(
                f"{i + 1:6d}\t{line}" for i, line in enumerate(lines[offset:end], offset)
            )
            more = f"\n[… {len(lines) - end} more lines]" if len(lines) > end else ""
            return body + more if body else "[empty file]"

        return execute

    @tool
    def write_file() -> Tool:
        async def execute(path: str, content: str) -> str:
            """Write ``content`` to ``path`` (overwriting).

            Args:
                path: Path (relative to the session directory, or absolute).
                content: File contents.
            """
            p = _resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"[wrote {len(content.encode())} bytes → {path}]"

        return execute

    @tool
    def edit_file() -> Tool:
        async def execute(path: str, old: str, new: str) -> str:
            """Replace one exact occurrence of ``old`` with ``new`` in ``path``.

            Args:
                path: Path (relative to the session directory, or absolute).
                old: Exact substring to replace (must appear exactly once).
                new: Replacement text.
            """
            p = _resolve(path)
            if not p.exists():
                return f"[error: {path} not found]"
            src = p.read_text()
            n = src.count(old)
            if n == 0:
                return f"[error: {old!r} not found in {path}]"
            if n > 1:
                return f"[error: {old!r} appears {n} times in {path} — be more specific]"
            dst = src.replace(old, new, 1)
            p.write_text(dst)
            diff = "".join(
                difflib.unified_diff(
                    src.splitlines(keepends=True),
                    dst.splitlines(keepends=True),
                    fromfile=path,
                    tofile=path,
                    n=2,
                )
            )
            return diff or f"[edited {path} · no visible diff]"

        return execute

    # -- review tools (thin gate wrappers) ------------------------------------

    @tool
    def ask_human() -> Tool:
        async def execute(question: str, options: list[str] | None = None) -> str:
            """Ask the human operator a question and block until answered.

            Args:
                question: The question to render on the gate card.
                options: Optional fixed choices (rendered as buttons).
            """
            with _turn(orch):
                return str(await orch.gate(Prompt(question, options)))

        return execute

    @tool
    def review_seeds() -> Tool:
        async def execute(
            seeds: list[str], description: str, config: dict[str, Any] | None = None
        ) -> str:
            """Propose a seed list for human approval before launching a run.

            The human may strike seeds or deny outright. Returns the
            (possibly-trimmed) seed list and approval state; only proceed
            with the run if ``approved``.

            Args:
                seeds: Seed instructions to run.
                description: One-line rationale for the run.
                config: Run config (``model``, ``max_turns``, ``n_per_seed``, …).
            """
            with _turn(orch):
                result = await proposals.review_seeds(
                    orch.gate, seeds, description, config
                )
            # inspect's ``ToolResult`` doesn't include ``dict`` — encode.
            return json.dumps(result)

        return execute

    @tool
    def review_finding() -> Tool:
        async def execute(
            claim: str, quotes: list[dict[str, Any]], description: str
        ) -> str:
            """Propose a finding for the human to sign off on.

            Args:
                claim: The claim being made.
                quotes: Supporting transcript excerpts
                    (each ``{"sample_id","at","role","text"}``).
                description: Context for the reviewer.
            """
            with _turn(orch):
                finding = await proposals.cite(
                    orch.gate, claim, quotes, description=description
                )
            return json.dumps({
                "signed": finding.signed_by is not None,
                "by": finding.signed_by,
                "claim": finding.claim,
                "quotes": [vars(q) for q in finding.quotes],
            })

        return execute

    return [
        bash(),
        read_file(),
        write_file(),
        edit_file(),
        ask_human(),
        review_seeds(),
        review_finding(),
    ]
