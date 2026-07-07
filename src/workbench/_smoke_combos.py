"""Adversarial multi-action combos for the branch/edit/resample machinery.

Goes after interaction effects between auditor behaviours (rollback,
multi-call turns, prefill, create_tool/remove_tool) and user actions
(edit/resample/branch on either column, inject, switch) that the
single-action E1–E8 / W6–W16 suites don't reach.

Each case builds a deterministic mockllm scenario, dispatches a *sequence*
of `_dispatch` commands, runs the resulting child(ren), and asserts on
`normalize()` (full lineage list) plus any combo-specific invariant.

Run:  uv run python -m workbench._smoke_combos
"""

from __future__ import annotations

import asyncio
import copy
import sys

import anyio
from inspect_ai.model import (
    ChatMessage,
    GenerateConfig,
    ModelOutput,
)
from inspect_ai.tool import ToolChoice, ToolInfo

from workbench._smoke_fixtures import (
    SCRIPT3,
    T0,
    T2_END,
    _auditor_turn,
    _count_trajectories,
    _diff,
    _nth_target_anchor,
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
from workbench.run import Branch, find_auditor_step
from workbench.server import _dispatch
from workbench.session import Session
from workbench.sources import TARGET_GEN_SOURCE
from workbench.timeline import build_auditor_timeline

# ── shared shapes ───────────────────────────────────────────────────────────


def _rb_send(mid: str, msg: str) -> tuple:
    return (
        ("rollback_conversation", frozenset({("message_id", mid)})),
        ("send_message", frozenset({("message", msg)})),
        ("resume", frozenset()),
    )


def _call_id(branch: Branch, turn_index: int, fn: str) -> str:
    step = find_auditor_step(branch.audit_tape.log, turn_index)
    assert isinstance(step.value, ModelOutput)
    return next(tc.id for tc in (step.value.message.tool_calls or []) if tc.function == fn)


# ── C1: rollback in shared prefix → user edits the turn after it ───────────
# Base T2 = rollback(M3)·send(u3)·resume; user edits T3's send. The shared
# prefix T0–T2 includes the rollback, so the child's L1 history rebuilds
# both trajectories during replay; `normalize()` carries r2 (the rolled-
# back response) AND r3/r4-EDIT.

SCRIPT_RB1: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u3"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u4"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]


