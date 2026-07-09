"""A1-b-wide graft stress: `build_target_timeline` under concurrent branches.

C16 (extends `_smoke_combos` C14/C15 to N concurrent forks + mid-replay
polling): does the session-wide event-sourced graft stay consistent when
5 `_candidates` children are simultaneously replaying an L2 prefix that
itself contains an L1 rollback, while a *further* L2 fork is spawned
mid-window at a lane-A anchor?

Scenario (all mockllm; async target callable staggers the excluded live
generate so `by_role` mutates across the 10×50ms poll window):

    base  T0 set_sys+u1 → r1  (lane A)
          T1 u2         → r2  (lane A)
          T2 rb(M3)+u3  → r3  (lane B; L1 rollback @t2)
          T3 u4         → r4  (lane B; anchor for candidates = t3′)
          T4 end
    → candidates(at=r4, n=5): each replays T0..T3 (rollback re-runs on a
      fresh L1 → A′ never-live, B′ live at r4-fresh-i), staggered 140–380ms
    → mid-poll: `Branch.fork(base, at=r2, exclusive)` — L2 fork in lane A
      *before* the rollback; child's L1-root goes live at r2-fresh
    poll `_target_timeline()` 10× @50ms while all of that mutates `by_role`

Asserts (per ARCHITECTURE-RACES.md §A1-b-wide invariant):
  - every poll returns without raising; every non-root span's
    `branchedFrom` is an `AnchorEvent.anchor_id` present in its
    `.branches`-parent's `content` (the `splice()` invariant)
  - determinism: two back-to-back builds over the same session state
    are byte-identical
  - final tree = base-A + base-B + 5×candidate-B′ (grafted under base-B
    at r3) + fork-A″ (grafted under base-A at r1); every candidate-A′
    is dropped (no live `ModelEvent`)
  - timing baseline for one build at ≥8 spans

STRICT scope: this file adds no production code; failures are reported,
not fixed.

Run:  uv run python -m workbench._smoke_target_timeline_stress
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

import anyio
from inspect_ai.model import ChatMessage, GenerateConfig, ModelOutput
from inspect_ai.tool import ToolChoice, ToolInfo

from workbench._smoke_combos import _content_anchors, _find_in_tl
from workbench._smoke_fixtures import (
    _auditor_turn,
    _count_trajectories,
    _nth_target_anchor,
    _target,
    _tc,
    auditor_by_turn,
    make_base,
    run_suite,
)
from workbench.run import Branch
from workbench.server import _dispatch
from workbench.session import Session
from workbench.timeline import build_target_timeline

# ── scenario ────────────────────────────────────────────────────────────────

# M-numbering (petri `Controller.short_id`): sys→M1, u1→M2, r1→M3, u2→M4,
# r2→M5, … — so `rollback_conversation(M3)` keeps r1 and drops r2 (same
# convention as C15).
SCRIPT_S1: list[ModelOutput] = [
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

TABLE = {"u1": "r1", "u2": "r2", "u3": "r3", "u4": "r4"}


def _walk_tree(
    root: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Pre-order DFS over a target-timeline tree → [(span, parent), …]."""
    out: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    stack: list[tuple[dict[str, Any], dict[str, Any] | None]] = [(root, None)]
    while stack:
        sp, parent = stack.pop()
        out.append((sp, parent))
        for c in sp["branches"]:
            stack.append((c, sp))
    return out


def _splice_violations(
    session: Session, root: dict[str, Any]
) -> list[tuple[str, str, str, list[str]]]:
    """Every non-root span's `branched_from` must be an `AnchorEvent`
    `anchor_id` in its `.branches`-parent's `content` (core.ts:413).
    Returns [(span_id, branched_from, parent_id, sorted(parent_anchors))]
    for each violating span — empty on success."""
    bad: list[tuple[str, str, str, list[str]]] = []
    for sp, parent in _walk_tree(root):
        bf = sp["branched_from"]
        if bf is None:
            continue
        # A span with bf≠None whose graft parent was dropped/root: still
        # check — `splice()` would discard the prefix (bf=None case), but a
        # non-None bf under root would throw. `build_target_timeline` grafts
        # such spans under `root` (content=[]), so this IS a violation.
        assert parent is not None
        if bf not in _content_anchors(session, parent):
            bad.append(
                (sp["id"], bf, parent["id"], sorted(_content_anchors(session, parent)))
            )
    return bad


def _shape(root: dict[str, Any]) -> str:
    """One-line tree dump for failure messages."""
    lines: list[str] = []

    def go(sp: dict[str, Any], depth: int) -> None:
        lines.append(
            f"{'  ' * depth}{sp['id']}  bf={sp['branched_from']!r}  "
            f"|content|={len(sp['content'])}"
        )
        for c in sp["branches"]:
            go(c, depth + 1)

    go(root, 0)
    return "\n" + "\n".join(lines)


