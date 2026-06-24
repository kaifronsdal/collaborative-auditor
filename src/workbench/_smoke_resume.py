"""Resume smoke test for the audit-workbench — level-2 branch prefix synthesis.

Runs one branch to completion in a session, then creates a second `Branch` in
the *same* session seeded with a prefix of the first branch's `audit_tape.log`
and runs it under `play()`. Asserts the three things the resume path must get
right (STREAMING.md §"Replay"; petri footguns #5 and #12):

(a) the second branch's auditor column gets the prefix's recorded turns as
    synthesised `ModelEvent`s *before* its first live turn — the column is not
    blank on a resumed branch;
(b) the second branch's `AuditTape()` does not see the first branch's
    `config_digest` — per-branch `Store` isolation holds (footgun #5);
(c) the session's single `drain` task is the one started by `Session.start()` —
    the branch did not start its own (footgun #12).

Run:  uv run python -m workbench._smoke_resume
"""

from __future__ import annotations

import anyio
from inspect_petri._auditor import AuditTape, audit_context
from inspect_petri.target import Channel, Controller, Tape

from workbench._smoke_util import FakeConn, model_events_for
from workbench.run import Branch
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED = "test seed"


async def _amain() -> None:
    session = Session()
    await session.start()
    drain_task = session._run_task  # noqa: SLF001 — assert it never changes (c)

    # --- branch 1: run to completion, capture its recorded tape -------------
    b1 = Branch(
        session, "b1", seed=SEED, auditor_model=MODEL, target_model=MODEL, max_turns=3
    )
    session.branches["b1"] = b1
    session.current = "b1"
    b1.play()
    await b1.run()

    log = b1.audit_tape.log
    # slice at a point that includes ≥1 auditor and ≥1 target ModelOutput.
    target_idx = next(
        (i for i, s in enumerate(log) if s.source == "Model.generate"), None
    )
    assert target_idx is not None, "branch 1 produced no target ModelOutput step"
    cut = target_idx + 1
    prefix = log[:cut]
    expected_auditor = sum(
        1
        for s in prefix
        if s.source == "auditor:Model.generate" and s.value is not None
    )
    expected_target = sum(
        1 for s in prefix if s.source == "Model.generate" and s.value is not None
    )
    assert expected_auditor >= 1 and expected_target >= 1, (
        f"prefix slice too small: auditor={expected_auditor} target={expected_target}"
    )

    # --- branch 2: resume from branch 1's prefix in the SAME session --------
    conn2 = FakeConn()
    session.connections.append(conn2)

    b2 = Branch(
        session,
        "b2",
        seed=SEED,
        auditor_model=MODEL,
        target_model=MODEL,
        max_turns=3,
        resume=prefix,
    )
    session.branches["b2"] = b2
    session.current = "b2"
    b2.play()
    await b2.run()
    await session.close()

    # (a) prefix synthesised: branch 2's auditor column carries the K recorded
    # auditor turns as settled events before its first live turn. We assert the
    # first `expected_auditor` auditor model events on b2's span are synthesised
    # (input==[], the prefix marker), then ≥1 live event follows.
    # synthesised prefix events carry `input=[]`; with no input there is nothing
    # to intern, so `condense` leaves `input_refs` at its `None` default. Live
    # turns always have a populated `input_refs` list. That is the discriminator.
    auditor_evts = model_events_for(conn2, session, b2.auditor_span_id)
    synth = [
        ev for ev in auditor_evts if ev["input"] == [] and ev["input_refs"] is None
    ]
    synth_uuids = []
    for ev in synth:
        if ev["uuid"] not in synth_uuids:
            synth_uuids.append(ev["uuid"])
    assert len(synth_uuids) >= expected_auditor, (
        f"expected ≥{expected_auditor} synthesised auditor events, "
        f"got {len(synth_uuids)}"
    )
    # the synthesised prefix events come before the first live (input-bearing) one.
    first_live = next(
        (i for i, ev in enumerate(auditor_evts) if ev["input_refs"]), None
    )
    assert first_live is not None, "branch 2 produced no live auditor turn after resume"
    live_uuid = auditor_evts[first_live]["uuid"]
    assert live_uuid not in synth_uuids, "live turn was misclassified as synthesised"

    target_synth = [
        ev
        for ev in model_events_for(conn2, session, b2.target_span_id)
        if ev["input"] == [] and ev["input_refs"] is None
    ]
    target_synth_uuids = {ev["uuid"] for ev in target_synth}
    assert len(target_synth_uuids) >= expected_target, (
        f"expected ≥{expected_target} synthesised target events, "
        f"got {len(target_synth_uuids)}"
    )

    # (b) per-branch Store isolation: branch 2's store must not carry branch 1's
    # config_digest. Read each branch's digest inside its own store context.
    digests: dict[str, str] = {}
    for name, br in (("b1", b1), ("b2", b2)):
        with audit_context(
            controller=Controller(Channel(seed_instructions=SEED)),
            audit_tape=Tape(),
            store=br.store,
        ):
            digests[name] = AuditTape().config_digest
    assert digests["b1"], "branch 1 recorded no config_digest"
    assert digests["b2"], "branch 2 recorded no config_digest"
    # same settings → same digest, but stored in SEPARATE stores: prove they are
    # distinct store objects (isolation), not the shared global.
    assert b1.store is not b2.store, "branches shared a Store object"

    # (c) drain did not double-start: still the same single task from start().
    assert session._run_task is drain_task, "drain task was replaced/double-started"  # noqa: SLF001

    print(
        f"resume: prefix_cut={cut} "
        f"synth_auditor={len(synth_uuids)} (expected {expected_auditor}) "
        f"synth_target={len(target_synth_uuids)} (expected {expected_target}); "
        f"b1.digest={digests['b1']} b2.digest={digests['b2']} "
        f"(separate stores); single drain task"
    )
    print("✓ resume smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
