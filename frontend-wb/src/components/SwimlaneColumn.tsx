/**
 * A column rendered from a server-built `Timeline` via inspect-view's swimlane
 * layout — the same data path inspect-view uses for petri transcripts
 * (`convertServerTimeline` → `computeFlatSwimlaneRows({showBranches:true})`).
 *
 * For `role === "target"` the timeline is petri's per-branch trajectory tree
 * (rollback forks); for `role === "auditor"` it's the session-wide workbench
 * branch tree (`build_auditor_timeline`). In both cases selecting a lane shows
 * that span's full lineage via `splice(root, span)` — the parent prefix up to
 * the fork + the span's own turns, exactly what an unbranched run of that
 * lineage would have produced.
 *
 * Falls back to the linear `Column` when no server timeline is available yet
 * (first turns of a fresh branch, before the first `{t:"timeline"}` op lands).
 */
import { type JSX, forwardRef, useEffect, useMemo, useState } from "react";
import {
  getAgents,
  type SwimlaneRow,
  type TimelineSpan,
} from "@tsmono/inspect-components/transcript/timeline";
import { TimelineSwimLanes } from "@tsmono/inspect-components/transcript/timeline/swimlanes";

import { eventsToTurns, isModelEvent } from "../lib/events";
import {
  computeForks,
  usePendingForkAnchor,
  useQueued,
  useStagedForTarget,
  useSwimlanes,
} from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { Bubble } from "./Bubble";
import { LinearColumn, QueuedBubble, type ColumnHandle } from "./Column";
import { ModelEventRow } from "./ModelEventRow";
import { ShimmerBubble } from "./ShimmerBubble";
import { useColumnScroll } from "./useColumnScroll";

type Props = {
  branch: BranchId;
  role?: Role;
  linked?: boolean;
  onSync?: (ts: string) => void;
};

/** The `TimelineSpan` a row wraps (one row = one branch span here). */
function rowSpan(r: SwimlaneRow): TimelineSpan {
  return getAgents(r.spans[0])[0];
}

