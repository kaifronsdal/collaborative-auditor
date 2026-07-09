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
- `SCRIPT3` / `T0` / `T1` / `T2_END` — the canonical 3-turn deterministic
  base scenario (set_system + send u1 → r1; send u2 → r2; end).
- `_diff` / `_send` / `_pool_msg_id` / `_count_trajectories` /
  `_nth_target_anchor` / `run_suite` — small assertion + lookup helpers
  shared by every wire-level suite.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import subprocess
import traceback
from collections.abc import Callable, Coroutine, Sequence
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import uvicorn
from inspect_ai.model import (
    ChatMessage,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from workbench.run import Branch
from workbench.server import _dispatch, app
from workbench.session import Session
from workbench.sources import GEN_SOURCE, TARGET_GEN_SOURCE
from workbench.timeline import _walk

if TYPE_CHECKING:
    from playwright.async_api import Page

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


def _target_tool_call(
    fn: str, call_id: str, *, content: str = "", **args: Any
) -> ModelOutput:
    """Target-side ModelOutput carrying one ``tool_call`` (simulated tool use)."""
    out = ModelOutput.from_content(model="mockllm", content=content)
    out.choices[0].message.tool_calls = [
        ToolCall(id=call_id, function=fn, type="function", arguments=dict(args))
    ]
    return out


def _target_out(
    input: list[ChatMessage],  # noqa: A002
    tools: list[ToolInfo],
    tool_choice: ToolChoice,
    config: GenerateConfig,
) -> ModelOutput:
    """Target mockllm callable: echo the last user message as ``reply-to:{text}``."""
    del tools, tool_choice, config
    last_user = next(m for m in reversed(input) if m.role == "user")
    return ModelOutput.from_content(model="mockllm", content=f"reply-to:{last_user.text}")


# ── infra (hoisted from _smoke_ui_rollback.py) ──────────────────────────────


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _backend(port: int):
    # Isolate tests from the user's persisted sessions and from each other —
    # each `_backend` gets its own ephemeral store dir.
    import tempfile

    import workbench.config as cfg_mod
    import workbench.server as srv_mod

    with tempfile.TemporaryDirectory(prefix="wb-smoke-") as tmp:
        prev_root, prev_sess = cfg_mod.STORE_DIR, srv_mod.STORE_DIR
        cfg_mod.STORE_DIR = Path(tmp)
        srv_mod.STORE_DIR = cfg_mod.sessions_dir()
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
            cfg_mod.STORE_DIR, srv_mod.STORE_DIR = prev_root, prev_sess


@asynccontextmanager
async def _vite(ws_port: int, ui_port: int):
    """Start the Vite dev server pointing its WS at our in-process backend."""
    env = {
        **os.environ,
        "VITE_WS_URL": f"ws://127.0.0.1:{ws_port}",
        # ``vite.config.ts`` proxies ``/sessions`` to this — without it the
        # sidebar Recents fetch 500s (backend runs on a free port, not 8765).
        "VITE_BACKEND": f"http://127.0.0.1:{ws_port}",
    }
    proc = subprocess.Popen(
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


# ── playwright capture (hoisted from _e2e_m1_chaos.py) ──────────────────────


@dataclass
class Capture:
    console: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)


_CONSOLE_NOISE = ("Lit is in dev mode",)


def _wire_page_capture(page: Page, *, extra_noise: tuple[str, ...] = ()) -> Capture:
    """Collect ``pageerror``s and error/warning console messages on `page`.

    ``extra_noise`` extends the shared filter for scenario-specific chatter
    (e.g. the reconnect storm's "CLOSING or CLOSED state").
    """
    cap = Capture()
    noise = _CONSOLE_NOISE + extra_noise
    page.on("pageerror", lambda e: cap.page_errors.append(str(e)))
    page.on(
        "console",
        lambda m: (
            cap.console.append(f"[{m.type}] {m.text}")
            if m.type in ("error", "warning")
            and not any(n in m.text for n in noise)
            else None
        ),
    )
    return cap


# ── UI-driven fork lifecycle (hoisted from _smoke_ui_actions.py) ────────────


async def _wait_fork(session: Session, prev: int) -> Branch:
    """Wait for a UI-driven WS command to land and `_register_and_spawn` to
    register the new branch + spawn its `run()` task. Returns the fork."""
    for _ in range(200):
        if len(session.branches) > prev and len(session.branch_tasks) > 0:
            break
        await anyio.sleep(0.05)
    assert len(session.branches) > prev, (
        f"no new branch after action (still {len(session.branches)})"
    )
    assert session.current is not None
    return session.branches[session.current]


async def _play_to_end(session: Session, fork: Branch) -> None:
    """Release the fork's gate, wait for it to run to `end_conversation`, then
    push a full `state` so the frontend has the settled per-branch timeline.

    The incremental `{t:"timeline"}` rebuild fires on the *first* (pending)
    target ModelEvent of each turn; for a fork whose prefix replays from
    `pending` that can land before the trajectory span is fully established,
    leaving the frontend with a stale empty timeline. The action handlers
    under test don't depend on that machinery, so re-sync via `state` here.
    """
    fork.play()
    await session.broadcast_status()
    for _ in range(400):
        if fork.status == "ended":
            break
        await anyio.sleep(0.05)
    assert fork.status == "ended", f"fork {fork.branch_id} never ended (status={fork.status})"
    assert fork.error is None, f"fork {fork.branch_id} failed: {fork.error}"
    session.version += 1
    await session.broadcast({"t": "state", "v": session.version, **session.view()})


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
    """Project tool-call arguments into a hashable, order-insensitive set.

    Scalars (str/int/float/bool/None) pass through unchanged. Non-scalar
    values (dict/list/anything else) are canonicalised to a sorted-key JSON
    string so they remain comparable instead of being silently dropped — a
    `normalize()` `==` over a call whose only distinguishing arg is a dict
    would otherwise vacuously pass.
    """
    return frozenset(
        (k, v)
        if isinstance(v, (str, int, float, bool)) or v is None
        else (k, json.dumps(v, sort_keys=True, default=str))
        for k, v in arguments.items()
    )


def normalize(session: Session, branch_id: str) -> list[tuple[str, str, tuple]]:
    """Flatten `branch_id`'s level-2 audit tape into a comparable list.

    With the L2 `audit_history` (PETRI-L2-HISTORY), a child's
    `trajectory.tape.log` IS its full lineage — replayed shared prefix
    (steps `[:prefix_len]`, served verbatim from the parent) followed by
    its own divergent + live calls. So the user-visible execution order is
    exactly the tape's `ModelOutput` steps in log order, projected per role.
    Each entry is `(role, text, ((fn, frozenset(args)), …))`; non-scalar
    args are JSON-canonicalised (see `_scalar_args`).
    """
    roles = {GEN_SOURCE: "auditor", TARGET_GEN_SOURCE: "target"}
    out: list[tuple[str, str, tuple]] = []
    for s in session.branches[branch_id].audit_tape.log:
        role = roles.get(s.source)
        if role is None or not isinstance(s.value, ModelOutput):
            continue
        msg = s.value.message
        calls = tuple(
            (tc.function, _scalar_args(tc.arguments)) for tc in msg.tool_calls or []
        )
        out.append((role, msg.text, calls))
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
    session.branch_tasks[branch_id] = task
    await task
    assert b.error is None, f"base branch failed: {b.error}"
    assert b.status == "ended", f"base branch status={b.status}"
    return b


async def run_child(session: Session) -> tuple[str, Branch]:
    """Release the `session.current` child branch and await it.

    `_register_and_spawn` set `session.current` to the child and registered
    its `run()` task in `session.branch_tasks`. `{"t":"play"}` is idempotent
    if autoplay already fired; `await` returns immediately if the child
    already ended.
    """
    await _dispatch(session, {"t": "play"})
    child_id = session.current
    assert child_id is not None
    assert child_id in session.branch_tasks, "no child task spawned"
    await session.branch_tasks[child_id]
    child = session.branches[child_id]
    assert child.error is None, f"child branch failed: {child.error}"
    return child_id, child


# ── shared 3-turn base scenario ─────────────────────────────────────────────
#
#   T0  set_system_message("sys") · send_message("u1") · resume  → target "r1"
#   T1  send_message("u2") · resume                               → target "r2"
#   T2  end_conversation
#
# Built once; `auditor_by_turn`/`auditor_counted` deep-copy entries per call.

SCRIPT3: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]