async def c1_rollback_then_edit_after() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_RB1),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u3": "r3", "u4": "r4", "u4-EDIT": "r4-EDIT"}
            ),
            max_turns=5,
        )
        assert _count_trajectories(base.history) == 2
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 3,
                "call_id": _call_id(base, 3, "send_message"),
                "args": {"message": "u4-EDIT"},
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2")),
            ("target", "r2", ()),
            ("auditor", "", _rb_send("M3", "u3")),
            ("target", "r3", ()),
            ("auditor", "", _send("u4-EDIT")),
            ("target", "r4-EDIT", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        # Replay re-ran the rollback on a fresh L1 → child mirrors base's
        # 2-trajectory shape.
        assert _count_trajectories(child.history) == 2, (
            f"child L1 trajectories={_count_trajectories(child.history)}, want 2"
        )
    finally:
        await session.close()


# ── C2: user edit → auditor rollback in the regenerated suffix ─────────────
# Edit T1 (send u2 → u2-EDIT). The child's first LIVE auditor turn is T2;
# `auditor_counted`'s alt[2] makes T2 a rollback(M3)·send·resume — i.e. the
# rollback is *introduced* by the regenerated suffix, not the prefix. The
# child's L1 history must branch correctly post-edit (2 trajectories) even
# though base never rolled back (1 trajectory).


async def c2_edit_then_rollback_in_regen() -> None:
    script: list[ModelOutput] = [
        _auditor_turn(
            _tc("set_system_message", system_message="sys"),
            _tc("send_message", message="u1"),
            _tc("resume"),
        ),
        _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
        _auditor_turn(_tc("end_conversation")),
        _auditor_turn(_tc("end_conversation")),  # T3 for the alt path
    ]
    alt = {
        2: _auditor_turn(
            _tc("rollback_conversation", message_id="M3"),
            _tc("send_message", message="u3"),
            _tc("resume"),
        )
    }
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_counted(script, alt=alt),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-EDIT": "r2-EDIT", "u3": "r3"}
            ),
            max_turns=4,
        )
        assert _count_trajectories(base.history) == 1, (
            f"base should not have rolled back; "
            f"trajectories={_count_trajectories(base.history)}"
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 1,
                "call_id": _call_id(base, 1, "send_message"),
                "args": {"message": "u2-EDIT"},
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-EDIT")),
            ("target", "r2-EDIT", ()),
            ("auditor", "", _rb_send("M3", "u3")),
            ("target", "r3", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        # The rollback fired LIVE in the child → fresh L1 trajectory.
        assert _count_trajectories(child.history) == 2, (
            f"child L1 trajectories={_count_trajectories(child.history)}, want 2 "
            f"(rollback at live T2 should branch the target history)"
        )
    finally:
        await session.close()


# ── C3: branch chain with switches (P→C, switch P, P→D) ────────────────────
# Two children of P at *different* anchors. `build_auditor_timeline` must
# show both as children of P; switching between P/C/D leaves each branch's
# `normalize()` unchanged (switch is view-only).


async def c3_branch_chain_switches() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        snap_p = normalize(session, "base")

        # C: branch (inclusive) at r1.
        await _dispatch(
            session,
            {"t": "branch", "branch": "base", "at": _nth_target_anchor(base.audit_tape.log, 0)},
        )
        c_id, _ = await run_child(session)
        snap_c = normalize(session, c_id)

        # Switch back to P, then D: branch at r2 (different anchor).
        await _dispatch(session, {"t": "switch", "branch": "base"})
        assert session.current == "base"
        await _dispatch(
            session,
            {"t": "branch", "branch": "base", "at": _nth_target_anchor(base.audit_tape.log, 1)},
        )
        d_id, d = await run_child(session)
        snap_d = normalize(session, d_id)

        assert c_id != d_id and d.parent_id == "base", (
            f"D.parent={d.parent_id!r}; expected sibling under base"
        )

        # Timeline: C and D are siblings under base, distinct branched_from.
        tl = build_auditor_timeline(session)
        base_span = next(s for s in tl["root"]["branches"] if s["id"] == "base")
        kids = {s["id"]: s for s in base_span["branches"]}
        assert c_id in kids and d_id in kids, (
            f"timeline children of base: {sorted(kids)}; want {c_id!r}, {d_id!r}"
        )
        assert kids[c_id]["branched_from"] != kids[d_id]["branched_from"], (
            f"C/D should splice at different auditor anchors; both "
            f"branched_from={kids[c_id]['branched_from']!r}"
        )

        # Switching is view-only: each branch's tape is stable across switches.
        for bid, snap in [("base", snap_p), (c_id, snap_c), (d_id, snap_d)]:
            await _dispatch(session, {"t": "switch", "branch": bid})
            assert session.current == bid
            after = normalize(session, bid)
            assert after == snap, f"[{bid}] normalize changed across switch" + _diff(
                after, snap
            )
    finally:
        await session.close()


# ── C4: inject while child is paused post-replay → drained at first LIVE ───
# Fork (autoplay=False) → `_register_and_spawn` awaits `_replayed` so by the
# time we can dispatch `inject`, replay is done and the child is parked at
# the gate. Inject feedback; play. The auditor's first live turn must see
# the feedback in its input — verified by a mockllm callable that returns a
# marker `send_message` when "FEEDBACK" is present.


def _auditor_feedback_aware(script: list[ModelOutput]) -> object:
    def _out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        k = sum(1 for m in input if m.role == "assistant")
        if any(m.role == "user" and m.text == "FEEDBACK" for m in input):
            return copy.deepcopy(
                _auditor_turn(_tc("send_message", message=f"u{k + 1}-FB"), _tc("resume"))
            )
        return copy.deepcopy(script[k])

    return _out


async def c4_inject_during_paused_replay() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=_auditor_feedback_aware(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-FB": "r2-FB", "u3-FB": "r3-FB"}
            ),
            max_turns=3,
        )
        # Fork exclusive of T1 → prefix replays T0+r1; child paused before T1.
        await _dispatch(
            session, {"t": "branch_auditor", "branch": "base", "turn_index": 1}
        )
        child_id = session.current
        assert child_id is not None and child_id != "base"
        child = session.branches[child_id]
        assert child._replayed.is_set(), "replay should be done before inject"
        assert child.queued["auditor"] == []

        await _dispatch(
            session,
            {
                "t": "inject",
                "branch": child_id,
                "role": "auditor",
                "message": {"role": "user", "content": "FEEDBACK", "id": "fb1"},
            },
        )
        assert len(child.queued["auditor"]) == 1, (
            f"inject did not queue: {child.queued['auditor']}"
        )

        await _dispatch(session, {"t": "play"})
        await session.branch_tasks[child_id]
        assert child.error is None, f"child failed: {child.error}"

        # Feedback drained at the first live turn (T1) → never re-drained at T2.
        assert child.queued["auditor"] == [], (
            f"feedback not drained: {child.queued['auditor']}"
        )
        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-FB")),
            ("target", "r2-FB", ()),
            ("auditor", "", _send("u3-FB")),
            ("target", "r3-FB", ()),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── C5: edit_target_message on a message inside a rolled-back trajectory ───
# Base rolls back at T2 (drops u2/r2). u2 lives only in the rolled-back L1
# trajectory. Its `boundary=="in"` mark is still on the L2 tape (the audit
# tape records every staged command regardless of L1 rollback), so
# `locate_staging_call` SHOULD find it and map to T1's send_message.
# Semantically the edit forks at T1, *before* the rollback, so the child
# re-runs the rollback live and the edited u2 ends up in the child's
# rolled-back trajectory too — the active conversation is unchanged.


async def c5_edit_rolled_back_message() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_RB1),
            target_outputs=target_by_last_user(
                {
                    "u1": "r1",
                    "u2": "r2",
                    "u2-EDIT": "r2-EDIT",
                    "u3": "r3",
                    "u4": "r4",
                }
            ),
            max_turns=5,
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
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        # The edit forked at T1; T2's rollback fires live in the child and
        # rolls past r2-EDIT — so the *active* target conversation is
        # identical to base's. The edited u2 is visible only on the L2 tape
        # and in the child's rolled-back L1 trajectory. This is arguably
        # surprising UX (the user's edit is immediately rolled back by the
        # auditor) but mechanically correct.
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-EDIT")),
            ("target", "r2-EDIT", ()),
            ("auditor", "", _rb_send("M3", "u3")),
            ("target", "r3", ()),
            ("auditor", "", _send("u4")),
            ("target", "r4", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        assert _count_trajectories(child.history) == 2, (
            f"child L1 trajectories={_count_trajectories(child.history)}"
        )
    finally:
        await session.close()


# ── C6: resample → edit-the-resample → resample again (3-deep at one turn) ─


async def c6_resample_edit_resample_chain() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_counted(
                SCRIPT3,
                alt={
                    1: _auditor_turn(
                        _tc("send_message", message="u2-RS"), _tc("resume")
                    )
                },
            ),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-RS": "r2-RS", "u2-ED": "r2-ED"}
            ),
            max_turns=3,
        )
        # C: resample auditor T1 (regenerates → u2-RS via alt[1]).
        await _dispatch(
            session, {"t": "resample_auditor", "branch": "base", "turn_index": 1}
        )
        c_id, c = await run_child(session)
        # D: edit C's T1 → u2-ED.
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": c_id,
                "turn_index": 1,
                "call_id": _call_id(c, 1, "send_message"),
                "args": {"message": "u2-ED"},
            },
        )
        d_id, d = await run_child(session)
        # E: resample D's T1 again (alt[1] still fires → u2-RS).
        await _dispatch(
            session, {"t": "resample_auditor", "branch": d_id, "turn_index": 1}
        )
        e_id, e = await run_child(session)

        for bid, msg in [(c_id, "u2-RS"), (d_id, "u2-ED"), (e_id, "u2-RS")]:
            actual = normalize(session, bid)
            expected = [
                ("auditor", "", T0),
                ("target", "r1", ()),
                ("auditor", "", _send(msg)),
                ("target", f"r{msg[1:]}", ()),
                ("auditor", "", T2_END),
            ]
            assert actual == expected, f"[{bid}]" + _diff(actual, expected)

        # L2 lineage: base → C → D → E (chain, not siblings — each fork's
        # turn-1 anchor lives in the previous child's fresh steps:
        # C's live T1 in C.log[prefix_len:], D's edited T1 at
        # D.log[prefix_len], so `_find_node_with_anchor` resolves E → D).
        assert c.parent_id == "base", f"C.parent={c.parent_id!r}"
        assert d.parent_id == c_id, f"D.parent={d.parent_id!r}, want {c_id!r}"
        assert e.parent_id == d_id, f"E.parent={e.parent_id!r}, want {d_id!r}"
        # All three cut at the same prefix length (just before T1).
        assert c.shared_prefix_len == d.shared_prefix_len == e.shared_prefix_len, (
            f"prefix_len differ: C={c.shared_prefix_len} D={d.shared_prefix_len} "
            f"E={e.shared_prefix_len}"
        )
    finally:
        await session.close()


