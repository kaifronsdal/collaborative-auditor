"""Wire-level e2e for the `_dispatch` action surface (W2–W16 of the design).

Each test builds a fresh `Session`, runs a deterministic 3-turn base branch
to completion, dispatches one command via `_dispatch`, plays the child to
completion, then asserts ONE thing: `normalize(session, child_id)` equals a
hardcoded full ordered event list.

These are written against the *intended* post-`EmittingTape` behaviour
(replayed prefix events appear with real content, one ModelEvent per turn,
in execution order). They may FAIL on the current backend; failures print
the actual `normalize()` output so the divergence is visible.

Coverage audit (W1–W16 of the original plan):

  W1   start                           → covered: `_smoke_server.py` S1
                                          (`_dispatch` `start` doesn't accept
                                          mockllm `custom_outputs`, so a
                                          deterministic wire test is N/A)
  W2   step (single-step gate)         → here
  W3   pause mid-run                   → N/A: racy with instant mockllm;
                                          `_smoke_server.py` S3 covers dispatch
  W4   end                             → here
  W5c  inject into ended branch        → here (5a/5b queue-drain timing
                                          doesn't fit the normalize() pattern)
  W6   branch (target inclusive)       → here
  W7   resample (target exclusive)     → here
  W8   branch_auditor / resample_aud.  → here
  W9   edit_auditor_call               → here
  W10  edit_target_message role=user   → here
  W11  edit_target_message role=system → here
  W12  edit_target_message role=tool   → here
  W12b edit_target_message multi-send  → here (positional disambiguation)
  W14  rewrite_tool_call → apply       → covered: `_smoke_rewrite.py` R1
                                          (draft) + W9 (apply path)
  W15  switch                          → here
  W16  fork while parent running       → here (`_stop_running_branches`)

Run:  uv run python -m workbench._smoke_actions_wire
"""

from __future__ import annotations

import sys

import anyio
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolCall

from workbench._smoke_fixtures import (
    SCRIPT3,
    T0,
    T1,
    T2_END,
    _auditor_turn,
    _diff,
    _nth_target_anchor,
    _pool_msg_id,
    _pool_user_id,
    _send,
    _tc,
    auditor_by_turn,
    auditor_counted,
    make_base,
    normalize,
    run_child,
    run_suite,
    target_by_last_user,
    target_counted,
)
from workbench._smoke_util import FakeConn
from workbench.run import find_auditor_step
from workbench.server import _dispatch
from workbench.session import Session

# ── helpers ─────────────────────────────────────────────────────────────────


async def _settle(session: Session, child_id: str, n: int) -> list[tuple]:
    """Poll `normalize()` until it has `n` entries (or time out)."""
    for _ in range(200):
        actual = normalize(session, child_id)
        if len(actual) >= n:
            return actual
        await anyio.sleep(0.01)
    raise AssertionError(
        f"normalize({child_id}) stuck at {len(actual)} entries, wanted {n}"
    )


# ── W6: branch (target inclusive) ───────────────────────────────────────────


async def w6_branch() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        anchor = _nth_target_anchor(base.audit_tape.log, 0)
        await _dispatch(session, {"t": "branch", "branch": "base", "at": anchor})
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W7: resample (target exclusive) ─────────────────────────────────────────


async def w7_resample() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted({"u1": ["r1", "r1-v2"], "u2": ["r2"]}),
            max_turns=3,
        )
        anchor = _nth_target_anchor(base.audit_tape.log, 0)
        await _dispatch(session, {"t": "resample", "branch": "base", "at": anchor})
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1-v2", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W8: branch_auditor / resample_auditor ───────────────────────────────────

T1_ALT = _auditor_turn(_tc("send_message", message="u2-alt"), _tc("resume"))

W8_EXPECTED = [
    ("auditor", "", T0),
    ("target", "r1", ()),
    ("auditor", "", _send("u2-alt")),
    ("target", "r2-alt", ()),
    ("auditor", "", T2_END),
]


