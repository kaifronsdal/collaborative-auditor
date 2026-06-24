/**
 * Target column rendered from petri's server-built `Timeline` via inspect-view's
 * swimlane layout — the same data path inspect-view uses for petri transcripts
 * (`convertServerTimeline` → `computeFlatSwimlaneRows({showBranches:true})`).
 *
 * Layout: a thin lane rail (one row per trajectory, branch rows indented and
 * marked with their fork point) above the conversation. Selecting a lane shows
 * that trajectory's full lineage via `splice(root, span)` — i.e. the parent
 * prefix up to the fork + the branch's own turns, exactly what an unbranched
 * run of that lineage would have produced.
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
  type TimelineSpan,
} from "@tsmono/inspect-components/transcript/timeline";
import { TimelineSwimLanes } from "@tsmono/inspect-components/transcript/timeline/swimlanes";

import { bisectTurns, eventsToTurns, isModelEvent } from "../lib/events";
import { useQueued, useStagedForTarget, useSwimlanes } from "../lib/selectors";
import type { BranchId } from "../lib/wire";
import { useSession } from "../store/session";
import { Bubble } from "./Bubble";
import { Column, type ColumnHandle } from "./Column";
import { ModelEventRow } from "./ModelEventRow";
import { ShimmerBubble } from "./ShimmerBubble";

type Props = { branch: BranchId; linked?: boolean; onSync?: (ts: string) => void };

export const SwimlaneColumn = forwardRef<ColumnHandle, Props>(function SwimlaneColumn(
  { branch, linked, onSync },
  ref
): JSX.Element {
  const { timeline, rows, layouts, lineage } = useSwimlanes(branch, "target");
  const queued = useQueued(branch, "target");
  const staged = useStagedForTarget(branch);
  const status = useSession((s) => s.status);

  // Selected lane key. Default to the deepest (latest) branch row, falling
  // back to root — that's "what the target is currently seeing".
  const defaultKey = useMemo(
    () => rows.length > 0 ? rows[rows.length - 1].key : null,
    [rows]
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(defaultKey);
  useEffect(() => {
    // Follow the live tip when new branch rows appear (rollback) and the user
    // hasn't picked a lane explicitly, or their selection no longer exists.
    if (selectedKey == null || !rows.some((r) => r.key === selectedKey)) {
      setSelectedKey(defaultKey);
    }
  }, [defaultKey, rows, selectedKey]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // Resolve the selected row's TimelineSpan and its full event lineage.
  const selected = rows.find((r) => r.key === selectedKey) ?? rows[0];
  const span: TimelineSpan | undefined = selected
    ? getAgents(selected.spans[0])[0]
    : undefined;
  const laneEvents = useMemo(
    () => (span && timeline ? lineage(span) : []),
    [span, timeline, lineage]
  );
  const laneTurns = useMemo(() => eventsToTurns(laneEvents, false), [laneEvents]);
  const rowEls = useRef(new Map<string, HTMLElement>());

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

  // No timeline yet → linear column (first target turn hasn't completed).
  if (!timeline || rows.length === 0) {
    return <Column ref={ref} branch={branch} role="target" linked={linked} onSync={onSync} />;
  }

  const lastEvent = laneEvents[laneEvents.length - 1];
  const lastIsPending =
    lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  const showShimmer = status === "running" && !lastIsPending;

  return (
    <div className="column swimlane-column" ref={scrollRef} onScroll={onScrollLinked}>
      <div className="column-head">
        target
        {rows.length > 1 && (
          <span className="sl-count">{rows.length} trajectories</span>
        )}
      </div>

      {rows.length > 1 && (
        <div className="lane-gantt-host">
          <TimelineSwimLanes
            layouts={layouts}
            timeline={{
              selected: selected?.key ?? null,
              select: (k) => setSelectedKey(k ?? defaultKey),
              clearSelection: () => setSelectedKey(defaultKey),
            }}
            defaultCollapsed={false}
          />
        </div>
      )}

      {laneTurns.map((turn, i) => (
        <ModelEventRow
          key={turn.ev.uuid}
          turn={turn}
          turnIndex={i}
          rowRef={(el) => {
            if (el) rowEls.current.set(turn.ev.uuid!, el);
            else rowEls.current.delete(turn.ev.uuid!);
          }}
        />
      ))}
      {staged.map((m, i) => (
        <Bubble key={m.id ?? `s${i}`} msg={m} ghost byline={`staged · ${m.role}`} />
      ))}
      {queued.map((m, i) => (
        <Bubble key={m.id ?? `q${i}`} msg={m} ghost byline="queued" />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
});

