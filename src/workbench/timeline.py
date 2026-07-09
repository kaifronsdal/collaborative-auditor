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
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from inspect_petri.target import Trajectory

from workbench.run import _tape_prefix
from workbench.sources import GEN_SOURCE

if TYPE_CHECKING:
    from workbench.session import Session


def _walk(t: Trajectory) -> Iterator[Trajectory]:
    yield t
    for c in t.children:
        yield from _walk(c)


def _msg_id(d: dict[str, Any]) -> str | None:
    """Assistant ``message.id`` from a dumped `ModelEvent`, once ``choices`` land."""
    choices = (d.get("output") or {}).get("choices") or []
    return choices[0].get("message", {}).get("id") if choices else None


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
            (
                s.anchor_id
                for s in reversed(_tape_prefix(t.tape))
                if s.source == GEN_SOURCE
            ),
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


def build_target_timeline(session: Session) -> dict[str, Any]:
    """The session-wide target `Timeline` (A1-b-wide, event-sourced graft).

    One flat tree of L1-trajectory spans across *every* `Branch`, structurally
    identical to the auditor timeline so both columns take the same
    ``convertServerTimeline → computeFlatSwimlaneRows → splice()`` path.
    Purely event-sourced — never reads any `Tape`.

    **Invariant `splice()` needs:** every non-root span's `branchedFrom` is a
    target anchor that appears as an `AnchorEvent` in its `.branches`-parent's
    `content`. So each L1 span is grafted under whichever L1 span, anywhere in
    the session, *generated* the fork's last shared target turn live — not
    under an L2 wrapper, and not under its `history.children` parent when that
    parent never went live (an L2 replay re-executing an L1 rollback).

    Four passes over `by_role` (ARCHITECTURE-RACES.md §A1-b-wide): (1) collect
    L1 span ids across all branches; (2) bucket target events per L1 span via
    `span_parent`; (3) split each bucket at its first `ModelEvent` — the
    pre-live/live boundary that covers *both* L1-replayed and L2-served
    prefixes uniformly — and index `anchor_owner[msg_id] = sid` from live
    `ModelEvent`s; (4) for each span with live content, `branchedFrom` = last
    `AnchorEvent.anchor_id` in its pre-live prefix, graft parent =
    `anchor_owner.get(branchedFrom)` (or the wrapper root). Spans with no live
    content are dropped — the child grafts directly under the ancestor that
    owns the anchor.
    """
    l1_of: dict[str, str] = {}
    for bid, b in session.branches.items():
        for t in _walk(b.history.root):
            l1_of[t.span_id] = bid

    def owner(sid: str | None) -> str | None:
        while sid is not None and sid not in l1_of:
            sid = session.span_parent.get(sid)
        return sid

    buckets: dict[str, list[str]] = {sid: [] for sid in l1_of}
    for bid in session.branches:
        for u in session.by_role.get((bid, "target"), []):
            if (d := session.events.get(u)) and (o := owner(d["span_id"])):
                buckets[o].append(u)

    pre: dict[str, list[str]] = {}
    live: dict[str, list[str]] = {}
    anchor_owner: dict[str, str] = {}
    for sid, uuids in buckets.items():
        i = next(
            (i for i, u in enumerate(uuids) if session.events[u]["event"] == "model"),
            len(uuids),
        )
        pre[sid], live[sid] = uuids[:i], uuids[i:]
        for u in live[sid]:
            d = session.events[u]
            if d["event"] == "model" and (mid := _msg_id(d)):
                anchor_owner[mid] = sid

    counter = itertools.count(1)

    def to_span(sid: str) -> tuple[dict[str, Any], str | None]:
        # `branchedFrom` = last target-*generate* anchor in the pre-live
        # prefix — NOT `Branch.branched_at` (may be an auditor/`Stage`
        # anchor), and NOT a staging (`Channel.next_command`) anchor. The
        # `anchor_owner` filter guarantees `bf` is an assistant message.id
        # some span emitted a live `ModelEvent` for, so its `AnchorEvent`
        # (which fires *after* the `ModelEvent`) is in that span's `live`
        # content and `splice()` will find it.
        #
        # Same-branch L1 rollback lanes emit **no** replayed `AnchorEvent`
        # under the new lane's span (petri's serve path emits nothing) —
        # only a `BranchEvent(from_anchor=<parent's message.id>)` marks the
        # replay boundary. Without the second arm the lane gets `bf=None`
        # and grafts as a sibling under root instead of nested under its
        # parent lane, and `splice()` misaligns (F1/`_smoke_ui_rollback`).
        def _bf_of(u: str) -> str | None:
            d = session.events[u]
            if d["event"] == "anchor":
                return d["anchor_id"]
            if d["event"] == "branch":
                return d.get("from_anchor")
            return None

        bf = next(
            (
                a
                for u in reversed(pre[sid])
                if (a := _bf_of(u)) is not None and a in anchor_owner
            ),
            None,
        )
        return {
            "type": "span",
            "id": sid,
            "name": f"branch {next(counter)}",
            "span_type": "branch",
            "branched_from": bf,
            "content": [{"type": "event", "event": u} for u in live[sid]],
            "branches": [],
        }, anchor_owner.get(bf)

    spans = {sid: to_span(sid) for sid in l1_of if live[sid]}
    root: dict[str, Any] = {
        "type": "span",
        "id": "target-root",
        "name": "target",
        "span_type": "branch",
        "branched_from": None,
        "content": [],
        "branches": [],
    }
    for span, parent_sid in spans.values():
        (spans[parent_sid][0] if parent_sid in spans else root)["branches"].append(span)

    return {"name": "target", "description": "Target conversation tree", "root": root}
