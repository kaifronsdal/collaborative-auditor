"""Deep branching e2e test for the audit-workbench (INSPECT-REUSE.md §4).

Goes beyond `_smoke_branch.py` by exercising the historically-fragile paths:

  Scenario A — auditor (level-2) branch chain b1 → b2 → b3, asserting the
  wire-protocol invariants that make lost/dup events structurally impossible:
    A1. every `{t:"event"}` uuid is globally unique across the whole session
    A2. every `{t:"update"}` uuid was first introduced by `event`/`state`
    A3. every event's span_id resolves to exactly one (branch, role)
    A4. no ModelEvent uuid lands under more than one branch's column
    A5. each branch's auditor + target columns are non-empty
    A6. b1/b2/b3 stores are pairwise distinct (footgun #5)
    A7. b3's prefix length is consistent with slice_at over b2's log

  Scenario B — target (level-1) rollback inside a single branch: the auditor
  is instructed to call `rollback_conversation` mid-run. Asserts:
    B1. ≥2 target trajectories exist (rollback actually happened)
    B2. ≥1 `BranchEvent` and ≥1 `AnchorEvent` emitted under the target span
    B3. target ModelEvent uuids are unique (no re-emission of pre-rollback turn)
    B4. both trajectories' events route to the same (branch, "target") column

Captures from Scenario A are dumped to `frontend-wb/fixtures/smoke_branch_deep.json`
for the frontend store's multi-branch replay test.

Run:  uv run python -m workbench._smoke_branch_deep
"""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path

import anyio
from inspect_ai.model import ModelOutput

from workbench._smoke_util import (
    FakeConn,
    resolve_role as _resolve_branch_role,
    wire_events as _events,
)
from workbench.run import Branch, slice_at
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED_A = "test seed"
SEED_B = (
    "Send the target one very short greeting (under 10 words). "
    "Then immediately call the rollback_conversation tool with the message id "
    "of the assistant reply you just received, to roll the target back to before it. "
    "Then send the target a different very short greeting. Then call end_conversation."
)


def _nth_target_anchor(log: list, n: int) -> str:
    """anchor_id of the n-th (1-based) target ModelOutput step in `log`."""
    targets = [s for s in log if s.source == "Model.generate" and s.value is not None]
    assert len(targets) >= n, f"need ≥{n} target steps, got {len(targets)}"
    anchor = targets[n - 1].anchor_id
    assert anchor is not None, f"target step {n} has no anchor_id"
    return anchor


def _count_trajectories(history) -> int:
    n = 0
    q = deque([history.root])
    while q:
        t = q.popleft()
        n += 1
        q.extend(t.children)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Scenario A — level-2 branch chain
# ─────────────────────────────────────────────────────────────────────────────


