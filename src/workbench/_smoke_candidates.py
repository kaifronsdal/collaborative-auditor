"""Wire-level e2e for Resample-N / candidates picker (RESAMPLE-N.md).

Each test builds a fresh `Session`, runs the canonical 3-turn base branch
to completion, dispatches one `candidates*` command, then asserts on
`session.candidate_batches` / `branch_tasks` / `normalize()`.

Coverage:
  N1  candidates n=3 → 3 background branches; `current` unchanged; batch recorded
  N2  each candidate's tape == parent prefix + one fresh target turn
  N3  pick_candidate → `current` switches; siblings cancelled; `picked` set
  N4  dismiss_candidates → all cancelled; `current` stays parent; `picked == parent`
  N5  candidates_auditor n=2 → exactly one fresh auditor turn past the branch point

Run:  uv run python -m workbench._smoke_candidates
"""

from __future__ import annotations

import sys

import anyio
from inspect_ai.model import ModelOutput

from workbench._smoke_fixtures import (
    SCRIPT3,
    T0,
    _auditor_turn,
    _diff,
    _nth_target_anchor,
    _send,
    _tc,
    auditor_by_turn,
    auditor_counted,
    make_base,
    normalize,
    run_suite,
    target_by_last_user,
    target_counted,
)
from workbench.run import Branch
from workbench.server import _dispatch
from workbench.session import Session
from workbench.sources import GEN_SOURCE


async def _settle(branches: list[Branch]) -> None:
    """Wait for every candidate's prefix replay (and any post-replay
    `step()`) to land. `_candidates` doesn't await `_replayed`, so the
    test must — model latency × N is zero with mockllm."""
    for b in branches:
        with anyio.move_on_after(5.0):
            await b._replayed.wait()
    # one more tick for `_step_after_replay`'s `step()` → live generate
    await anyio.sleep(0.05)


async def _spawn_candidates(
    session: Session, base: Branch, *, n: int
) -> tuple[str, list[Branch]]:
    anchor = _nth_target_anchor(base.audit_tape.log, 0)
    await _dispatch(
        session, {"t": "candidates", "branch": "base", "at": anchor, "n": n}
    )
    [(batch_id, batch)] = session.candidate_batches.items()
    children = [session.branches[c] for c in batch.children]
    await _settle(children)
    return batch_id, children


# ── N1: candidates n=3 → background, `current` unchanged ───────────────────


async def n1_spawn_background() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted(
                {"u1": ["r1", "r1-a", "r1-b", "r1-c"], "u2": ["r2"]}
            ),
            max_turns=3,
        )
        before = set(session.branches)
        anchor = _nth_target_anchor(base.audit_tape.log, 0)
        await _dispatch(
            session, {"t": "candidates", "branch": "base", "at": anchor, "n": 3}
        )

        # `current` unchanged; parent untouched (its task already completed —
        # `make_base` ran it to end — but `_candidates` would not have stopped
        # a live one either: it never calls `_stop_running_branches`).
        assert session.current == "base", f"current repointed to {session.current!r}"
        new = set(session.branches) - before
        assert len(new) == 3, f"expected 3 new branches, got {len(new)}"
        for cid in new:
            assert cid in session.branch_tasks, f"{cid} not spawned"
            assert session.branches[cid].meta.batch is not None

        assert len(session.candidate_batches) == 1
        [(batch_id, batch)] = session.candidate_batches.items()
        assert batch.parent == "base"
        assert batch.anchor == anchor
        assert batch.kind == "target"
        assert set(batch.children) == new
        assert batch.picked is None
        view = session.view()
        assert batch_id in view["candidate_batches"]
    finally:
        await session.close()


# ── N2: prefix shared, divergence at exactly the anchor turn ────────────────


async def n2_prefix_shared() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted(
                {"u1": ["r1", "r1-a", "r1-b", "r1-c"], "u2": ["r2"]}
            ),
            max_turns=3,
        )
        _, children = await _spawn_candidates(session, base, n=3)

        # Parent's tape up to (and excluding) the resampled target step.
        prefix = [("auditor", "", T0)]
        seen: set[str] = set()
        for c in children:
            actual = normalize(session, c.branch_id)
            assert actual[: len(prefix)] == prefix, _diff(actual[: len(prefix)], prefix)
            # Exactly one fresh target turn past the prefix; gate parked.
            tail = actual[len(prefix) :]
            assert len(tail) == 1 and tail[0][0] == "target", _diff(actual, prefix)
            assert tail[0][1] in {"r1-a", "r1-b", "r1-c"}, f"divergent={tail[0][1]!r}"
            seen.add(tail[0][1])
            assert not c._free_running, "candidate should not autoplay"
        assert seen == {"r1-a", "r1-b", "r1-c"}, f"candidates not distinct: {seen}"
    finally:
        await session.close()


