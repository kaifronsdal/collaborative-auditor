"""Branch smoke test for the audit-workbench.

Runs a branch to completion, then forks at the first target-model turn via
`Branch.fork()` (which delegates to `session.audit_history.branch()` —
PETRI-L2-HISTORY) and runs the child. Asserts:

(a) The child's `audit_tape` prefix (`tape.prefix_len`) matches the parent's
    log up to the cutoff, unfiltered (marks included — `_mark` pops `pending`).
(b) Splice model: the child's *own* auditor-role events (in `_by_role`) are
    post-prefix only — shared-prefix events are dropped while
    `_replaying_shared`; the parent supplies them via `splice()`. Anchors
    are stable across replay (`Controller._gen_id` served), so the child's
    target timeline references the parent's `ModelEvent`s for the prefix.
(c) The child goes live and produces ≥1 fresh auditor `ModelEvent`.
(d) `Branch.fork()` raises `ValueError` for an unknown anchor.
(e) `view()` includes a `branches` key with parent/branched_at metadata
    derived from the `Trajectory`.

Run:  uv run python -m workbench._smoke_branch
"""

from __future__ import annotations

import anyio
from inspect_petri._auditor import AuditTape, audit_context
from inspect_petri.target import Channel, Controller, Step, Tape

from workbench._smoke_util import FakeConn, model_events_for
from workbench.run import Branch
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED = "test seed"


async def _amain() -> None:
    session = Session()
    await session.start()

    # ── branch 1: run to completion ──────────────────────────────────────────
    b1 = Branch(
        session, "b1", seed=SEED, auditor_model=MODEL, target_model=MODEL, max_turns=3
    )
    session.branches["b1"] = b1
    session.current = "b1"
    b1.play()
    await b1.run()

    log: list[Step] = b1.audit_tape.log

    # Fork at the first target-model step (inclusive — `branch` semantics).
    target_step = next(
        (s for s in log if s.source == "Model.generate" and s.value is not None),
        None,
    )
    assert target_step is not None, "branch 1 produced no target ModelOutput step"
    assert target_step.anchor_id is not None, "target step has no anchor_id"
    anchor = target_step.anchor_id

    # ── (d) Branch.fork raises for unknown anchor ────────────────────────────
    try:
        Branch.fork(session, b1, anchor="unknown-anchor-id-xyz")
        raise AssertionError("Branch.fork should have raised ValueError")
    except ValueError:
        pass

    # ── branch 2: fork from b1 at the target anchor ──────────────────────────
    conn2 = FakeConn()
    session.connections.append(conn2)

    b2 = Branch.fork(session, b1, anchor=anchor)
    # max_turns covers the replayed prefix's auditor steps plus headroom for
    # ≥1 live turn — without eager_resume the prefix can carry up to 3.
    expected_auditor = sum(
        1
        for s in b2.audit_tape.pending
        if s.source == "auditor:Model.generate" and s.value is not None
    )
    expected_target = sum(
        1
        for s in b2.audit_tape.pending
        if s.source == "Model.generate" and s.value is not None
    )
    assert expected_auditor >= 1, "prefix has no auditor steps"
    assert expected_target >= 1, "prefix has no target steps"
    b2.meta = b2.meta.__class__(
        **{**b2.meta.__dict__, "max_turns": expected_auditor + 2}
    )
    session.branches[b2.branch_id] = b2
    session.current = b2.branch_id

    # (a) prefix is unfiltered: every step (marks included) is in `pending`,
    # and `prefix_len` matches.
    assert len(b2.audit_tape.pending) == b2.audit_tape.prefix_len, (
        f"pending length {len(b2.audit_tape.pending)} != "
        f"prefix_len {b2.audit_tape.prefix_len}"
    )
    assert b2.audit_tape.prefix_len == log.index(target_step) + 2, (
        "prefix_len should cover through the matched step + its trailing ack"
    )

    b2.play()
    await b2.run()
    await session.close()

    # (b)/(c) Auditor: while `_replaying_shared`, `_on_event` drops every
    # auditor-role event — the shared prefix is spliced from the parent's
    # `TimelineSpan`, not re-emitted per branch. So b2's *own* auditor
    # column is live-only; the `expected_auditor` shared turns are absent.
    auditor_evts = model_events_for(conn2, session, b2.auditor_span_id)
    auditor_uuids = {ev["uuid"] for ev in auditor_evts}
    assert len(auditor_uuids) <= b2.meta.max_turns - expected_auditor, (
        f"b2 auditor column has {len(auditor_uuids)} events; shared-prefix "
        f"({expected_auditor}) should have been dropped, leaving "
        f"≤{b2.meta.max_turns - expected_auditor} live"
    )
    assert auditor_uuids, "branch 2 produced no live auditor turn after prefix"

    # (b) Target: shared-prefix target generates are *served* (no provider
    # call → no `ModelEvent` under b2's span). Anchors are stable, so the
    # b2 target timeline references b1's events for those steps; only b2's
    # *live* target generates land under its own span.
    target_evts = model_events_for(conn2, session, b2.target_span_id)
    target_uuids = {ev["uuid"] for ev in target_evts}
    # b2's L2 tape carries the full lineage; its target steps must be ≥
    # the prefix's (replayed) plus ≥1 live.
    tape_target = sum(
        1
        for s in b2.audit_tape.log
        if s.source == "Model.generate" and s.value is not None
    )
    assert tape_target >= expected_target, (
        f"b2 tape has {tape_target} target steps, expected ≥{expected_target} "
        f"replayed from prefix"
    )
    assert all(ev["input_refs"] for ev in target_evts), (
        "b2 target event missing input_refs"
    )
    assert len(target_uuids) == tape_target - expected_target, (
        f"b2's own target ModelEvents should be live-only "
        f"({tape_target - expected_target}), got {len(target_uuids)}"
    )

    # (e) session view() includes branches metadata derived from Trajectory.
    view = session.view()
    assert "branches" in view, "view() missing 'branches' key"
    b2_meta = view["branches"].get(b2.branch_id)
    assert b2_meta is not None, "b2 not in view branches"
    assert b2_meta["parent"] == "b1", f"wrong parent: {b2_meta['parent']!r}"
    assert b2_meta["branched_at"] == anchor, (
        f"wrong branched_at: {b2_meta['branched_at']!r}"
    )
    assert b2_meta["status"] == "ended", f"wrong status: {b2_meta['status']!r}"
    assert b2_meta["seed"].startswith(SEED[:20]), f"wrong seed: {b2_meta['seed']!r}"

    # Store isolation still holds (same check as smoke_resume).
    assert b1.store is not b2.store, "branches share a Store object"

    # Per-branch config_digest check.
    digests: dict[str, str] = {}
    for name, br in (("b1", b1), ("b2", b2)):
        with audit_context(
            controller=Controller(Channel(seed_instructions=SEED)),
            audit_tape=Tape(),
            store=br.store,
        ):
            digests[name] = AuditTape().config_digest
    assert digests["b1"], "b1 config_digest empty"
    assert digests["b2"], "b2 config_digest empty"

    print(
        f"branch: prefix_len={b2.audit_tape.prefix_len} "
        f"b2_target_own={len(target_uuids)} (live-only; {expected_target} "
        f"replayed → b1's events) b2_auditor_own={len(auditor_uuids)} "
        f"(live-only; {expected_auditor} shared dropped); "
        f"parent=b1 branched_at={anchor[:8]}…"
    )
    print("✓ branch smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
