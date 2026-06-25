"""Wire-level e2e for the fork operations (W6–W10 of the design).

Each test builds a fresh `Session`, runs a deterministic 3-turn base branch
to completion, dispatches one fork command via `_dispatch`, plays the child
to completion, then asserts ONE thing: `normalize(session, child_id)` equals
a hardcoded full ordered event list.

These are written against the *intended* post-`EmittingTape` behaviour
(replayed prefix events appear with real content, one ModelEvent per turn,
in execution order). They may FAIL on the current backend; failures print
the actual `normalize()` output so the divergence is visible.

Run:  uv run python -m workbench._smoke_actions_wire
"""

from __future__ import annotations

import sys
import traceback

import anyio
from inspect_ai.model import ModelOutput

from workbench._smoke_fixtures import (
    _auditor_turn,
    _tc,
    auditor_by_turn,
    auditor_counted,
    make_base,
    normalize,
    run_child,
    target_by_last_user,
    target_counted,
)
from workbench.run import find_auditor_step
from workbench.server import _dispatch  # noqa: PLC2701
from workbench.session import Session

# ── shared base scenario ────────────────────────────────────────────────────
#
#   T0  set_system_message("sys") · send_message("u1") · resume  → target "r1"
#   T1  send_message("u2") · resume                               → target "r2"
#   T2  end_conversation
#
# `SCRIPT` is built once; `auditor_by_turn`/`auditor_counted` deep-copy entries
# per call so cross-branch reuse is safe.

SCRIPT: list[ModelOutput] = [
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
T1 = (
    ("send_message", frozenset({("message", "u2")})),
    ("resume", frozenset()),
)
T2 = (("end_conversation", frozenset()),)


def _send(msg: str) -> tuple:
    return (
        ("send_message", frozenset({("message", msg)})),
        ("resume", frozenset()),
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _first_target_anchor(branch) -> str:
    anchor = next(
        (
            s.anchor_id
            for s in branch.audit_tape.log
            if s.source == "Model.generate"
            and isinstance(s.value, ModelOutput)
            and s.anchor_id is not None
        ),
        None,
    )
    assert anchor is not None, "base produced no target ModelOutput step"
    return anchor


def _pool_user_id(session: Session, text: str) -> str:
    for m in session.pool:
        if m.role == "user" and m.text == text:
            assert m.id is not None
            return m.id
    raise AssertionError(f"no pool user message with text {text!r}")


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


# ── W6: branch (target inclusive) ───────────────────────────────────────────


async def w6_branch() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT),
            target_outputs=target_by_last_user({"u1": "r1", "u2": "r2"}),
            max_turns=3,
        )
        anchor = _first_target_anchor(base)
        await _dispatch(session, {"t": "branch", "branch": "base", "at": anchor})
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2),
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
            auditor_outputs=auditor_by_turn(SCRIPT),
            target_outputs=target_counted({"u1": ["r1", "r1-v2"], "u2": ["r2"]}),
            max_turns=3,
        )
        anchor = _first_target_anchor(base)
        await _dispatch(session, {"t": "resample", "branch": "base", "at": anchor})
        child_id, _ = await run_child(session)

        actual = normalize(session, child_id)
        expected = [
            ("auditor", "", T0),
            ("target", "r1-v2", ()),
            ("auditor", "", T1),
            ("target", "r2", ()),
            ("auditor", "", T2),
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
    ("auditor", "", T2),
]


async def _w8_one(cmd: str) -> list[tuple]:
    session = Session()
    await session.start()
    try:
        await make_base(
            session,
            auditor_outputs=auditor_counted(SCRIPT, alt={1: T1_ALT}),
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
    ("auditor", "", T2),
]


async def w9_edit_auditor_call() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-EDIT": "r2-EDIT"}
            ),
            max_turns=3,
        )
        _, step = find_auditor_step(base.audit_tape.log, 1)
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
            auditor_outputs=auditor_by_turn(SCRIPT),
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


# ── runner ──────────────────────────────────────────────────────────────────

TESTS = [
    ("W6  branch (target inclusive)", w6_branch),
    ("W7  resample (target exclusive)", w7_resample),
    ("W8  resample_auditor", w8_resample_auditor),
    ("W8b branch_auditor", w8_branch_auditor),
    ("W9  edit_auditor_call", w9_edit_auditor_call),
    ("W10 edit_target_message (=W9)", w10_edit_target_message),
]


async def _amain() -> int:
    failed = 0
    for name, fn in TESTS:
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
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


def main() -> None:
    sys.exit(anyio.run(_amain))


if __name__ == "__main__":
    main()