# ── C7: create_tool with dict-valued `parameters` → edit that dict ─────────
# Tests that non-scalar tool-call args (a JSON-schema dict) survive
# `edited_auditor_step`'s deep-copy + `normalize()`'s `_scalar_args`
# canonicalisation. The edit changes the schema; the child's L2 tape must
# carry the edited dict.

PARAMS_ORIG = {"type": "object", "properties": {"q": {"type": "string"}}}
PARAMS_EDIT = {
    "type": "object",
    "properties": {"q": {"type": "string"}, "n": {"type": "integer"}},
}


def _tool_t0_calls(params_json: str) -> tuple:
    return (
        ("set_system_message", frozenset({("system_message", "sys")})),
        (
            "create_tool",
            frozenset(
                {
                    ("environment_description", "env"),
                    ("name", "lookup"),
                    ("description", "look something up"),
                    ("parameters", params_json),
                }
            ),
        ),
        ("send_message", frozenset({("message", "u1")})),
        ("resume", frozenset()),
    )


async def c7_edit_create_tool_parameters() -> None:
    import json

    script: list[ModelOutput] = [
        _auditor_turn(
            _tc("set_system_message", system_message="sys"),
            _tc(
                "create_tool",
                environment_description="env",
                name="lookup",
                description="look something up",
                parameters=PARAMS_ORIG,
            ),
            _tc("send_message", message="u1"),
            _tc("resume"),
        ),
        _auditor_turn(_tc("end_conversation")),
    ]
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(script),
            target_outputs=target_by_last_user({"u1": "r1"}),
            max_turns=2,
        )
        # Sanity: base normalize() shows the original params dict (JSON-canon).
        actual_base = normalize(session, "base")
        expected_base = [
            ("auditor", "", _tool_t0_calls(json.dumps(PARAMS_ORIG, sort_keys=True))),
            ("target", "r1", ()),
            ("auditor", "", T2_END),
        ]
        assert actual_base == expected_base, _diff(actual_base, expected_base)

        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 0,
                "call_id": _call_id(base, 0, "create_tool"),
                "args": {
                    "environment_description": "env",
                    "name": "lookup",
                    "description": "look something up",
                    "parameters": PARAMS_EDIT,
                },
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", _tool_t0_calls(json.dumps(PARAMS_EDIT, sort_keys=True))),
            ("target", "r1", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)

        # The child's target actually saw the edited tool definition: the
        # `AddTool` command's L1 receipt carries the resolved `ToolInfo`.
        tool_names_props: list[tuple[str, set[str]]] = []
        for traj in (child.history.root, *child.history.root.children):
            for s in traj.tape.log:
                ti = getattr(getattr(s.value, "tool", None), "parameters", None)
                if ti is not None and getattr(s.value, "tool", None) is not None:
                    tool_names_props.append(
                        (s.value.tool.name, set(s.value.tool.parameters.properties))
                    )
        assert ("lookup", {"q", "n"}) in tool_names_props, (
            f"child target never saw edited tool schema; L1 tool receipts: "
            f"{tool_names_props}"
        )
    finally:
        await session.close()


