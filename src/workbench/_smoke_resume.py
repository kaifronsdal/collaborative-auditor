"""Resume smoke test for the audit-workbench — level-2 branch prefix replay.

Runs one branch to completion in a session, forks a second `Branch` from it
via `Branch.fork()` (which delegates to `session.audit_history.branch()`)
and runs it under `play()`. Asserts the three things the resume path must
get right (STREAMING.md §"Replay"; petri footguns #5 and #12):

(a) the second branch's *auditor* column carries only the live suffix —
    shared-prefix auditor events are dropped in `_on_event` while
    `_replaying_shared` (the parent supplies them via `splice()`); the
    *target* column likewise carries only its own live `ModelEvent`s
    (anchors stable → the target timeline references the parent's events
    for the replayed prefix);
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
    # Fork at the first target ModelOutput (inclusive — replay through it).
    target_step = next(
        (s for s in log if s.source == "Model.generate" and s.value is not None),
        None,
    )
    assert target_step is not None, "branch 1 produced no target ModelOutput step"
    assert target_step.anchor_id is not None

    # --- branch 2: fork from branch 1 in the SAME session -------------------
    conn2 = FakeConn()
    session.connections.append(conn2)

    b2 = Branch.fork(session, b1, anchor=target_step.anchor_id)
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
    assert expected_auditor >= 1 and expected_target >= 1, (
        f"prefix too small: auditor={expected_auditor} target={expected_target}"
    )
    session.branches[b2.branch_id] = b2
    session.current = b2.branch_id
    b2.play()
    await b2.run()
    await session.close()

    # (a) Auditor: while `_replaying_shared`, `_on_event` drops every
    # auditor-role event — b2's *own* auditor column is post-prefix live
    # turns only.
    auditor_evts = model_events_for(conn2, session, b2.auditor_span_id)
    auditor_uuids = {ev["uuid"] for ev in auditor_evts}
    assert auditor_uuids, "branch 2 produced no live auditor turn after resume"
    assert all(ev["input_refs"] for ev in auditor_evts), (
        "b2 auditor event without input_refs — a shared-prefix event leaked "
        "past the `_replaying_shared` drop"
    )

    # Target: served generates emit no `ModelEvent`; anchors are stable so
    # the b2 target timeline references b1's events for the replayed prefix.
    # b2's own target ModelEvents are live-only.
    target_uuids = {
        ev["uuid"] for ev in model_events_for(conn2, session, b2.target_span_id)
    }
    tape_target = sum(
        1
        for s in b2.audit_tape.log
        if s.source == "Model.generate" and s.value is not None
    )
    assert tape_target >= expected_target, (
        f"b2 tape has {tape_target} target steps, expected ≥{expected_target}"
    )
    assert len(target_uuids) == tape_target - expected_target, (
        f"b2's own target ModelEvents should be live-only "
        f"({tape_target - expected_target}), got {len(target_uuids)}"
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
        f"resume: prefix_len={b2.audit_tape.prefix_len} "
        f"b2_auditor_own={len(auditor_uuids)} (live-only; {expected_auditor} "
        f"shared dropped) b2_target_own={len(target_uuids)} (live-only; "
        f"{expected_target} replayed → b1's events); "
        f"b1.digest={digests['b1']} b2.digest={digests['b2']} "
        f"(separate stores); single drain task"
    )
    print("✓ resume smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