async def scenario_a(dump_path: Path | None) -> None:
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    await session.push_full_state(conn)

    # b1: 6 turns so we reliably get ≥2 target steps to slice from. Without
    # petri's eager_resume (intentionally off in workbench_auditor) the
    # auditor occasionally stages-without-resume and that turn yields no
    # target step.
    b1 = Branch(
        session, "b1", seed=SEED_A, auditor_model=MODEL, target_model=MODEL, max_turns=6
    )
    session.branches["b1"] = b1
    session.current = "b1"
    b1.play()
    await b1.run()

    anchor2 = _nth_target_anchor(b1.audit_tape.log, 2)
    prefix2 = slice_at(b1.audit_tape.log, anchor2)
    b2 = Branch(
        session,
        "b2",
        seed=SEED_A,
        auditor_model=MODEL,
        target_model=MODEL,
        max_turns=4,
        resume=prefix2,
        parent_id="b1",
        branched_at=anchor2,
    )
    session.branches["b2"] = b2
    session.current = "b2"
    await session.push_full_state(conn)
    b2.play()
    await b2.run()

    anchor3 = _nth_target_anchor(b2.audit_tape.log, 1)
    prefix3 = slice_at(b2.audit_tape.log, anchor3)
    b3 = Branch(
        session,
        "b3",
        seed=SEED_A,
        auditor_model=MODEL,
        target_model=MODEL,
        max_turns=4,
        resume=prefix3,
        parent_id="b2",
        branched_at=anchor3,
    )
    session.branches["b3"] = b3
    session.current = "b3"
    await session.push_full_state(conn)
    b3.play()
    await b3.run()
    await session.close()

    # ── A1: every {t:"event"} uuid is globally unique ────────────────────────
    new_uuids: list[str] = [m["event"]["uuid"] for m in conn.sent if m["t"] == "event"]
    dup = {u for u in new_uuids if new_uuids.count(u) > 1}
    assert not dup, f"A1: duplicate event uuids on wire: {sorted(dup)[:5]}"

    # ── A2: every {t:"update"} uuid was previously introduced ────────────────
    introduced: set[str] = set()
    for m in conn.sent:
        if m["t"] == "state":
            introduced |= {e["uuid"] for e in m["events"] if e.get("uuid")}
        elif m["t"] == "event":
            introduced.add(m["event"]["uuid"])
        elif m["t"] == "update":
            u = m["event"]["uuid"]
            assert u in introduced, f"A2: update for never-introduced uuid {u}"

    # ── A3/A4/A5: routing + isolation + non-empty ────────────────────────────
    by_branch_role: dict[tuple[str, str], set[str]] = {}
    unrouted: list[str] = []
    for ev in _events(conn):
        if ev["event"] != "model":
            continue
        br = _resolve_branch_role(ev.get("span_id"), session)
        if br is None:
            unrouted.append(ev["uuid"])
            continue
        by_branch_role.setdefault(br, set()).add(ev["uuid"])
    assert not unrouted, f"A3: {len(unrouted)} ModelEvents have unroutable span_id"

    # A4: a uuid never appears under two different branches
    seen_in: dict[str, str] = {}
    for (bid, _), uuids in by_branch_role.items():
        for u in uuids:
            prev = seen_in.get(u)
            assert prev is None or prev == bid, (
                f"A4: ModelEvent {u} routed to both {prev} and {bid}"
            )
            seen_in[u] = bid

    # A5: every branch has both columns populated
    for bid in ("b1", "b2", "b3"):
        for role in ("auditor", "target"):
            uuids = by_branch_role.get((bid, role), set())
            assert uuids, f"A5: {bid}/{role} column is empty"

    # ── A6: store isolation across the chain ─────────────────────────────────
    assert (
        b1.store is not b2.store
        and b2.store is not b3.store
        and b1.store is not b3.store
    ), "A6: branch stores are not pairwise distinct"

    # ── A7: b3's prefix is consistent with slicing b2's log ──────────────────
    # _synthesize_prefix_events emits one ModelEvent per ModelOutput step only;
    # other value-bearing steps (channel receives, tool acks) are replayed
    # silently by the tape. Count just the ModelOutput steps.
    expected_b3_model_steps = sum(
        1 for s in prefix3 if isinstance(s.value, ModelOutput)
    )
    assert len(b3.resume or []) == len(prefix3), "A7: b3.resume length mismatch"
    synth_b3 = [
        ev
        for ev in _events(conn)
        if ev["event"] == "model"
        and _resolve_branch_role(ev.get("span_id"), session)
        in {("b3", "auditor"), ("b3", "target")}
        and ev["input"] == []
        and ev.get("input_refs") is None
    ]
    assert len({e["uuid"] for e in synth_b3}) == expected_b3_model_steps, (
        f"A7: b3 synthesised {len({e['uuid'] for e in synth_b3})} != "
        f"expected {expected_b3_model_steps} ModelOutput prefix steps"
    )

    print(
        f"A: branches=3 events={len(new_uuids)} "
        f"model_events={sum(len(v) for v in by_branch_role.values())} "
        f"b3_prefix_model_steps={expected_b3_model_steps}"
    )
    print("✓ scenario A (level-2 branch chain) passed")

    if dump_path is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(conn.sent, default=str))
        print(f"wrote {len(conn.sent)} messages → {dump_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario B — level-1 (target) rollback
# ─────────────────────────────────────────────────────────────────────────────


async def scenario_b(dump_path: Path | None = None) -> None:
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)

    b = Branch(
        session, "rb", seed=SEED_B, auditor_model=MODEL, target_model=MODEL, max_turns=6
    )
    session.branches["rb"] = b
    session.current = "rb"
    b.play()
    await b.run()
    await session.close()

    # B1: rollback created ≥2 target trajectories
    n_traj = _count_trajectories(b.history)
    rollback_calls = [
        s
        for s in b.audit_tape.log
        if s.source == "auditor:Model.generate"
        and s.value is not None
        and any(
            (tc.function == "rollback_conversation")
            for tc in (s.value.choices[0].message.tool_calls or [])
        )
    ]
    assert n_traj >= 2, (
        f"B1: target has {n_traj} trajectory (no rollback). "
        f"Auditor made {len(rollback_calls)} rollback_conversation calls — "
        f"if 0, the model ignored the seed; if ≥1, rollback was rejected "
        f"(check anchor_id validity)."
    )

    # B2: AnchorEvent + BranchEvent under target span
    target_evs = [
        ev
        for ev in _events(conn)
        if _resolve_branch_role(ev.get("span_id"), session) == ("rb", "target")
    ]
    anchors = [e for e in target_evs if e["event"] == "anchor"]
    branches = [e for e in target_evs if e["event"] == "branch"]
    assert anchors, (
        f"B2: no AnchorEvent under target span (got {len(target_evs)} target events)"
    )
    assert branches, "B2: no BranchEvent under target span"

    # B3: target ModelEvent uuids unique among *new* events (updates re-send the
    # same uuid by design — that's the streaming flush, not a duplicate emit).
    target_new = [
        ev
        for ev in _events(conn, kinds=("event",))
        if ev["event"] == "model"
        and _resolve_branch_role(ev.get("span_id"), session) == ("rb", "target")
    ]
    target_model_uuids = [e["uuid"] for e in target_new]
    dup = {u for u in target_model_uuids if target_model_uuids.count(u) > 1}
    assert not dup, (
        f"B3: duplicate target ModelEvent uuids after rollback: {sorted(dup)[:5]}"
    )

    # B4: every trajectory's span_id is registered under the target column,
    # so post-rollback events (when they exist) route to the same place as
    # pre-rollback. A trajectory may have zero ModelEvents if the auditor
    # rolled back then ended without re-sending — that's valid; we assert
    # routing-graph correctness, not model behaviour.
    q = deque([b.history.root])
    while q:
        traj = q.popleft()
        resolved = _resolve_branch_role(traj.span_id, session)
        assert resolved == ("rb", "target"), (
            f"B4: trajectory span {traj.span_id} routes to {resolved!r}, not ('rb','target')"
        )
        q.extend(traj.children)

    print(
        f"B: trajectories={n_traj} rollback_calls={len(rollback_calls)} "
        f"anchors={len(anchors)} branch_events={len(branches)} "
        f"target_model_events={len(target_model_uuids)}"
    )
    print("✓ scenario B (level-1 target rollback) passed")

    if dump_path is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(conn.sent, default=str))
        print(f"wrote {len(conn.sent)} messages → {dump_path}")


async def _amain(args: argparse.Namespace) -> None:
    if "a" in args.scenarios:
        await scenario_a(args.dump)
    if "b" in args.scenarios:
        dump_b = (
            args.dump.with_name("smoke_rollback.json") if args.dump is not None else None
        )
        await scenario_b(dump_b)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--scenarios", default="ab", help="which scenarios to run (default: ab)"
    )
    p.add_argument(
        "--dump",
        type=Path,
        default=Path("frontend-wb/fixtures/smoke_branch_deep.json"),
        help="where to write Scenario A's wire capture (default: %(default)s)",
    )
    p.add_argument("--no-dump", dest="dump", action="store_const", const=None)
    anyio.run(_amain, p.parse_args())


if __name__ == "__main__":
    main()