# ── C8: edit resume(prefill=…) ──────────────────────────────────────────────
# Base T1 = send·resume(prefill="PRE-A"); edit the resume's `prefill` arg.
#
# CURRENTLY FAILS — backend bug, not a test bug:
#   `workbench_auditor` builds `auditor_tools()` with the default
#   `prefill=False`, which registers `resume` with `parameters=ToolParams()`
#   (no args). A `resume` call carrying a `prefill` arg — whether emitted by
#   a real auditor model OR introduced via `edit_auditor_call` — fails
#   inspect's tool-arg validation, the target is never resumed, and the
#   turn silently produces no target response (no `{t:"error"}`, no
#   `branch.error`). The expected list below asserts the *correct*
#   behaviour (r2 present); the diff on failure shows r2 missing.


async def c8_edit_resume_prefill() -> None:
    script: list[ModelOutput] = [
        _auditor_turn(
            _tc("set_system_message", system_message="sys"),
            _tc("send_message", message="u1"),
            _tc("resume"),
        ),
        _auditor_turn(
            _tc("send_message", message="u2"), _tc("resume", prefill="PRE-A")
        ),
        _auditor_turn(_tc("end_conversation")),
    ]
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(script),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 1,
                "call_id": _call_id(base, 1, "resume"),
                "args": {"prefill": "PRE-B"},
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        t1_edit = (
            ("send_message", frozenset({("message", "u2")})),
            ("resume", frozenset({("prefill", "PRE-B")})),
        )
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", t1_edit),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── C9: two rollbacks in the shared prefix → edit T5 ───────────────────────
# Base: T2=rollback(M3)·send(u3), T4=rollback(M7)·send(u5). Edit T5's send.
# Replaying T0–T4 on a fresh L1 must rebuild THREE trajectories.

