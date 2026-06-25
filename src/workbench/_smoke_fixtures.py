"""Shared infrastructure for the wire-level e2e suite.

Hoists the deterministic-mockllm helpers (`_tc`, `_auditor_turn`, `_target`)
and the backend/vite context managers (`_free_port`, `_backend`, `_vite`)
that originated in `_smoke_ui_rollback.py`, plus the new fixtures the
`_smoke_actions_wire` suite needs:

- `auditor_by_turn(script)` / `target_by_last_user(table)` — stateless
  mockllm callables that pick their reply from the *input* (assistant count
  / last user text), so a fork that replays a prefix and then goes live at
  turn N gets `script[N]` regardless of how many earlier turns were served
  from `pending`.
- `target_counted(table)` / `auditor_counted(script, alt)` — stateful
  variants whose closure is shared across `Branch.fork()` (which inherits
  `*_model_args` verbatim), so a resample's regenerated turn is observably
  different from the original.
- `normalize(session, branch_id)` — flatten a branch's settled ModelEvents
  into a comparable `list[(role, text, ((fn, frozenset(args.items())), …))]`.
- `make_base(...)` / `run_child(...)` — build+run a parent branch, then
  release a `_dispatch`-spawned child and await it.

The UI smoke tests (`_smoke_ui_rollback.py`, `_smoke_ui_content.py`,
`_smoke_ui_actions.py`) will be refactored to import from here in a follow-up.
"""

from __future__ import annotations

import asyncio
import copy
import os
import socket
import subprocess
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import anyio
import uvicorn
from inspect_ai.model import (
    ChatMessage,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from workbench.run import GEN_SOURCE, Branch
from workbench.server import _dispatch, app  # noqa: PLC2701
from workbench.session import Session

REPO = Path(__file__).resolve().parents[2]

# mockllm `custom_outputs` callable signature.
CustomOut = Callable[
    [list[ChatMessage], list[ToolInfo], ToolChoice, GenerateConfig], ModelOutput
]

# ── scripted model outputs (hoisted from _smoke_ui_rollback.py) ─────────────

_n = 0


def _tc(function: str, **arguments: Any) -> ToolCall:
    global _n
    _n += 1
    return ToolCall(id=f"c{_n}", function=function, type="function", arguments=arguments)


def _auditor_turn(*calls: ToolCall) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content="")
    out.choices[0].message.tool_calls = list(calls)
    return out


def _target(content: str) -> ModelOutput:
    return ModelOutput.from_content(model="mockllm", content=content)


# ── infra (hoisted from _smoke_ui_rollback.py) ──────────────────────────────


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _backend(port: int):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    try:
        for _ in range(50):
            if srv.started:
                break
            await anyio.sleep(0.1)
        assert srv.started, "uvicorn failed to start"
        yield
    finally:
        srv.should_exit = True
        await task