T0 = (
    ("set_system_message", frozenset({("system_message", "sys")})),
    ("send_message", frozenset({("message", "u1")})),
    ("resume", frozenset()),
)
T1 = (("send_message", frozenset({("message", "u2")})), ("resume", frozenset()))
T2_END = (("end_conversation", frozenset()),)


def _send(msg: str) -> tuple:
    """`normalize()` shape for an auditor `send_message(msg) · resume` turn."""
    return (("send_message", frozenset({("message", msg)})), ("resume", frozenset()))


# ── assertion / lookup helpers ──────────────────────────────────────────────


def _fmt(seq: list[tuple]) -> str:
    lines: list[str] = []
    for role, text, calls in seq:
        cs = ", ".join(f"{fn}({dict(sorted(args))})" for fn, args in calls)
        lines.append(f"    ({role!r}, {text!r}, [{cs}])")
    return "\n".join(lines) if lines else "    <empty>"


def _diff(actual: list[tuple], expected: list[tuple]) -> str:
    return (
        f"\nactual   ({len(actual)}):\n{_fmt(actual)}"
        f"\nexpected ({len(expected)}):\n{_fmt(expected)}"
    )


def _nth_target_anchor(log: list, n: int) -> str:
    """anchor_id of the n-th (0-based) target generate in `log`."""
    hits = [
        s.anchor_id
        for s in log
        if s.source == TARGET_GEN_SOURCE
        and isinstance(s.value, ModelOutput)
        and s.anchor_id is not None
    ]
    assert len(hits) > n, f"need ≥{n + 1} target steps, got {len(hits)}"
    return hits[n]


def _pool_msg_id(session: Session, role: str, text: str) -> str:
    for m in session.pool:
        if m.role == role and m.text == text:
            assert m.id is not None
            return m.id
    raise AssertionError(f"no pool {role} message with text {text!r}")


def _pool_user_id(session: Session, text: str) -> str:
    return _pool_msg_id(session, "user", text)


def _count_trajectories(history) -> int:
    return sum(1 for _ in _walk(history.root))


# ── runner ──────────────────────────────────────────────────────────────────


async def run_suite(
    tests: Sequence[tuple[str, Callable[[], Coroutine[Any, Any, None]]]],
) -> int:
    """Run each `(name, async fn)` pair; print PASS/FAIL/ERROR; return exit code."""
    failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}{exc}")
        except Exception:
            failed += 1
            print(f"ERROR {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0