SCRIPT_RB2: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u3"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u4"), _tc("resume")),
    _auditor_turn(
        _tc("rollback_conversation", message_id="M7"),
        _tc("send_message", message="u5"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u6"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]


async def c9_two_rollbacks_then_edit() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_RB2),
            target_outputs=target_by_last_user(
                {
                    "u1": "r1",
                    "u2": "r2",
                    "u3": "r3",
                    "u4": "r4",
                    "u5": "r5",
                    "u6": "r6",
                    "u6-EDIT": "r6-EDIT",
                }
            ),
            max_turns=7,
        )
        assert _count_trajectories(base.history) == 3, (
            f"base L1 trajectories={_count_trajectories(base.history)}, want 3"
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 5,
                "call_id": _call_id(base, 5, "send_message"),
                "args": {"message": "u6-EDIT"},
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2")),
            ("target", "r2", ()),
            ("auditor", "", _rb_send("M3", "u3")),
            ("target", "r3", ()),
            ("auditor", "", _send("u4")),
            ("target", "r4", ()),
            ("auditor", "", _rb_send("M7", "u5")),
            ("target", "r5", ()),
            ("auditor", "", _send("u6-EDIT")),
            ("target", "r6-EDIT", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        assert _count_trajectories(child.history) == 3, (
            f"child L1 trajectories={_count_trajectories(child.history)}, want 3 "
            f"(both rollbacks must rebuild during prefix replay)"
        )
    finally:
        await session.close()


# ── C10: fork from a frozen (cancelled-mid-run) parent at an unreached turn ─
# E7-style: gate base through T0+T1, leave T2 unrun, then issue a fork that
# cancels base mid-run. Now base is frozen at 2 auditor turns; ask to
# resample auditor T2 — `find_auditor_step` must raise "out of range",
# `_dispatch` must surface a `{t:"error"}`, and NO child is spawned.


async def c10_fork_frozen_unreached_turn() -> None:
    session = Session()
    await session.start()
    try:
        base = Branch(
            session,
            "base",
            seed="combo-suite",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=3,
            auditor_model_args={"custom_outputs": auditor_by_turn(SCRIPT3)},
            target_model_args={
                "custom_outputs": target_counted({"u1": ["r1", "r1-v2"], "u2": ["r2"]})
            },
        )
        session.branches["base"] = base
        session.current = "base"
        task = asyncio.create_task(base.run())
        session.branch_tasks["base"] = task

        async def _step_until(n_target: int) -> None:
            base.step()
            with anyio.fail_after(5.0):
                while (
                    sum(
                        1
                        for s in base.audit_tape.log
                        if s.source == TARGET_GEN_SOURCE
                        and isinstance(s.value, ModelOutput)
                    )
                    < n_target
                ):
                    await anyio.sleep(0.005)

        await _step_until(1)
        await _step_until(2)
        assert not task.done()

        # Fork once (cancels base) — child C runs fine.
        await _dispatch(
            session,
            {"t": "resample", "branch": "base", "at": _nth_target_anchor(base.audit_tape.log, 0)},
        )
        assert task.done() and base.status == "ended"
        c_id, _ = await run_child(session)
        n_before = len(session.branches)

        # Now try to resample auditor T2 on the FROZEN base — base never ran
        # T2, so its tape has only 2 auditor generates (indices 0, 1).
        conn = FakeConn()
        session.connections.append(conn)
        await _dispatch(
            session, {"t": "resample_auditor", "branch": "base", "turn_index": 2}
        )
        errs = [m for m in conn.sent if m["t"] == "error"]
        assert len(errs) == 1, (
            f"expected 1 error for unreached turn; got {[m['t'] for m in conn.sent]}"
        )
        assert "out of range" in errs[0]["message"], errs[0]["message"]
        assert len(session.branches) == n_before, (
            f"a child was spawned for an unreachable anchor "
            f"({len(session.branches)} vs {n_before})"
        )
        assert session.current == c_id, (
            f"current changed on failed fork: {session.current!r}"
        )
    finally:
        await session.close()


# ── C11: P1.8(c) live per-turn scanners ────────────────────────────────────
# Branch with `live_scanners=["marks_r2"]`; run 2 target turns. `post_turn`
# fires the scanner on each target reply (fire-and-forget), emitting a
# pending `turn_score` InfoEvent then updating it in place with the score.
# Assert: one resolved score per target anchor, routed to the target column,
# `score` matches (r1 → 0.0, r2 → 1.0).
#
# The scanner is loaded from a temp file (P1.8(a) library path) rather than
# defined inline: this module has ``from __future__ import annotations`` so
# an inline scanner's param annotation would be the *string* "Transcript",
# which scout's ``create_implicit_loader`` (raw ``inspect.signature``, not
# ``get_type_hints``) can't resolve.


async def c11_live_scanners_post_turn() -> None:
    import shutil
    import tempfile
    import textwrap
    from dataclasses import replace
    from pathlib import Path

    from workbench import config
    from workbench.m1 import scanners as scanmod

    lib_dir = tempfile.mkdtemp(prefix="wb-c11-scanlib-")
    (Path(lib_dir) / "flags.py").write_text(
        textwrap.dedent("""
            from inspect_scout import Result, Transcript, scanner

            @scanner(messages="all")
            def marks_r2():
                async def scan(t: Transcript) -> Result:
                    return Result(
                        value=t.messages[-1].text == "r2",
                        explanation=f"saw {t.messages[-1].text!r}",
                    )
                return scan
        """)
    )
    prev_settings = config.settings
    config.settings = replace(config.settings, scanner_dir=lib_dir)
    scanmod._lib_cache = None

    session = Session()
    await session.start()
    try:
        base = Branch(
            session,
            "base",
            seed="live-scanner smoke",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            max_turns=3,
            auditor_model_args={"custom_outputs": auditor_by_turn(SCRIPT3)},
            target_model_args={
                "custom_outputs": target_by_last_user({"u1": "r1", "u2": "r2"})
            },
            live_scanners=["marks_r2"],
        )
        session.branches["base"] = base
        session.current = "base"
        base.play()
        task = asyncio.create_task(base.run())
        session.branch_tasks["base"] = task
        await task
        assert base.error is None, base.error

        # fire-and-forget: wait for outstanding score tasks to settle
        with anyio.fail_after(5.0):
            while base._score_tasks:
                await anyio.sleep(0.01)

        scores = [
            ev["data"]
            for ev in session.events.values()
            if ev.get("event") == "info"
            and isinstance(ev.get("data"), dict)
            and ev["data"].get("kind") == "turn_score"
        ]
        anchors = [
            s.anchor_id
            for s in base.audit_tape.log
            if s.source == TARGET_GEN_SOURCE and isinstance(s.value, ModelOutput)
        ]
        assert len(anchors) == 2, anchors
        by_anchor = {s["turn_uuid"]: s for s in scores}
        assert set(by_anchor) == set(anchors), (sorted(by_anchor), sorted(anchors))
        assert by_anchor[anchors[0]]["score"] == 0.0, by_anchor[anchors[0]]
        assert by_anchor[anchors[1]]["score"] == 1.0, by_anchor[anchors[1]]
        assert by_anchor[anchors[1]]["scanner"] == "marks_r2"
        assert "r2" in by_anchor[anchors[1]]["explanation"]
        for s in scores:
            assert s["error"] is None, s
        # routed to the target column (span_id → (branch, "target"))
        target_uuids = session.by_role.get(("base", "target"), [])
        for a in anchors:
            assert f"ts:base:{a}:marks_r2" in target_uuids, (
                f"turn_score for {a} not in by_role[target]"
            )
    finally:
        await session.close()
        config.settings = prev_settings
        scanmod._lib_cache = None
        shutil.rmtree(lib_dir, ignore_errors=True)


# ── runner ──────────────────────────────────────────────────────────────────

TESTS = [
    ("C1   rollback-in-prefix → edit after", c1_rollback_then_edit_after),
    ("C2   edit → rollback in regenerated suffix", c2_edit_then_rollback_in_regen),
    ("C3   P→C, switch P, P→D; switch stability", c3_branch_chain_switches),
    ("C4   inject on paused fork → first live turn", c4_inject_during_paused_replay),
    ("C5   edit_target_message on rolled-back msg", c5_edit_rolled_back_message),
    ("C6   resample → edit → resample (3-deep)", c6_resample_edit_resample_chain),
    ("C7   edit create_tool dict parameters", c7_edit_create_tool_parameters),
    ("C8   edit resume(prefill=…)", c8_edit_resume_prefill),
    ("C9   two rollbacks in prefix → edit", c9_two_rollbacks_then_edit),
    ("C10  fork frozen parent @ unreached turn", c10_fork_frozen_unreached_turn),
    ("C11  P1.8(c) live per-turn scanners", c11_live_scanners_post_turn),
]


if __name__ == "__main__":
    sys.exit(anyio.run(run_suite, TESTS))