@asynccontextmanager
async def _vite(ws_port: int, ui_port: int):
    """Start the Vite dev server pointing its WS at our in-process backend."""
    env = {**os.environ, "VITE_WS_URL": f"ws://127.0.0.1:{ws_port}"}
    proc = subprocess.Popen(  # noqa: S603
        [
            "npx", "--yes", "pnpm@10.29.3", "dev",
            "--host", "127.0.0.1", "--port", str(ui_port), "--strictPort",
        ],
        cwd=REPO / "frontend-wb",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(100):
            try:
                with closing(socket.create_connection(("127.0.0.1", ui_port), 0.2)):
                    break
            except OSError:
                await anyio.sleep(0.2)
        else:
            out = proc.stdout.read().decode() if proc.stdout else ""
            raise RuntimeError(f"vite failed to start on :{ui_port}\n{out}")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── mockllm callables ───────────────────────────────────────────────────────


def auditor_by_turn(script: list[ModelOutput]) -> CustomOut:
    """Return `deepcopy(script[k])` where k = #assistant messages in input.

    `tape.replayable` short-circuits replayed turns before reaching the
    model, so this is only called for *live* turns; the assistant count in
    `input` is the live turn index regardless of how many turns replayed.
    """

    def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        return copy.deepcopy(script[k])

    return _out


def target_by_last_user(table: dict[str, str | ModelOutput]) -> CustomOut:
    """Map the last user/tool message's text → reply (str wrapped via `_target`)."""

    def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        last = next(m for m in reversed(input) if m.role in ("user", "tool"))
        v = table[last.text]
        return copy.deepcopy(v) if isinstance(v, ModelOutput) else _target(v)

    return _out


def target_counted(table: dict[str, list[str]]) -> CustomOut:
    """Stateful: nth call for a given last-user text returns `table[text][n]`
    (clamped). The closure persists across `Branch.fork()` so a resampled
    target reply is observably different from the parent's."""
    seen: dict[str, int] = {}

    def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        last = next(m for m in reversed(input) if m.role in ("user", "tool"))
        n = seen.get(last.text, 0)
        seen[last.text] = n + 1
        seq = table[last.text]
        return _target(seq[min(n, len(seq) - 1)])

    return _out


def auditor_counted(script: list[ModelOutput], alt: dict[int, ModelOutput]) -> CustomOut:
    """Stateful: first live call at turn k returns `script[k]`; subsequent
    calls at turn k return `alt[k]` (falling back to `script[k]`). Closure
    is shared across forks so `resample_auditor` regenerates differently."""
    seen: dict[int, int] = {}

    def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        n = seen.get(k, 0)
        seen[k] = n + 1
        out = script[k] if n == 0 else alt.get(k, script[k])
        return copy.deepcopy(out)

    return _out


# ── normalize ───────────────────────────────────────────────────────────────


def _scalar_args(arguments: dict[str, Any]) -> frozenset[tuple[str, Any]]:
    return frozenset(
        (k, v)
        for k, v in arguments.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    )


def _project(ev: dict[str, Any], role: str) -> tuple[str, str, tuple]:
    msg = ev["output"]["choices"][0]["message"]
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    else:
        text = ""
    calls = tuple(
        (tc["function"], _scalar_args(tc.get("arguments") or {}))
        for tc in (msg.get("tool_calls") or [])
    )
    return (role, text, calls)


def _model_events(session: Session, bid: str, role: str) -> list[dict[str, Any]]:
    """`(bid, role)`'s own ModelEvents in emission order, via `_by_role`."""
    out: list[dict[str, Any]] = []
    for uuid in session._by_role.get((bid, role), []):  # noqa: SLF001
        ev = session.events.get(uuid)
        if ev is not None and ev.get("event") == "model":
            out.append(ev)
    return out


def _shared_auditor_turns(b: Branch) -> int:
    """#auditor generates in `b`'s shared-with-parent prefix — i.e. the turns
    `_on_event` dropped while `_replaying_shared`. NOT `meta.branched_at_turn`,
    which counts the *full* `resume` and so over-counts by one for the
    `edit_*` ops that append a divergent edited step."""
    if b.resume is None:
        return 0
    return sum(
        1
        for s in b.resume[: b.shared_prefix_len]
        if s.source == GEN_SOURCE and isinstance(s.value, ModelOutput)
    )


def _auditor_lineage(session: Session, branch_id: str) -> list[dict[str, Any]]:
    """`branch_id`'s full spliced auditor-ModelEvent list, root → leaf.

    Walks `BranchMeta.parent` to the root, then replays the splice forward:
    at each link, truncate the accumulated lineage to the child's shared
    auditor-turn count (what was dropped) and append the child's own
    post-shared auditor events. Mirrors the frontend `splice()`."""
    chain: list[str] = []
    bid: str | None = branch_id
    while bid is not None:
        chain.append(bid)
        bid = session.branches[bid].meta.parent
    chain.reverse()
    aud: list[dict[str, Any]] = []
    for cid in chain:
        aud = aud[: _shared_auditor_turns(session.branches[cid])]
        aud.extend(_model_events(session, cid, "auditor"))
    return aud


def normalize(session: Session, branch_id: str) -> list[tuple[str, str, tuple]]:
    """Flatten `branch_id`'s spliced lineage into a comparable list.

    Post splice-refactor, a child's `session.events` holds only its *own*
    events — target replays plus post-shared auditor turns; the shared
    auditor prefix lives on the parent and `_on_event` drops the child's
    duplicate. This rebuilds what the user sees: the spliced auditor lineage
    zipped turn-for-turn with this branch's target ModelEvents (kept in full
    by `_on_event`, replay included). Each entry is
    `(role, text, ((fn, frozenset(scalar_args)), …))`.

    The zip relies on the petri invariant that auditor turn *k* yields at
    most one target generate (the `resume` tool's reply) before turn *k+1*;
    a trailing `end_conversation` turn has none.
    """
    aud = _auditor_lineage(session, branch_id)
    tgt = _model_events(session, branch_id, "target")
    out: list[tuple[str, str, tuple]] = []
    for i in range(max(len(aud), len(tgt))):
        if i < len(aud):
            out.append(_project(aud[i], "auditor"))
        if i < len(tgt):
            out.append(_project(tgt[i], "target"))
    return out


# ── branch lifecycle helpers ────────────────────────────────────────────────


async def make_base(
    session: Session,
    *,
    auditor_outputs: list[ModelOutput] | CustomOut,
    target_outputs: list[ModelOutput] | CustomOut,
    max_turns: int,
    seed: str = "wire-suite seed",
    branch_id: str = "base",
) -> Branch:
    """Build + run a parent branch to completion. Registers it on `session`
    so a subsequent `_dispatch({"t": <fork-op>, "branch": branch_id, …})`
    finds it."""
    b = Branch(
        session,
        branch_id,
        seed=seed,
        auditor_model="mockllm/model",
        target_model="mockllm/model",
        max_turns=max_turns,
        auditor_model_args={"custom_outputs": auditor_outputs},
        target_model_args={"custom_outputs": target_outputs},
    )
    session.branches[branch_id] = b
    session.current = branch_id
    b.play()
    task = asyncio.create_task(b.run())
    session.branch_tasks.append(task)
    await task
    assert b.error is None, f"base branch failed: {b.error}"
    assert b.status == "ended", f"base branch status={b.status}"
    return b


async def run_child(session: Session) -> tuple[str, Branch]:
    """Release the most-recently-spawned child branch and await it.

    `_register_and_spawn` set `session.current` to the child and appended its
    `run()` task to `session.branch_tasks`. `{"t":"play"}` is idempotent if
    autoplay already fired; `await` returns immediately if the child already
    ended.
    """
    await _dispatch(session, {"t": "play"})
    assert session.branch_tasks, "no child task spawned"
    await session.branch_tasks[-1]
    assert session.current is not None
    child = session.branches[session.current]
    assert child.error is None, f"child branch failed: {child.error}"
    return session.current, child
