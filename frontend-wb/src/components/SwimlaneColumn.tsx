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
import {
  type JSX,
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getAgents,
  type SwimlaneRow,
  type TimelineSpan,
} from "@tsmono/inspect-components/transcript/timeline";
import { TimelineSwimLanes } from "@tsmono/inspect-components/transcript/timeline/swimlanes";

import { bisectTurns, eventsToTurns, isModelEvent } from "../lib/events";
import {
  computeForks,
  useQueued,
  useStagedForTarget,
  useSwimlanes,
} from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { Bubble } from "./Bubble";
import { LinearColumn, type ColumnHandle } from "./Column";
import { ModelEventRow } from "./ModelEventRow";
import { ShimmerBubble } from "./ShimmerBubble";

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
  const send = useSession((s) => s.send);
  const isAuditor = role === "auditor";

  // Selected lane key. For the target column, default to the deepest (latest)
  // branch row — "what the target is currently seeing". For the auditor
  // column, the swimlane is the workbench-branch tree and the selected lane
  // *is* the current branch — find the row whose span id is `branch`.
  const defaultKey = useMemo(() => {
    if (rows.length === 0) return null;
    if (isAuditor) {
      const r = rows.find((r) => rowSpan(r).id === branch);
      if (r) return r.key;
    }
    return rows[rows.length - 1].key;
  }, [rows, isAuditor, branch]);
  const [selectedKey, setSelectedKey] = useState<string | null>(defaultKey);
  useEffect(() => {
    // Follow the live tip when new branch rows appear (rollback) and the user
    // hasn't picked a lane explicitly, or their selection no longer exists.
    // The auditor lane is *derived* from `branch`, so it always tracks
    // `defaultKey`.
    if (
      isAuditor ||
      selectedKey == null ||
      !rows.some((r) => r.key === selectedKey)
    ) {
      setSelectedKey(defaultKey);
    }
  }, [defaultKey, rows, selectedKey, isAuditor]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // Resolve the selected row's TimelineSpan and its full event lineage.
  const selected = rows.find((r) => r.key === selectedKey) ?? rows[0];
  const span: TimelineSpan | undefined = selected ? rowSpan(selected) : undefined;
  const laneEvents = useMemo(
    () => (span && timeline ? lineage(span) : []),
    [span, timeline, lineage]
  );
  const laneTurns = useMemo(
    () => eventsToTurns(laneEvents, isAuditor),
    [laneEvents, isAuditor]
  );
  const rowEls = useRef(new Map<string, HTMLElement>());

  // Transient highlight: pulse the row a sync-jump landed on, then clear.
  const [highlightedUuid, setHighlightedUuid] = useState<string | null>(null);
  const hlTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (hlTimer.current) clearTimeout(hlTimer.current); }, []);

  // Fork points keyed by assistant message id → sibling row keys + our idx.
  const forks = useMemo(
    () => computeForks(rows, selected?.key ?? null),
    [rows, selected?.key]
  );
  const selectLane = (key: string | null): void => {
    if (isAuditor) {
      // Auditor lanes are workbench branches — switching one switches the
      // whole desk. Dispatch `switch`; the resulting `state` broadcast
      // updates `current`, DeskView re-renders with the new `branch` prop,
      // and `defaultKey` follows.
      const r = key != null ? rows.find((r) => r.key === key) : undefined;
      const id = r ? rowSpan(r).id : null;
      if (id != null && id !== branch) {
        send({ t: "switch", branch: id });
        useSession.setState({ current: id, pendingNewAudit: false });
      }
    } else {
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

  const centeredTimestamp = (): string | null => {
    const sc = scrollRef.current;
    if (!sc) return null;
    const mid = sc.getBoundingClientRect().top + sc.clientHeight / 2;
    let best: { d: number; ts: string } | null = null;
    for (const turn of laneTurns) {
      const el = rowEls.current.get(turn.ev.uuid!);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const d = Math.abs((r.top + r.bottom) / 2 - mid);
      if (best == null || d < best.d) best = { d, ts: turn.ev.timestamp };
    }
    return best?.ts ?? null;
  };

  useImperativeHandle(
    ref,
    () => ({
      scrollToTimestamp(ts) {
        let i = bisectTurns(laneTurns, ts);
        while (i >= 0 && !rowEls.current.has(laneTurns[i].ev.uuid!)) i--;
        const el = i >= 0 ? rowEls.current.get(laneTurns[i].ev.uuid!) : undefined;
        if (!el) return;
        stick.current = false;
        el.scrollIntoView({ block: "center", behavior: "auto" });
        setHighlightedUuid(laneTurns[i].ev.uuid!);
        if (hlTimer.current) clearTimeout(hlTimer.current);
        hlTimer.current = setTimeout(() => setHighlightedUuid(null), 1500);
      },
      centeredTimestamp,
    }),
    [laneTurns]
  );

  const syncRaf = useRef<number | null>(null);
  useEffect(() => () => { if (syncRaf.current) cancelAnimationFrame(syncRaf.current); }, []);
  const onScrollLinked = (): void => {
    onScroll();
    if (!linked || !onSync || syncRaf.current != null) return;
    syncRaf.current = requestAnimationFrame(() => {
      syncRaf.current = null;
      const ts = centeredTimestamp();
      if (ts) onSync(ts);
    });
  };

  // Tail-follow on every render (catches streaming partials, not just new events).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  // No timeline yet → linear column (first turn hasn't completed).
  if (!timeline || rows.length === 0) {
    return (
      <LinearColumn ref={ref} branch={branch} role={role} linked={linked} onSync={onSync} />
    );
  }

  const lastEvent = laneEvents[laneEvents.length - 1];
  const lastIsPending =
    lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  const showShimmer = status === "running" && !lastIsPending;

  return (
    <div className="column swimlane-column" ref={scrollRef} onScroll={onScrollLinked}>
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
            highlighted={highlightedUuid === turn.ev.uuid}
            siblingPos={fork && { idx: fork.idx, total: fork.siblings.length }}
            onSwitchSibling={
              fork && anchor != null ? (d) => switchSibling(anchor, d) : undefined
            }
            rowRef={(el) => {
              if (el) rowEls.current.set(turn.ev.uuid!, el);
              else rowEls.current.delete(turn.ev.uuid!);
            }}
          />
        );
      })}
      {!isAuditor && staged.map((m, i) => (
        <Bubble key={m.id ?? `s${i}`} msg={m} ghost byline={`staged · ${m.role}`} />
      ))}
      {queued.map((m, i) => (
        <Bubble key={m.id ?? `q${i}`} msg={m} ghost byline="queued" />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
});
