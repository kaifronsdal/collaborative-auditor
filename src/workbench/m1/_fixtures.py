"""Shared fixtures for the M1 smoke tests (M1-REFACTOR.md Batch H).

The eight-line ``Session()/.start()/FakeConn/.connections.append/
.start_orchestrator(mockllm, custom_outputs=…)`` bootstrap and the
``sum(role=="assistant")``-indexed mockllm script appeared verbatim in six
smokes; the poll-for-``gate.pending`` loop and the ``for _ in range(N):
await sleep(0)`` settle in a further half-dozen inline copies. Collapsing
them here also lets us fix the one shared footgun centrally:
``Orchestrator.__init__`` does ``os.chdir(session_dir)``, so any smoke that
``rmtree``'d its session dir on the way out left the process cwd pointing at
a deleted directory — the next ``os.getcwd()`` (tempfile, pathlib, pytest
teardown) raised ``FileNotFoundError``. ``mock_orch_session`` snapshots and
restores cwd around the whole thing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeAlias

from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from workbench._smoke_util import FakeConn
from workbench.m1.orchestrator import ORCH_SOURCE
from workbench.m1.prompt import AUDIT_TASK
from workbench.m1.wire import WB_MIME
from workbench.session import Session

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator
    from workbench.m1.proposals import Gate

__all__ = [
    "AUDIT_TASK",
    "HYBRID_CORE_TURNS",
    "FakeConn",
    "TurnSpec",
    "demo_eval_cmd",
    "mock_orch_session",
    "orch_by_turn",
    "orch_events",
    "settle",
    "tool_call",
    "wait_for",
    "wait_gate",
    "wb_events",
]

#: One scripted assistant turn: prose + zero-or-more tool calls.
TurnSpec: TypeAlias = tuple[str, list[tuple[str, dict[str, Any]]]]

#: mockllm ``custom_outputs`` signature (kwargs form).
CustomOutputs: TypeAlias = Callable[..., ModelOutput]


# ── scripted-model helpers ──────────────────────────────────────────────────


def tool_call(prose: str, calls: list[tuple[str, dict[str, Any]]]) -> ModelOutput:
    """A ``ModelOutput`` with assistant ``prose`` and the given tool calls.

    Generalises the per-smoke ``_python(code)`` / ``_call(prose, calls)`` /
    ``_tool_call(fn, **args)`` builders.
    """
    out = ModelOutput.from_content(model="mockllm", content=prose)
    out.choices[0].message.tool_calls = [
        ToolCall(id=f"c{i}", function=fn, type="function", arguments=args)
        for i, (fn, args) in enumerate(calls)
    ]
    return out


def orch_by_turn(turns: list[ModelOutput | TurnSpec]) -> CustomOutputs:
    """mockllm ``custom_outputs`` indexed by prior-assistant count.

    ``turns[n]`` is returned on the ``n``-th generate; a bare ``TurnSpec``
    tuple is wrapped via :func:`tool_call`. Past the end of the script the
    model emits a no-tool ``"done."`` so the orchestrator loop parks.
    """
    outs = [t if isinstance(t, ModelOutput) else tool_call(*t) for t in turns]
    done = ModelOutput.from_content(model="mockllm", content="done.")

    def outputs(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        n = sum(1 for m in input if m.role == "assistant")
        return outs[n] if n < len(outs) else done

    return outputs


@asynccontextmanager
async def mock_orch_session(
    turns: list[ModelOutput | TurnSpec] | CustomOutputs,
    *,
    max_turns: int | None = None,
    model_args: dict[str, Any] | None = None,
    **orch_kw: Any,
) -> AsyncIterator[tuple[Session, "Orchestrator", FakeConn]]:
    """The ``Session → FakeConn → start_orchestrator(mockllm)`` bootstrap.

    ``turns`` is either a scripted turn list (fed through :func:`orch_by_turn`)
    or a ready-made ``custom_outputs`` callable for tests that need to inspect
    the ``tools``/``input`` arguments. ``model_args`` are merged over
    ``{"custom_outputs": …}`` (e.g. ``stream_chunks=4``); remaining ``orch_kw``
    forward to ``Session.start_orchestrator`` (``span_id``, ``system_prompt``).

    Snapshots ``os.getcwd()`` before ``Orchestrator.__init__`` chdirs into
    ``session_dir`` and restores it in ``finally`` — otherwise a smoke that
    ``rmtree``'s its session dir leaves the process cwd deleted.
    """
    orig_cwd = os.getcwd()
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    custom = turns if callable(turns) else orch_by_turn(turns)
    if max_turns is None:
        max_turns = 20 if callable(turns) else len(turns) + 4
    await session.start_orchestrator(
        model="mockllm/model",
        model_args={"custom_outputs": custom, **(model_args or {})},
        max_turns=max_turns,
        **orch_kw,
    )
    orch = session.orchestrator
    assert orch is not None
    try:
        yield session, orch, conn
    finally:
        await session.close()
        os.chdir(orig_cwd)


# ── polling / settling ──────────────────────────────────────────────────────


async def settle(n: int = 100) -> None:
    """Yield to the loop ``n`` times so in-flight mockllm turns quiesce."""
    for _ in range(n):
        await asyncio.sleep(0)


async def wait_for(
    pred: Callable[[], object], *, timeout: float = 3.0, tick: float = 0.01
) -> None:
    """Poll ``pred()`` until truthy; raise ``TimeoutError`` after ``timeout``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out after {timeout}s waiting for {pred}")
        await asyncio.sleep(tick)


