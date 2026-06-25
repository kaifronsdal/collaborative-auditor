"""Branch smoke test for the audit-workbench.

Runs a branch to completion, then uses `slice_at` to cut at the first
target-model turn and creates a second branch from that prefix. Asserts:

(a) The new branch's `audit_tape.pending` has the right length (matching the
    prefix's value-bearing steps).
(b) Splice model: the new branch's *target* column gets the prefix's target
    turns as real `ModelEvent`s (emitted by `EmittingTape` with full `input`);
    its *auditor* column carries only the post-prefix live turns —
    shared-prefix auditor events are dropped while `_replaying_shared` (the
    parent supplies them via `splice()`).
(c) The new branch goes live and produces ≥1 fresh auditor ModelEvent.
(d) `slice_at` raises `ValueError` for an unknown anchor_id.
(e) The session `view()` includes a `branches` key with parent/branched_at metadata.

Run:  uv run python -m workbench._smoke_branch
"""

from __future__ import annotations

import anyio
from inspect_petri._auditor import AuditTape, audit_context
from inspect_petri.target import Channel, Controller, Step, Tape

from workbench._smoke_util import FakeConn, model_events_for
from workbench.run import Branch, slice_at
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED = "test seed"


def _test_mid_rollback_slice() -> None:
    """slice_at should raise ValueError when anchor lands mid-rollback."""
    from inspect_ai.model import (
        ChatCompletionChoice,
        ChatMessageAssistant,
        ModelOutput,
    )
    from inspect_ai.model._model import ModelUsage
    from inspect_ai.tool import ToolCall
    from inspect_petri.target import Step

    # Build a minimal ModelOutput with a rollback_conversation tool call.
    rollback_call = ToolCall(
        id="tc1",
        function="rollback_conversation",
        arguments={"message_id": "m0"},
        type="function",
    )
    mo = ModelOutput(
        model="test",
        choices=[],
        usage=ModelUsage(),
        error=None,
    )
    msg = ChatMessageAssistant(content="", tool_calls=[rollback_call], id="anchor1")
    choice = ChatCompletionChoice(message=msg, stop_reason="tool_calls")
    mo.choices = [choice]

    auditor_step = Step(value=mo, source="auditor:Model.generate", anchor_id="anchor1")
    # NO subsequent boundary=="in" step — this is mid-rollback
    target_step = Step(value=None, source="Model.generate", anchor_id="anchor2")

    log = [auditor_step, target_step]

    try:
        slice_at(log, "anchor2")
        raise AssertionError("Expected slice_at to raise ValueError mid-rollback")
    except ValueError as exc:
        assert "mid-rollback" in str(exc), f"Expected 'mid-rollback' in error: {exc}"
    print("✓ mid-rollback slice guard test passed")


async def _amain() -> None:
    _test_mid_rollback_slice()

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

    # Slice at the first target-model step.
    target_step = next(
        (s for s in log if s.source == "Model.generate" and s.value is not None),
        None,
    )
    assert target_step is not None, "branch 1 produced no target ModelOutput step"
    assert target_step.anchor_id is not None, "target step has no anchor_id"
    anchor = target_step.anchor_id

    prefix = slice_at(log, anchor)
    expected_value_steps = sum(1 for s in prefix if s.value is not None)
    expected_auditor = sum(
        1
        for s in prefix
        if s.source == "auditor:Model.generate" and s.value is not None
    )
    expected_target = sum(
        1 for s in prefix if s.source == "Model.generate" and s.value is not None
    )
    assert expected_auditor >= 1, "prefix has no auditor steps"
    assert expected_target >= 1, "prefix has no target steps"

    # ── (d) slice_at raises for unknown anchor ────────────────────────────────
    try:
        slice_at(log, "unknown-anchor-id-xyz")
        raise AssertionError("slice_at should have raised ValueError")
    except ValueError:
        pass

    # ── branch 2: resume from prefix ─────────────────────────────────────────
    conn2 = FakeConn()
    session.connections.append(conn2)

    # max_turns covers the replayed prefix's auditor steps plus headroom for
    # ≥1 live turn — without eager_resume the prefix can carry up to 3.
    b2 = Branch(
        session,
        "b2",
        seed=SEED,
        auditor_model=MODEL,
        target_model=MODEL,
        max_turns=expected_auditor + 2,
        resume=prefix,
        parent_id="b1",
        branched_at=anchor,
    )
    session.branches["b2"] = b2
    session.current = "b2"

    # (a) pending has the right count — value-bearing steps only (the
    # `audit_tape.pending` filter strips value=None markers).
    assert len(b2.audit_tape.pending) == expected_value_steps, (
        f"pending length {len(b2.audit_tape.pending)} != expected {expected_value_steps}"
    )

    b2.play()
    await b2.run()
    await session.close()

    # (b) splice model. Target: per-branch column, no cross-branch splice —
    # `EmittingTape` emits a real settled ModelEvent (with full `input`, so
    # `input_refs` is populated) for each replayed target generate. b2's
    # target column must carry at least the prefix's target steps.
    auditor_evts = model_events_for(conn2, session, b2.auditor_span_id)
    target_evts = model_events_for(conn2, session, b2.target_span_id)

    target_uuids = {ev["uuid"] for ev in target_evts}
    assert len(target_uuids) >= expected_target, (
        f"expected ≥{expected_target} replayed target events on b2, "
        f"got {len(target_uuids)}"
    )
    assert all(ev["input_refs"] for ev in target_evts), (
        "replayed target event missing input_refs — EmittingTape should emit "
        "full `input`, not the old `input==[]` synth marker"
    )

    # Auditor: while `_replaying_shared`, `_on_event` drops every auditor-role
    # event (the parent's `TimelineSpan` supplies the shared prefix via
    # `splice()`). So b2's *own* auditor column is live-only — the
    # `expected_auditor` shared turns must NOT appear here.
    auditor_uuids = {ev["uuid"] for ev in auditor_evts}
    assert len(auditor_uuids) <= b2.meta.max_turns - expected_auditor, (
        f"b2 auditor column has {len(auditor_uuids)} events; shared-prefix "
        f"({expected_auditor}) should have been dropped, leaving "
        f"≤{b2.meta.max_turns - expected_auditor} live"
    )

    # (c) at least one fresh (live) auditor turn after the prefix.
    assert auditor_uuids, "branch 2 produced no live auditor turn after prefix"

    # (e) session view() includes branches metadata.
    view = session.view()
    assert "branches" in view, "view() missing 'branches' key"
    b2_meta = view["branches"].get("b2")
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
        f"branch: prefix_len={len(prefix)} value_steps={expected_value_steps} "
        f"b2_target={len(target_uuids)} (≥{expected_target} replayed) "
        f"b2_auditor_own={len(auditor_uuids)} (live-only; {expected_auditor} "
        f"shared dropped); parent=b1 branched_at={anchor[:8]}…"
    )
    print("✓ branch smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
