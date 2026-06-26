"""Adversarial edge-cases for the branch/edit/resample machinery.

Goes after the corners W6–W10 don't reach: turn-0 edits, grandchild chains,
sibling forks, rollback-in-prefix, multi-send positional disambiguation,
L1 anchor stability across forks, fork-while-running, and re-editing an edit.

Each case builds a deterministic mockllm scenario, dispatches via `_dispatch`,
runs the child, and asserts on `normalize()` (full lineage list) or the
specific invariant where `normalize()` can't see it.

Run:  uv run python -m workbench._smoke_edge_cases
"""

from __future__ import annotations

import asyncio
import sys

import anyio
from inspect_ai.model import ModelOutput

from workbench._smoke_fixtures import (
    SCRIPT3,
    T0,
    T1,
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
from workbench.run import TARGET_GEN_SOURCE, Branch, find_auditor_step
from workbench.server import _dispatch  # noqa: PLC2701
from workbench.session import Session, build_auditor_timeline


def _send_call_id(branch: Branch, turn_index: int) -> str:
    step = find_auditor_step(branch.audit_tape.log, turn_index)
    assert isinstance(step.value, ModelOutput)
    return next(
        tc.id
        for tc in (step.value.message.tool_calls or [])
        if tc.function == "send_message"
    )


# ── E1: edit_auditor_call at turn 0 ─────────────────────────────────────────
# No shared auditor turn in the prefix; the inline divergent emit must carry
# the edited T0 and the splice gate must already be open when it does.


async def e1_edit_turn0() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u1-EDIT": "r1-EDIT"}
            ),
            max_turns=3,
        )
        # edit T0's send_message arg
        step = find_auditor_step(base.audit_tape.log, 0)
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
                "turn_index": 0,
                "call_id": call_id,
                "args": {"message": "u1-EDIT"},
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            (
                "auditor",
                "",
                (
                    ("set_system_message", frozenset({("system_message", "sys")})),
                    ("send_message", frozenset({("message", "u1-EDIT")})),
                    ("resume", frozenset()),
                ),
            ),
            ("target", "r1-EDIT", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)

        # Prefix has no auditor turn → splice anchor is None; parent is base.
        assert child.parent_id == "base", f"parent_id={child.parent_id!r}"
        assert child.branched_at is None, f"branched_at={child.branched_at!r}"

        # `branched_at_turn` is documented as the count of GEN_SOURCE steps in
        # the *replayed prefix* — which is 0 here. The current implementation
        # counts over `pending` (which includes the appended edited step), so
        # this exposes the off-by-one for every `edit_*` op.
        assert child.branched_at_turn == 0, (
            f"branched_at_turn={child.branched_at_turn!r}; expected 0 "
            f"(prefix_len={child.shared_prefix_len}, no GEN_SOURCE in prefix)"
        )
    finally:
        await session.close()


# ── E2: grandchild edit (P → C → D) ─────────────────────────────────────────


async def e2_grandchild_edit() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-C": "r2-C", "u2-D": "r2-D"}
            ),
            max_turns=3,
        )
        # C: edit base's T1 send_message → "u2-C"
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 1,
                "call_id": _send_call_id(base, 1),
                "args": {"message": "u2-C"},
            },
        )
        c_id, c = await run_child(session)

        # D: edit C's T1 (the edited step, fresh on C) → "u2-D"
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": c_id,
                "turn_index": 1,
                "call_id": _send_call_id(c, 1),
                "args": {"message": "u2-D"},
            },
        )
        d_id, d = await run_child(session)

        actual = normalize(session, d_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-D")),
            ("target", "r2-D", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        # D's L2 parent is C (the edited T1 anchor lives in C's fresh steps).
        assert d.parent_id == c_id, f"D.parent_id={d.parent_id!r}, expected {c_id!r}"
    finally:
        await session.close()


# ── E3: sibling forks at the same anchor ────────────────────────────────────


async def e3_sibling_resamples() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_counted(
                SCRIPT3,
                alt={
                    1: _auditor_turn(_tc("send_message", message="u2-B"), _tc("resume"))
                },
            ),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-B": "r2-B"}
            ),
            max_turns=3,
        )
        await _dispatch(
            session, {"t": "resample_auditor", "branch": "base", "turn_index": 1}
        )
        c_id, c = await run_child(session)
        await _dispatch(
            session, {"t": "resample_auditor", "branch": "base", "turn_index": 1}
        )
        d_id, d = await run_child(session)

        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-B")),
            ("target", "r2-B", ()),
            ("auditor", "", T2_END),
        ]
        for bid in (c_id, d_id):
            actual = normalize(session, bid)
            assert actual == expected, f"[{bid}]" + _diff(actual, expected)

        # Both attach under base in the L2 tree, same auditor splice anchor.
        assert c.parent_id == "base" and d.parent_id == "base", (
            f"C.parent={c.parent_id!r} D.parent={d.parent_id!r}"
        )
        tl = build_auditor_timeline(session)
        base_span = next(s for s in tl["root"]["branches"] if s["id"] == "base")
        kids = {s["id"]: s for s in base_span["branches"]}
        assert c_id in kids and d_id in kids, (
            f"timeline siblings under base: {sorted(kids)}; expected {c_id!r}, {d_id!r}"
        )
        assert (
            kids[c_id]["branched_from"] == kids[d_id]["branched_from"]
            and kids[c_id]["branched_from"] is not None
        ), (
            f"sibling auditor_branched_from differ: "
            f"{kids[c_id]['branched_from']!r} vs {kids[d_id]['branched_from']!r}"
        )
    finally:
        await session.close()


