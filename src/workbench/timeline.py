"""Auditor-column `Timeline` construction (STREAMING.md §B, PETRI-L2-HISTORY).

Split from `session.py` (M1-REFACTOR Batch I). `build_auditor_timeline` is a
free function over a `Session` — its only private coupling is the incremental
`_by_role` index, which exists precisely so this rebuild is O(events-in-role)
rather than a full `session.events` scan.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

from inspect_petri.target import Trajectory

from workbench.sources import GEN_SOURCE

if TYPE_CHECKING:
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
    from `_by_role`, not `build_history_timeline(audit_history)` directly:
    the L2 tape records *every* model call (auditor and target), so the
    anchor-keyed content would interleave target `ModelEvent`s into the
    auditor column. Filtering to `_by_role[(bid, "auditor")]` keeps the
    existing `eventsToTurns(hasToolEvents=true)` render path unchanged.

    Built directly as the dumped dict (rather than via `Timeline.model_dump`)
    because `session.events` already holds dumped events — reconstructing
    `Event` objects just to re-serialise their uuids would be wasted work.
    """

    def content_for(bid: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for uuid in session._by_role.get((bid, "auditor"), []):  # noqa: SLF001
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