# ── S1: concurrent candidates × L1 rollback × mid-poll L2 fork ─────────────


async def s1_bwide_concurrent_graft_stress() -> None:
    # Shared stateful async target — closure inherited by every fork
    # (`Branch.fork` copies `target_model_args` verbatim). n=0 is base's
    # own generate (immediate); n>0 is a fork's live regenerate. u4/n>0
    # is a candidate's excluded step: stagger so `by_role` mutates across
    # the poll window. u2/n>0 is the mid-poll L2-fork's excluded step.
    counts: dict[str, int] = {}

    async def target_out(
        input: list[ChatMessage],  # noqa: A002
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        del tools, tool_choice, config
        last = next(m for m in reversed(input) if m.role in ("user", "tool")).text
        n = counts.get(last, 0)
        counts[last] = n + 1
        if n > 0 and last == "u4":
            await anyio.sleep(0.08 + 0.06 * n)  # 140/200/260/320/380 ms
        elif n > 0 and last == "u2":
            await anyio.sleep(0.05)
        return _target(TABLE[last] if n == 0 else f"{TABLE[last]}-{n}")

    session = Session()
    await session.start()
    try:
        base = await make_base(
            session,
            auditor_outputs=auditor_by_turn(SCRIPT_S1),
            target_outputs=target_out,
            max_turns=5,
        )
        assert _count_trajectories(base.history) == 2, (
            f"base L1 lanes={_count_trajectories(base.history)}; want 2 (rollback @t2)"
        )
        lane_a = base.history.root.span_id
        lane_b = base.history.root.children[0].span_id
        r1 = _nth_target_anchor(base.audit_tape.log, 0)
        r2 = _nth_target_anchor(base.audit_tape.log, 1)
        r3 = _nth_target_anchor(base.audit_tape.log, 2)
        r4 = _nth_target_anchor(base.audit_tape.log, 3)

        # ── spawn 5 concurrent candidates at t3′=r4 (lane B) ──
        # `candidates` → `_locate_resample` (exclusive of r4) → each child
        # L2-replays T0..T3 (T2's rollback re-runs on a fresh L1 → A′/B′),
        # then r4 regenerates LIVE in B′ (staggered). No `_stop_running`.
        await _dispatch(
            session, {"t": "candidates", "branch": "base", "at": r4, "n": 5}
        )
        [(_batch_id, batch)] = session.candidate_batches.items()
        cands = [session.branches[c] for c in batch.children]
        assert len(cands) == 5

        # ── poll `_target_timeline()` 10× @50ms while candidates mid-replay ──
        polls: list[dict[str, Any]] = []
        span_counts: list[int] = []
        fork_child: Branch | None = None
        for i in range(10):
            # Determinism: two builds over identical state (no await
            # between → nothing else can mutate `by_role`/`events`) must
            # be structurally equal. `anchor_owner` is rebuilt from
            # `session.branches`-insertion-order each call.
            tl1 = build_target_timeline(session)["root"]
            tl2 = build_target_timeline(session)["root"]
            assert tl1 == tl2, (
                f"poll {i}: nondeterministic build (same by_role/events "
                f"snapshot, different tree):\n"
                f"  first:  {_shape(tl1)}\n  second: {_shape(tl2)}"
            )
            v = _splice_violations(session, tl1)
            assert not v, (
                f"poll {i}: splice() invariant violated — "
                f"[(span, bf, parent, parent_anchors)] = {v}\n"
                f"tree: {_shape(tl1)}"
            )
            polls.append(tl1)
            span_counts.append(len(_walk_tree(tl1)) - 1)  # exclude wrapper root

            # Mid-window: L2-fork base at t1=r2 (lane A, *before* the
            # rollback). `Branch.fork` + manual spawn — NOT via `_dispatch`
            # (`{"t":"branch"}` would `_stop_running_branches` and cancel
            # the 5 in-flight candidates, defeating the test).
            if i == 4:
                fork_child = Branch.fork(session, base, anchor=r2, inclusive=False)
                session.branches[fork_child.branch_id] = fork_child
                session.branch_tasks[fork_child.branch_id] = asyncio.create_task(
                    fork_child.run()
                )

            await anyio.sleep(0.05)

        # Sanity: the poll window actually straddled mutation — at least
        # one intermediate poll saw fewer spans than the final settled
        # tree, and at least one poll saw more than base's 2 lanes.
        assert min(span_counts) < max(span_counts), (
            f"poll window missed all mutation (span counts flat at "
            f"{span_counts}); tighten target_out stagger"
        )

        # ── settle: candidates' `_replayed` (r4-fresh landed) + fork ──
        with anyio.fail_after(5.0):
            for c in cands:
                await c._replayed.wait()
            assert fork_child is not None
            await fork_child._replayed.wait()
        await anyio.sleep(0.02)  # trailing AnchorEvent after ModelEvent

        # No branch's inline `_on_event → _target_timeline()` threw.
        for b in [*cands, fork_child]:
            assert b.error is None, (
                f"branch {b.branch_id} errored during concurrent replay "
                f"(likely _target_timeline raised in _on_event): {b.error}"
            )
            assert _count_trajectories(b.history) == (2 if b in cands else 1), (
                f"{b.branch_id}: L1 lanes={_count_trajectories(b.history)}"
            )

        # ── final tree structure ──
        tl = build_target_timeline(session)["root"]
        v = _splice_violations(session, tl)
        assert not v, f"FINAL splice() invariant violated — {v}\ntree: {_shape(tl)}"
        spans = {sp["id"]: (sp, parent) for sp, parent in _walk_tree(tl)}
        n_spans = len(spans) - 1  # exclude wrapper
        # base-A + base-B + 5×candidate-B′ + fork-A″ = 8. Every candidate-A′
        # (L1 root) has no live `ModelEvent` (all L2-served/L1-replayed) and
        # is dropped.
        assert n_spans == 8, (
            f"final span count {n_spans}; want 8 (base×2 + cand-B′×5 + "
            f"fork×1). tree: {_shape(tl)}"
        )
        # base-A grafts under wrapper root; base-B under base-A.
        assert spans[lane_a][1]["id"] == "target-root"
        assert spans[lane_b][1]["id"] == lane_a, (
            f"base lane-B grafted under {spans[lane_b][1]['id']!r}; want "
            f"lane-A {lane_a!r}"
        )
        # Every candidate: A′ dropped; B′'s boundary-traj grafts under
        # base-B (the lane containing t3′=r4's live `ModelEvent`) at
        # `branchedFrom=r3` — the last L2-served target anchor in B′'s
        # pre-live prefix, owned by base-B via `anchor_owner[r3]`.
        for c in cands:
            c_a = c.history.root.span_id
            c_b = c.history.root.children[0].span_id
            assert c_a not in spans, (
                f"candidate {c.branch_id} lane-A′ ({c_a!r}) has no live "
                f"ModelEvent and must be dropped. tree: {_shape(tl)}"
            )
            hit = _find_in_tl(tl, c_b)
            assert hit is not None, f"candidate B′ {c_b!r} absent from tree"
            b_span, b_parent = hit
            assert b_parent is not None and b_parent["id"] == lane_b, (
                f"candidate {c.branch_id} B′ grafted under "
                f"{b_parent['id'] if b_parent else None!r}; want base "
                f"lane-B {lane_b!r} (anchor_owner[r3]). tree: {_shape(tl)}"
            )
            assert b_span["branched_from"] == r3, (
                f"candidate B′ branched_from={b_span['branched_from']!r}; "
                f"want r3={r3!r}"
            )
        # L2-fork child (at r2, lane A, before rollback): its L1-root grafts
        # under base-A at `branchedFrom=r1` — spawned while 5 other
        # branches were mid-replay mutating `by_role`.
        f_root = fork_child.history.root.span_id
        hit = _find_in_tl(tl, f_root)
        assert hit is not None, f"L2-fork L1-root {f_root!r} absent from tree"
        f_span, f_parent = hit
        assert f_parent is not None and f_parent["id"] == lane_a, (
            f"L2-fork grafted under {f_parent['id'] if f_parent else None!r}; "
            f"want base lane-A {lane_a!r} (anchor_owner[r1] — fork at t1 is "
            f"before the rollback). tree: {_shape(tl)}"
        )
        assert f_span["branched_from"] == r1, (
            f"L2-fork branched_from={f_span['branched_from']!r}; want r1={r1!r}"
        )

        # ── timing baseline ──
        n_events = sum(len(sp["content"]) for sp, _ in _walk_tree(tl))
        t0 = time.perf_counter()
        for _ in range(20):
            build_target_timeline(session)
        per_call_ms = (time.perf_counter() - t0) / 20 * 1000
        print(
            f"\n      _target_timeline: {n_spans} spans, {n_events} events, "
            f"{len(session.branches)} branches → {per_call_ms:.3f} ms/call "
            f"(poll span-counts: {span_counts})"
        )
    finally:
        await session.close()


TESTS = [
    (
        "S1  b-wide graft: 5 concurrent candidates × L1 rollback × mid-poll L2 fork",
        s1_bwide_concurrent_graft_stress,
    ),
]


if __name__ == "__main__":
    sys.exit(anyio.run(run_suite, TESTS))