# ── E4: rollback in shared prefix, edit a turn after it ─────────────────────


SCRIPT_RB: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u2"), _tc("resume")),
    # M1=sys M2=u1 M3=r1 — roll back to r1, drop r2
    _auditor_turn(
        _tc("rollback_conversation", message_id="M3"),
        _tc("send_message", message="u3"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("send_message", message="u4"), _tc("resume")),
    _auditor_turn(_tc("end_conversation")),
]

T2_RB = (
    ("rollback_conversation", frozenset({("message_id", "M3")})),
    ("send_message", frozenset({("message", "u3")})),
    ("resume", frozenset()),
)


async def e4_rollback_prefix_then_edit() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_RB),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u3": "r3", "u4": "r4", "u4-EDIT": "r4-EDIT"}
            ),
            max_turns=5,
        )
        assert _count_trajectories(base.history) == 2, (
            f"base L1 history should have 2 trajectories (rollback fired); "
            f"got {_count_trajectories(base.history)}"
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 3,
                "call_id": _send_call_id(base, 3),
                "args": {"message": "u4-EDIT"},
            },
        )
        child_id, child = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2_RB),
            ("target", "r3", ()),
            ("auditor", "", _send("u4-EDIT")),
            ("target", "r4-EDIT", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
        # Replaying the shared prefix re-ran the rollback on a fresh L1
        # history → child sees the same 2-trajectory shape.
        assert _count_trajectories(child.history) == 2, (
            f"child L1 history should have 2 trajectories; "
            f"got {_count_trajectories(child.history)}"
        )
    finally:
        await session.close()


# ── E5: two send_message in one turn → edit the second positionally ─────────


SCRIPT_MULTI: list[ModelOutput] = [
    _auditor_turn(
        _tc("set_system_message", system_message="sys"),
        _tc("send_message", message="u1"),
        _tc("resume"),
    ),
    # Two staged USER messages — the singleton slot keeps the *second*, so
    # the target sees "b". `locate_staging_call` must map "b"'s anchor to
    # calls[2] (positional), not calls[1].
    _auditor_turn(
        _tc("send_message", message="a"),
        _tc("send_message", message="b"),
        _tc("resume"),
    ),
    _auditor_turn(_tc("end_conversation")),
]


async def e5_multi_send_edit_second() -> None:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_MULTI),
            target_outputs=target_by_last_user(
                {"u1": "r1", "b": "rb", "b-EDIT": "rb-EDIT"}
            ),
            max_turns=3,
        )
        msg_id = _pool_user_id(session, "b")
        await _dispatch(
            session,
            {
                "t": "edit_target_message",
                "branch": "base",
                "message_id": msg_id,
                "role": "user",
                "content": "b-EDIT",
            },
        )
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            (
                "auditor",
                "",
                (
                    ("send_message", frozenset({("message", "a")})),
                    ("send_message", frozenset({("message", "b-EDIT")})),
                    ("resume", frozenset()),
                ),
            ),
            ("target", "rb-EDIT", ()),
            ("auditor", "", T2_END),
        ]
        assert actual == expected, _diff(actual, expected)
    finally:
        await session.close()


# ── E6: L1 anchor stability across sibling forks ────────────────────────────
# Two siblings forked inclusively at the *last* target step → the only live
# turn is `end_conversation` (no `_gen_id` call, no target generate). Every
# anchored step on each child's L1 root tape is therefore prefix-determined,
# so the two anchor sequences must be identical (and equal the parent's).


