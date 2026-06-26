"""Branch smoke test for the audit-workbench.

Runs a branch to completion, then forks at the first target-model turn via
`Branch.fork()` (which delegates to `session.audit_history.branch()` —
PETRI-L2-HISTORY) and runs the child. Asserts:

(a) Full-list `==` on the shared prefix: `normalize(b2)[:N] ==
    normalize(b1)[:N]` where N is the count of auditor+target `ModelOutput`
    steps in b2's replayed prefix. This is the load-bearing check — every
    role/text/tool-call in the lineage matches the parent verbatim.
(b) The child goes live: `len(normalize(b2)) > N`.
(c) `Branch.fork()` raises `ValueError` for an unknown anchor.
(d) `view()` includes a `branches` key with parent/branched_at metadata
    derived from the `Trajectory`.

Run:  uv run python -m workbench._smoke_branch
"""

from __future__ import annotations

import anyio
from inspect_petri._auditor import AuditTape, audit_context
from inspect_petri.target import Channel, Step, Trajectory

from workbench._smoke_fixtures import normalize
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
    b2 = Branch.fork(session, b1, anchor=anchor)
    # max_turns covers the replayed prefix's auditor steps plus headroom for
    # ≥1 live turn.
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

    # (a)/(b) Load-bearing assertion: full-list `==` on the shared prefix.
    # b2's L2 tape is its full lineage (replayed prefix verbatim from b1,
    # then live), so its first N normalize() entries — N = #ModelOutput
    # steps in the prefix — must equal b1's first N exactly: every role,
    # text, and tool-call arg. Any drift in replay (re-generated content,
    # dropped/reordered steps, mutated args) fails this. Both sides are
    # normalized *after* b2.run() because the serve path returns shared
    # `Step` refs and `_eager_resume_inject` mutates them in place — parent
    # and child settle to the same view, but only once replay completes.
    actual_b2 = normalize(session, b2.branch_id)
    captured_b1 = normalize(session, "b1")
    n = expected_auditor + expected_target
    assert actual_b2[:n] == captured_b1[:n], (
        f"b2 shared prefix diverged from b1:\n"
        f"  b2[:{n}] = {actual_b2[:n]}\n"
        f"  b1[:{n}] = {captured_b1[:n]}"
    )
    assert len(actual_b2) > n, (
        f"b2 produced no live suffix past the {n}-entry replayed prefix: "
        f"{actual_b2}"
    )

    # (d) session view() includes branches metadata derived from Trajectory.
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
            channel=Channel(seed_instructions=SEED),
            audit_trajectory=Trajectory(),
            store=br.store,
        ):
            digests[name] = AuditTape().config_digest
    assert digests["b1"], "b1 config_digest empty"
    assert digests["b2"], "b2 config_digest empty"

    print(
        f"branch: prefix_len={b2.audit_tape.prefix_len} "
        f"normalize(b2)[:{n}]==normalize(b1)[:{n}] "
        f"live_suffix={len(actual_b2) - n}; "
        f"parent=b1 branched_at={anchor[:8]}…"
    )
    print("✓ branch smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