# ── N3: pick_candidate → switch + stop-the-rest ─────────────────────────────


async def n3_pick() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted(
                {"u1": ["r1", "r1-a", "r1-b", "r1-c"], "u2": ["r2"]}
            ),
            max_turns=3,
        )
        batch_id, children = await _spawn_candidates(session, base, n=3)
        picked = children[1].branch_id
        others = [c.branch_id for c in children if c.branch_id != picked]

        await _dispatch(
            session, {"t": "pick_candidate", "batch": batch_id, "branch": picked}
        )
        assert session.current == picked, f"current={session.current!r}"
        assert session.candidate_batches[batch_id].picked == picked
        # Siblings' tasks cancelled (removed from `branch_tasks`); picked's
        # task survives. Their `Trajectory` nodes stay in `audit_history`.
        assert picked in session.branch_tasks
        for cid in others:
            assert cid not in session.branch_tasks, f"{cid} task survived pick"
            assert cid in session.branches, f"{cid} dropped from branches"
            assert (
                session.branches[cid].trajectory.parent is base.trajectory
            ), f"{cid} unlinked from audit_history"
    finally:
        await session.close()


# ── N4: dismiss_candidates → all cancelled, parent wins ─────────────────────


async def n4_dismiss() -> None:
    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT3),
            target_outputs=target_counted(
                {"u1": ["r1", "r1-a", "r1-b", "r1-c"], "u2": ["r2"]}
            ),
            max_turns=3,
        )
        batch_id, children = await _spawn_candidates(session, base, n=3)

        await _dispatch(session, {"t": "dismiss_candidates", "batch": batch_id})
        assert session.current == "base", f"current moved to {session.current!r}"
        assert session.candidate_batches[batch_id].picked == "base"
        for c in children:
            assert c.branch_id not in session.branch_tasks
            assert c.status == "ended"
    finally:
        await session.close()


# ── N5: candidates_auditor n=2 → one fresh auditor turn each ────────────────


async def n5_auditor() -> None:
    session = Session()
    await session.start()
    try:
        alt = _auditor_turn(_tc("send_message", message="u2-alt"), _tc("resume"))
        await make_base(
            session,
            auditor_outputs=auditor_counted(SCRIPT3, alt={1: alt}),
            target_outputs=target_by_last_user(
                {"u1": "r1", "u2": "r2", "u2-alt": "r2-alt"}
            ),
            max_turns=3,
        )
        await _dispatch(
            session,
            {"t": "candidates_auditor", "branch": "base", "turn_index": 1, "n": 2},
        )
        [(_, batch)] = session.candidate_batches.items()
        assert batch.kind == "auditor"
        children = [session.branches[c] for c in batch.children]
        await _settle(children)

        for c in children:
            tail = c.audit_tape.log[c.shared_prefix_len :]
            n_aud = sum(
                1
                for s in tail
                if s.source == GEN_SOURCE and isinstance(s.value, ModelOutput)
            )
            assert n_aud == 1, (
                f"{c.branch_id}: {n_aud} auditor turns past prefix (want 1)"
            )
            actual = normalize(session, c.branch_id)
            prefix = [("auditor", "", T0), ("target", "r1", ())]
            assert actual[:2] == prefix, _diff(actual[:2], prefix)
            # Divergent auditor turn is `alt` (both children — `auditor_counted`
            # serves `alt[1]` for every call after the first at turn 1).
            assert actual[2] == ("auditor", "", _send("u2-alt")), _diff(
                actual[2:3], [("auditor", "", _send("u2-alt"))]
            )
        assert session.current == "base"
    finally:
        await session.close()


TESTS = [
    ("N1  candidates n=3 background, current unchanged", n1_spawn_background),
    ("N2  candidates share prefix, diverge at anchor", n2_prefix_shared),
    ("N3  pick_candidate switches + stops siblings", n3_pick),
    ("N4  dismiss_candidates → parent wins", n4_dismiss),
    ("N5  candidates_auditor → one fresh auditor turn", n5_auditor),
]


if __name__ == "__main__":
    sys.exit(anyio.run(run_suite, TESTS))