async def e6_anchor_stability() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        anchor = _nth_target_anchor(base.audit_tape.log, 1)  # r2

        await _dispatch(session, {"t": "branch", "branch": "base", "at": anchor})
        c_id, c = await run_child(session)
        await _dispatch(session, {"t": "branch", "branch": "base", "at": anchor})
        d_id, d = await run_child(session)

        def l1_anchors(b: Branch) -> list[str | None]:
            return [s.anchor_id for s in b.history.root.tape.log]

        ca, da, pa = l1_anchors(c), l1_anchors(d), l1_anchors(base)
        assert ca == da, (
            f"L1 anchor sequences differ across siblings\n"
            f"  C ({c_id}): {ca}\n  D ({d_id}): {da}"
        )
        # Both should reproduce the parent's L1 anchors up to the shared prefix
        # (parent has one more step — the End command).
        assert ca == pa[: len(ca)], (
            f"child L1 anchors don't match parent prefix\n"
            f"  child:  {ca}\n  parent: {pa[: len(ca)]}"
        )
    finally:
        await session.close()


# ── E7: fork while parent is still running ──────────────────────────────────


async def e7_fork_while_running() -> None:
    session = Session()
    await session.start()
    try:
        base = Branch(
            session,
            "base",
            seed="edge-suite",
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
        session.branch_tasks.append(task)

        # Release T0 + T1; T2 stays gated (parent task alive, blocked).
        # Poll on the *target* step landing — that's the last L2 append of
        # the turn (after `execute_tools`), so the parent is provably back
        # at the gate before we fork.
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
        assert not task.done(), "parent task ended early"

        anchor = _nth_target_anchor(base.audit_tape.log, 0)  # r1
        await _dispatch(session, {"t": "resample", "branch": "base", "at": anchor})
        # `_register_and_spawn` cancelled the parent before spawning the child.
        assert task.done(), "parent task should be cancelled by _stop_running_branches"
        assert base.status == "ended", f"parent status={base.status!r}"

        frozen = normalize(session, "base")
        expected_frozen = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
        ]
        assert frozen == expected_frozen, _diff(frozen, expected_frozen)

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


# ── E8: edit, then edit the edit (grandchild via two edit_auditor_call) ─────


async def e8_edit_the_edit() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-A": "r2-A", "u2-B": "r2-B"}
            ),
            max_turns=3,
        )
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": "base",
                "turn_index": 1,
                "call_id": _send_call_id(base, 1),
                "args": {"message": "u2-A"},
            },
        )
        c_id, c = await run_child(session)
        actual_c = normalize(session, c_id)
        expected_c = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-A")),
            ("target", "r2-A", ()),
            ("auditor", "", T2_END),
        ]
        assert actual_c == expected_c, _diff(actual_c, expected_c)

        # Re-edit C's T1 (the just-edited step, fresh on C).
        await _dispatch(
            session,
            {
                "t": "edit_auditor_call",
                "branch": c_id,
                "turn_index": 1,
                "call_id": _send_call_id(c, 1),
                "args": {"message": "u2-B"},
            },
        )
        d_id, d = await run_child(session)
        actual_d = normalize(session, d_id)
        expected_d = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", _send("u2-B")),
            ("target", "r2-B", ()),
            ("auditor", "", T2_END),
        ]
        assert actual_d == expected_d, _diff(actual_d, expected_d)
        assert d.parent_id == c_id, f"D.parent_id={d.parent_id!r}, expected {c_id!r}"
        # D's prefix == C's prefix (both cut just before T1); the re-edit
        # didn't accidentally include C's edited T1 in D's shared prefix.
        assert d.shared_prefix_len == c.shared_prefix_len, (
            f"D.prefix_len={d.shared_prefix_len} C.prefix_len={c.shared_prefix_len}"
        )
    finally:
        await session.close()


# ── runner ──────────────────────────────────────────────────────────────────

TESTS = [
    ("E1  edit_auditor_call @ turn 0", e1_edit_turn0),
    ("E2  grandchild edit (P→C→D)", e2_grandchild_edit),
    ("E3  sibling resample_auditor", e3_sibling_resamples),
    ("E4  rollback in prefix + edit after", e4_rollback_prefix_then_edit),
    ("E5  multi-send_message positional edit", e5_multi_send_edit_second),
    ("E6  L1 anchor stability across siblings", e6_anchor_stability),
    ("E7  fork while parent running", e7_fork_while_running),
    ("E8  edit then edit-the-edit", e8_edit_the_edit),
]


if __name__ == "__main__":
    sys.exit(anyio.run(run_suite, TESTS))