async def wait_gate(gate: "Gate", *, timeout: float = 2.0) -> str:
    """Poll until ``gate.pending`` is non-empty; return the one pending id.

    Collapses the six inline ``for _ in range(50): await sleep(0); if
    gate.pending: break`` copies. Asserts exactly one pending id — a
    multi-gate test (``asyncio.gather(wb.ask_human, wb.ask_human)``) should
    poll ``gate.pending`` directly.
    """
    await wait_for(lambda: gate.pending, timeout=timeout, tick=0)
    (pid,) = gate.pending
    return pid


# ── event filters ───────────────────────────────────────────────────────────


def orch_events(session: Session) -> list[dict[str, Any]]:
    """Every orchestrator ``InfoEvent`` (dumped) in ``session.events``."""
    return [
        e
        for e in session.events.values()
        if e["event"] == "info" and e.get("source") == ORCH_SOURCE
    ]


def wb_events(session: Session) -> list[dict[str, Any]]:
    """Every orchestrator ``InfoEvent.data`` carrying a ``WB_MIME`` bundle."""
    return [
        e["data"]
        for e in orch_events(session)
        if WB_MIME in (e["data"].get("bundle") or {})
    ]


# ── subprocess-eval turn scripts ────────────────────────────────────────────


def demo_eval_cmd(
    *,
    n: int = 3,
    turns: int = 3,
    turn_sleep: float = 0.5,
    log_dir: str,
    acp: bool = False,
    **flags: Any,
) -> str:
    """Build the ``inspect eval …@demo …`` shell command for the ``bash`` tool.

    ``@demo`` is the mockllm-friendly task in ``m1/_audit_task.py`` — the real
    ``@audit`` task rejects mockllm output before any sample completes. Extra
    ``flags`` render as ``--flag-name value`` (underscores → dashes).
    """
    parts = [
        f"{sys.executable} -m inspect_ai eval {AUDIT_TASK}@demo",
        f"-T n={n} -T turns={turns} -T turn_sleep={turn_sleep}",
        f"--model mockllm/model --log-dir {log_dir} --log-buffer 1",
    ]
    if acp:
        parts.append("--acp-server")
    for k, v in flags.items():
        parts.append(f"--{k.replace('_', '-')} {v}")
    return " ".join(parts)


def HYBRID_CORE_TURNS(  # noqa: N802
    n: int, log_dir: str, **eval_flags: Any
) -> list[TurnSpec]:
    """The ``write_file → bash(inspect eval @demo) → python(wb.attach)`` triple.

    The canonical M1-HYBRID happy path, shared between the hybrid e2e smoke
    and the screenshot script (which splices ``[1:]`` — bash + attach — into
    its longer turn list). ``eval_flags`` forward to :func:`demo_eval_cmd`.
    """
    cmd = demo_eval_cmd(n=n, log_dir=log_dir, **eval_flags)
    attach = f"h = wb.attach({log_dir!r})\nawait h.wait()\nh"
    return [
        (
            "Writing seeds.",
            [("write_file", {"path": "seeds.json", "content": '["a","b","c"]'})],
        ),
        (
            f"Launching a {n}-sample eval; the card ticks live.",
            [("bash", {"cmd": cmd, "timeout": 120})],
        ),
        (
            "Attaching to the log directory for analysis.",
            [("python", {"code": attach})],
        ),
    ]
