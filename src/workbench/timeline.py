"""Auditor- and target-column `Timeline` construction (STREAMING.md §B).

Split from `session.py` (M1-REFACTOR Batch I). Both builders are free
functions over a `Session` whose only private coupling is the incremental
`by_role` index — kept precisely so a rebuild is O(events-in-role) rather
than a full `session.events` scan. Both walk a `History` for tree *shape*
(set-once metadata, never lags) and source `content` from `by_role`
(populated on emit, so an in-flight `ModelEvent` is present before its step
is appended to any tape) — ARCHITECTURE-RACES.md A1.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from inspect_petri.target import Trajectory

from workbench.sources import GEN_SOURCE

if TYPE_CHECKING:
    from workbench.run import Branch
    from workbench.session import Session


def build_auditor_timeline(session: Session) -> dict[str, Any]:
    """The session-wide auditor `Timeline`, one `TimelineSpan` per `Branch`.

    Tree shape comes from `session.audit_history` (PETRI-L2-HISTORY): each
    `Branch` *is* one L2 `Trajectory`, so `TimelineSpan.id == branch_id` and
    `branched_from == trajectory.branched_from` (already normalised by
    `Branch.fork()` to the last anchored step in the shared prefix, which is
    what `splice()` cuts on, inclusive). The synthetic `audit_history.root`
    is the wrapper span — it never runs, so its `content` is empty and each
    real root branch has `branched_from=None` (`splice()` discards the
    wrapper's prefix).

    `content` is the branch's own (post-shared-prefix) auditor-role events
    from `by_role`, not `build_history_timeline(audit_history)` directly:
    the L2 tape records *every* model call (auditor and target), so the
    anchor-keyed content would interleave target `ModelEvent`s into the
    auditor column. Filtering to `by_role[(bid, "auditor")]` keeps the
    existing `eventsToTurns(hasToolEvents=true)` render path unchanged.

    Built directly as the dumped dict (rather than via `Timeline.model_dump`)
    because `session.events` already holds dumped events — reconstructing
    `Event` objects just to re-serialise their uuids would be wasted work.
    """

    def content_for(bid: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for uuid in session.by_role.get((bid, "auditor"), []):
            d = session.events.get(uuid)
            if d is None:
                continue
            # Exclude petri's pre-`execute_tools` `AnchorEvent` so the
            # `TURN_END_SOURCE` one (after the turn's `ToolEvent`s) is the
            # only `findIndex` match for `splice()`.
            if d["event"] == "anchor" and d.get("source") == GEN_SOURCE:
                continue
            out.append({"type": "event", "event": uuid})
        return out

    def auditor_branched_from(t: Trajectory) -> str | None:
        # Last auditor generate in the shared prefix — the only anchors
        # present in the parent's *auditor-role* content are the
        # `TURN_END_SOURCE` `AnchorEvent`s keyed on auditor message ids, so
        # `splice()` must cut there (`t.branched_from` may be a target or
        # `Stage` anchor, which the auditor column never carries).
        return next(
            (s.anchor_id for s in reversed(t.tape.prefix()) if s.source == GEN_SOURCE),
            None,
        )

    counter = itertools.count(1)

    def to_span(t: Trajectory) -> dict[str, Any]:
        return {
            "type": "span",
            "id": t.span_id,
            "name": f"branch {next(counter)}",
            "span_type": "branch",
            "branched_from": auditor_branched_from(t),
            "content": content_for(t.span_id),
            "branches": [to_span(c) for c in t.children],
        }

    root = session.audit_history.root
    return {
        "name": "auditor",
        "description": "Auditor branch tree",
        "root": {
            "type": "span",
            "id": root.span_id,
            "name": "auditor",
            "span_type": "branch",
            "branched_from": None,
            "content": [],
            "branches": [to_span(c) for c in root.children],
        },
    }


def build_target_timeline(session: Session, branch: Branch) -> dict[str, Any]:
    """Per-`Branch` target `Timeline`, `by_role`-sourced (A1-b-narrow).

    Tree shape from `branch.history` (the L1 target trajectory tree — set at
    `Trajectory` construction, never lags). Content from
    `by_role[(bid, "target")]`, bucketed to each L1 trajectory by walking
    `session.span_parent` to the nearest trajectory span. Unlike petri's
    `build_history_timeline` this never reads `tape.log`, so a target
    `ModelEvent` (emitted before `replayable` appends its step) is present
    the instant it lands — no lag, no R2 tail-append.

    Each non-root trajectory's L1-replayed prefix anchors are dropped
    (`splice()` reconstructs them from the parent span). For each remaining
    anchor, `session._by_anchor` resolves its `ModelEvent` uuid: on a live
    turn the event is already in the bucket (dedup no-ops), but on a forked
    L2 `Branch`'s *served* prefix — where no target `ModelEvent` is emitted —
    it borrows the parent-L2 branch's event so the child's replayed turns
    render before the first live one (chaos s4).
    """
    bid = branch.branch_id
    root = branch.history.root

    traj_of: dict[str, Trajectory] = {}

    def collect(t: Trajectory) -> None:
        traj_of[t.span_id] = t
        for c in t.children:
            collect(c)

    collect(root)

    def owner(span_id: str | None) -> str | None:
        while span_id is not None and span_id not in traj_of:
            span_id = session.span_parent.get(span_id)
        return span_id

    buckets: dict[str, list[str]] = {sid: [] for sid in traj_of}
    for u in session.by_role.get((bid, "target"), []):
        if (d := session.events.get(u)) is not None and (o := owner(d["span_id"])):
            buckets[o].append(u)

    def content_for(t: Trajectory) -> list[dict[str, Any]]:
        prefix = {s.anchor_id for s in t.tape.prefix() if s.anchor_id}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for u in buckets[t.span_id]:
            d = session.events[u]
            if d["event"] == "anchor":
                if d["anchor_id"] in prefix:
                    continue
                # Cross-L2 borrow: pull this anchor's `ModelEvent` uuid from
                # `_by_anchor` — the parent-L2 branch's event when this turn
                # was L2-served (no local emit); already `seen` when live.
                for r in session._by_anchor.get(d["anchor_id"], ()):  # noqa: SLF001
                    if r not in seen:
                        out.append({"type": "event", "event": r})
                        seen.add(r)
            if u not in seen:
                out.append({"type": "event", "event": u})
                seen.add(u)
        return out

    counter = itertools.count(1)

    def to_span(t: Trajectory) -> dict[str, Any]:
        return {
            "type": "span",
            "id": t.span_id,
            "name": f"branch {next(counter)}",
            "span_type": "branch",
            "branched_from": t.branched_from,
            "content": content_for(t),
            "branches": [to_span(c) for c in t.children],
        }

    return {
        "name": f"{bid}:target",
        "description": "Target conversation tree",
        "root": to_span(root),
    }