async def _w8_one(cmd: str) -> list[tuple]:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_counted(SCRIPT3, alt={1: T1_ALT}),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-alt": "r2-alt"}
            ),
            max_turns=3,
        )
        await _dispatch(session, {"t": cmd, "branch": "base", "turn_index": 1})
        child_id, _ = await run_child(session)
        return normalize(session, child_id)
    finally:
        await session.close()


async def w8_resample_auditor() -> None:
    actual = await _w8_one("resample_auditor")
    assert actual == W8_EXPECTED, _diff(actual, W8_EXPECTED)


async def w8_branch_auditor() -> None:
    actual = await _w8_one("branch_auditor")
    assert actual == W8_EXPECTED, _diff(actual, W8_EXPECTED)


# ── W9: edit_auditor_call ───────────────────────────────────────────────────

W9_EXPECTED = [
    ("auditor", "", T0),
    ("target", "r1", ()),
    ("auditor", "", _send("u2-EDIT")),
    ("target", "r2-EDIT", ()),
    ("auditor", "", T2_END),
]


async def w9_edit_auditor_call() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-EDIT": "r2-EDIT"}
            ),
            max_turns=3,
        )
        step = find_auditor_step(base.audit_tape.log, 1)
        assert isinstance(step.value, ModelOutput)
        call_id = next(
            tc.id
            for tc in (step.value.message.tool_calls or [])
            if tc.function == "send_message"
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 1,
                "call_id": call_id,
                "args": {"message": "u2-EDIT"},
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        assert actual == W9_EXPECTED, _diff(actual, W9_EXPECTED)
    finally:
        await session.close()


# ── W10: edit_target_message (role=user) — same path as W9 ──────────────────


async def w10_edit_target_message() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-EDIT": "r2-EDIT"}
            ),
            max_turns=3,
        )
        msg_id = _pool_user_id(session, "u2")
        await _dispatch(
            session,
            {
                "t": "edit_target_message",
                "branch": "base",
                "message_id": msg_id,
                "role": "user",
                "content": "u2-EDIT",
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        # Same expected list as W9 — proves edit_target_message and
        # edit_auditor_call converge on the same fork path.
        assert actual == W9_EXPECTED, _diff(actual, W9_EXPECTED)
    finally:
        await session.close()


# ── W2: step (single-step gate) ─────────────────────────────────────────────


async def w2_step() -> None:
    """`step` releases exactly one auditor turn; the gate re-blocks after."""
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        # Fork exclusive of auditor T1 → prefix replays T0+r1, then pauses
        # at the gate before live T1 (`branch_auditor` is autoplay=False).
        await _dispatch(
            session, {"t": "branch_auditor", "branch": "base", "turn_index": 1}
        )
        assert session.current is not None and session.current != "base"
        child_id = session.current
        task = session.branch_tasks[child_id]

        # One step → T1 + r2 land; gate re-blocks before T2.
        await _dispatch(session, {"t": "step"})
        actual = await _settle(session, child_id, 4)
        await anyio.sleep(0.05)
        actual = normalize(session, child_id)
        expected_mid = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
        ]
        assert actual == expected_mid, _diff(actual, expected_mid)
        assert not task.done(), "branch ended after one step (gate did not re-block)"

        # Second step → T2 (end_conversation) → task completes.
        await _dispatch(session, {"t": "step"})
        await task
        actual = normalize(session, child_id)
        expected = [*expected_mid, ("auditor", "", T2_END)]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W4: end ─────────────────────────────────────────────────────────────────


async def w4_end() -> None:
    """`end` on a paused-at-gate branch: target receives `EndConversation`,
    `_stop_running_branches` cancels the auditor task, status → ended."""
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        await _dispatch(
            session, {"t": "branch_auditor", "branch": "base", "turn_index": 1}
        )
        assert session.current is not None and session.current != "base"
        child_id = session.current
        task = session.branch_tasks[child_id]
        assert not task.done()

        await _dispatch(session, {"t": "end"})
        assert task.done(), "branch task survived `end`"
        assert session.branches[child_id].status == "ended"

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W5c: inject into ended branch ───────────────────────────────────────────


async def w5c_inject_ended() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        conn = FakeConn()
        session.connections.append(conn)
        await _dispatch(
            session,
            {
                "t": "inject",
                "branch": "base",
                "role": "auditor",
                "message": {"role": "user", "content": "late feedback", "id": "inj1"},
            },
        )
        errs = [m for m in conn.sent if m["t"] == "error"]
        assert len(errs) == 1, f"expected 1 error, got {[m['t'] for m in conn.sent]}"
        assert "ended" in errs[0]["message"]
        assert base.queued["auditor"] == [], "queued should not grow on ended branch"
    finally:
        await session.close()


# ── W11: edit_target_message (role=system) ──────────────────────────────────


async def w11_edit_target_system() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        msg_id = _pool_msg_id(session, "system", "sys")
        await _dispatch(
            session,
            {
                "t": "edit_target_message",
                "branch": "base",
                "message_id": msg_id,
                "role": "system",
                "content": "sys-EDIT",
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        t0_edit = (
            ("set_system_message", frozenset({("system_message", "sys-EDIT")})),
            ("send_message", frozenset({("message", "u1")})),
            ("resume", frozenset()),
        )
        expected = [
            ("auditor", "", t0_edit),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W12: edit_target_message (role=tool, with tool_call_id) ─────────────────
#
# Base scenario (target makes a tool_call, auditor supplies the result):
#   T0  set_system_message · create_tool · send_message("ask") · resume
#       → target calls get_weather(tool_call_id="tc-1")
#   T1  send_tool_call_result(tc-1, "72F") · resume  → target "warm"
#   T2  end_conversation


def _target_tool_call(call_id: str, fn: str, **args: object) -> ModelOutput:
    out = ModelOutput.from_content(model="mockllm", content="")
    out.choices[0].message.tool_calls = [
        ToolCall(id=call_id, function=fn, type="function", arguments=dict(args))
    ]
    return out


TOOL_SCRIPT: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc(
            "create_tool",
            environment_description="weather api",
            name="get_weather",
            description="get weather",
            parameters={"type": "object", "properties": {}},
        ),
        _tc("send_message", message="ask"),
        _tc("resume"),
    ),
    _auditor_turn(
        _tc("send_tool_call_result", tool_call_id="tc-1", result="72F"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]

TOOL_T0 = (
    ("set_system_message", frozenset({("system_message", "sys")})),
    (
        "create_tool",
        frozenset(
            {
                ("environment_description", "weather api"),
                ("name", "get_weather"),
                ("description", "get weather"),
                ("parameters", '{"properties": {}, "type": "object"}'),
            }
        ),
    ),
    ("send_message", frozenset({("message", "ask")})),
    ("resume", frozenset()),
)
TOOL_TGT0 = ("target", "", (("get_weather", frozenset()),))


async def w12_edit_target_tool() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(TOOL_SCRIPT),
            target_outputs=target_by_last_user(
                {
                    "ask": _target_tool_call("tc-1", "get_weather"),
                    "72F": "warm",
                    "30F": "cold",
                }
            ),
            max_turns=3,
        )
        msg_id = _pool_msg_id(session, "tool", "72F")
        await _dispatch(
            session,
            {
                "t": "edit_target_message",
                "branch": "base",
                "message_id": msg_id,
                "role": "tool",
                "tool_call_id": "tc-1",
                "content": "30F",
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        t1_edit = (
            (
                "send_tool_call_result",
                frozenset({("tool_call_id", "tc-1"), ("result", "30F")}),
            ),
            ("resume", frozenset()),
        )
        expected = [
            ("auditor", "", TOOL_T0),
            TOOL_TGT0,
            ("auditor", "", t1_edit),
            ("target", "cold", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W12b: edit_target_message — multi-send_message per turn ─────────────────
#
# T1 has TWO `send_message` calls. `locate_staging_call`'s positional
# disambiguation (count `boundary=="in"` marks between the auditor step and
# the matched mark) must pick the right one. We edit the *second* user
# message — `pos == 1` → `calls[1]`.

MULTI_SCRIPT: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(
        _tc("send_message", message="u2a"),
        _tc("send_message", message="u2b"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]


async def w12b_edit_target_multi_send() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(MULTI_SCRIPT),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2b": "r2", "u2b-EDIT": "r2-EDIT"}
            ),
            max_turns=3,
        )
        msg_id = _pool_user_id(session, "u2b")
        await _dispatch(
            session,
            {
                "t": "edit_target_message",
                "branch": "base",
                "message_id": msg_id,
                "role": "user",
                "content": "u2b-EDIT",
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        t1_edit = (
            ("send_message", frozenset({("message", "u2a")})),
            ("send_message", frozenset({("message", "u2b-EDIT")})),
            ("resume", frozenset()),
        )
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", t1_edit),
            ("target", "r2-EDIT", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── W15: switch ─────────────────────────────────────────────────────────────


async def w15_switch() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        anchor = _nth_target_anchor(base.audit_tape.log, 0)
        await _dispatch(session, {"t": "branch", "branch": "base", "at": anchor})
        child_id = session.current
        assert child_id is not None and child_id != "base"

        conn = FakeConn()
        session.connections.append(conn)
        await _dispatch(session, {"t": "switch", "branch": "base"})
        assert session.current == "base", f"switch left current={session.current!r}"
        states = [m for m in conn.sent if m["t"] == "state"]
        assert len(states) == 1 and states[0]["current"] == "base"

        await _dispatch(session, {"t": "switch", "branch": child_id})
        assert session.current == child_id
        # Drain the child so `session.close()` is a clean cancel-free shutdown.
        _, _ = await run_child(session)
    finally:
        await session.close()


# ── W16: fork while parent running (`_stop_running_branches`) ───────────────


async def w16_fork_running_parent() -> None:
    """Fork *from* a branch whose `run()` task is still alive (paused at the
    gate). `_register_and_spawn` must cancel that task before spawning the
    child — otherwise two branches race on the shared session transcript /
    `_on_event` / pool (footgun #2)."""
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted({"u1": ["r1", "r1-v2"], "u2": ["r2"]}),
            max_turns=3,
        )
        # Parent A: forked, replays T0+r1, then blocks at the gate (live task).
        await _dispatch(
            session, {"t": "branch_auditor", "branch": "base", "turn_index": 1}
        )
        a_id = session.current
        assert a_id is not None and a_id != "base"
        a = session.branches[a_id]
        task_a = session.branch_tasks[a_id]
        assert not task_a.done(), "parent A should be alive (paused at gate)"

        # Fork B from A at A's first target anchor (in A's replayed prefix).
        # `_stop_running_branches` must cancel task_a synchronously inside
        # `_register_and_spawn` before B's task is spawned.
        anchor = _nth_target_anchor(a.audit_tape.log, 0)
        await _dispatch(session, {"t": "resample", "branch": a_id, "at": anchor})
        assert task_a.done(), "`_stop_running_branches` did not cancel parent A"
        assert a.status == "ended", f"A.status={a.status!r} after cancel"

        b_id, _ = await run_child(session)
        assert b_id != a_id
        actual = normalize(session, b_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1-v2", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── runner ──────────────────────────────────────────────────────────────────

TESTS = [
    ("W2   step (single-step gate)", w2_step),
    ("W4   end", w4_end),
    ("W5c  inject into ended branch", w5c_inject_ended),
    ("W6   branch (target inclusive)", w6_branch),
    ("W7   resample (target exclusive)", w7_resample),
    ("W8   resample_auditor", w8_resample_auditor),
    ("W8b  branch_auditor", w8_branch_auditor),
    ("W9   edit_auditor_call", w9_edit_auditor_call),
    ("W10  edit_target_message role=user", w10_edit_target_message),
    ("W11  edit_target_message role=system", w11_edit_target_system),
    ("W12  edit_target_message role=tool", w12_edit_target_tool),
    ("W12b edit_target_message multi-send", w12b_edit_target_multi_send),
    ("W15  switch", w15_switch),
    ("W16  fork while parent running", w16_fork_running_parent),
]


if __name__ == "__main__":
    sys.exit(anyio.run(run_suite, TESTS))