export const SwimlaneColumn = forwardRef<ColumnHandle, Props>(function SwimlaneColumn(
  { branch, role = "target", linked, onSync },
  ref
): JSX.Element {
  const { timeline, rows, layouts, lineage } = useSwimlanes(branch, role);
  const queued = useQueued(branch, role);
  const staged = useStagedForTarget(branch);
  const status = useSession((s) => s.status);
  const generating = useSession((s) => s.generating);
  const switchBranch = useSession((s) => s.switchBranch);
  const branches = useSession((s) => s.branches);
  const forkAnchor = usePendingForkAnchor();
  const isAuditor = role === "auditor";

  // A1-b-wide: both timelines are session-wide. Auditor rows are workbench
  // branches (span id == branch id); target rows are L1-trajectory spans
  // owned by whichever branch's `l1_spans` contains them. Default lane =
  // the deepest row belonging to `branch` (falls back to the last row when
  // `branch` has no live L1 span yet — a just-forked child before its first
  // target turn shows the parent's tip).
  const l1Set = useMemo(
    () => new Set(branches[branch]?.l1_spans ?? []),
    [branches, branch]
  );
  const defaultKey = useMemo(() => {
    if (rows.length === 0) return null;
    const mine = isAuditor
      ? rows.find((r) => rowSpan(r).id === branch)
      : [...rows].reverse().find((r) => l1Set.has(rowSpan(r).id));
    return mine?.key ?? rows[rows.length - 1].key;
  }, [rows, isAuditor, branch, l1Set]);
  const [selectedKey, setSelectedKey] = useState<string | null>(defaultKey);
  useEffect(() => {
    // Follow the live tip when new branch rows appear (rollback) and the user
    // hasn't picked a lane explicitly, or their selection no longer exists /
    // belongs to a different branch (post-`switchBranch`). The auditor lane
    // is *derived* from `branch`, so it always tracks `defaultKey`.
    const own = rows.find(
      (r) =>
        r.key === selectedKey &&
        (isAuditor || l1Set.size === 0 || l1Set.has(rowSpan(r).id))
    );
    if (isAuditor || selectedKey == null || own == null) {
      setSelectedKey(defaultKey);
    }
  }, [defaultKey, rows, selectedKey, isAuditor, l1Set]);

  // Resolve the selected row's TimelineSpan and its full event lineage.
  const selected = rows.find((r) => r.key === selectedKey) ?? rows[0];
  const span: TimelineSpan | undefined = selected ? rowSpan(selected) : undefined;
  const laneEvents = useMemo(() => {
    if (!span || !timeline) return [];
    // RACE-FIXES.md R3 / WS-race #15: drop retracted (interrupted)
    // generates so `eventsToTurns` doesn't misalign on the orphan.
    const evs = lineage(span).filter(
      (e) => (e as { rewound?: boolean }).rewound !== true
    );
    // F3: derived optimistic truncation at the in-flight fork's anchor —
    // replaces the imperative `byRole[PENDING_BRANCH]` snapshot. `current`
    // stays on the parent so this column keeps its swimlane context (vs.
    // the old drop-to-LinearColumn) while the transcript below cuts.
    if (forkAnchor == null) return evs;
    const idx = evs.findIndex(
      (e) => isModelEvent(e) && e.output.choices[0]?.message.id === forkAnchor
    );
    return idx >= 0 ? evs.slice(0, idx + 1) : evs;
  }, [span, timeline, lineage, forkAnchor]);
  const laneTurns = useMemo(
    () => eventsToTurns(laneEvents, isAuditor),
    [laneEvents, isAuditor]
  );
  const { scrollRef, onScroll, rowRef, highlighted } = useColumnScroll(
    laneTurns, { linked, onSync }, ref
  );

  // Fork points keyed by assistant message id → sibling row keys + our idx.
  const forks = useMemo(
    () => computeForks(rows, selected?.key ?? null),
    [rows, selected?.key]
  );
  // Reverse `l1_spans` map: L1 span id → owning branch id. Lets a click on
  // a *foreign* target lane resolve which workbench branch to switch to.
  const l1Owner = useMemo(() => {
    const m = new Map<string, BranchId>();
    for (const [bid, meta] of Object.entries(branches)) {
      for (const sid of meta.l1_spans ?? []) m.set(sid, bid);
    }
    return m;
  }, [branches]);

  const selectLane = (key: string | null): void => {
    const r = key != null ? rows.find((r) => r.key === key) : undefined;
    const sid = r ? rowSpan(r).id : null;
    if (isAuditor) {
      // Auditor lanes ARE workbench branches — switching one switches the
      // whole desk. `defaultKey` follows via the `branch` prop re-render.
      if (sid != null && sid !== branch) switchBranch(sid);
    } else {
      // Target: if the picked span is a *foreign* branch's L1, switch to
      // its owner so `current`/queued/status track it — mirrors the
      // auditor-lane behaviour. Local `selectedKey` is set regardless so
      // the specific lane (not just the owner's default) is shown.
      if (sid != null && !l1Set.has(sid)) {
        const owner = l1Owner.get(sid);
        if (owner != null && owner !== branch) switchBranch(owner);
      }
      setSelectedKey(key ?? defaultKey);
    }
  };
  const switchSibling = (anchor: string, dir: 1 | -1): void => {
    const g = forks.get(anchor);
    if (!g) return;
    const next = g.idx + dir;
    if (next < 0 || next >= g.siblings.length) return;
    selectLane(g.siblings[next]);
  };

  // No timeline yet → linear column (first turn hasn't completed).
  if (!timeline || rows.length === 0) {
    return (
      <LinearColumn ref={ref} branch={branch} role={role} linked={linked} onSync={onSync} />
    );
  }

  const lastEvent = laneEvents[laneEvents.length - 1];
  const lastIsPending =
    lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  // RACE-FIXES.md R3 gap #3,4: gate on `generating === role`, not
  // `status === "running"`, so the auditor column doesn't shimmer while
  // the target is generating. `undefined` = old backend → fall back.
  const isGenerating =
    generating === undefined ? status === "running" : generating === role;
  const showShimmer = isGenerating && !lastIsPending;

  return (
    <div className="column swimlane-column" ref={scrollRef} onScroll={onScroll}>
      <div className="column-head">
        {role}
        {rows.length > 1 && (
          <span className="sl-count">
            {rows.length} {isAuditor ? "branches" : "trajectories"}
          </span>
        )}
      </div>

      {rows.length > 1 && (
        <div className="lane-gantt-host">
          <TimelineSwimLanes
            layouts={layouts}
            timeline={{
              selected: selected?.key ?? null,
              select: (k) => selectLane(k),
              clearSelection: () => selectLane(defaultKey),
            }}
            defaultCollapsed={false}
          />
        </div>
      )}

      {laneTurns.map((turn, i) => {
        const anchor = turn.ev.output?.choices?.[0]?.message?.id;
        const fork = anchor != null ? forks.get(anchor) : undefined;
        return (
          <ModelEventRow
            key={turn.ev.uuid}
            turn={turn}
            turnIndex={i}
            auditor={isAuditor}
            highlighted={highlighted === turn.ev.uuid}
            siblingPos={fork && { idx: fork.idx, total: fork.siblings.length }}
            onSwitchSibling={
              fork && anchor != null ? (d) => switchSibling(anchor, d) : undefined
            }
            rowRef={rowRef(turn.ev.uuid!)}
          />
        );
      })}
      {!isAuditor && staged.map((m, i) => (
        <Bubble key={m.id ?? `s${i}`} msg={m} ghost byline={`staged · ${m.role}`} />
      ))}
      {queued.map((m, i) => (
        <QueuedBubble key={m.id ?? `q${i}`} branch={branch} role={role} msg={m} />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
});
