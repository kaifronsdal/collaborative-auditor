import { type JSX, forwardRef, useMemo } from "react";

import { eventsToTurns, isModelEvent } from "../lib/events";
import {
  useEvents,
  useQueued,
  useStagedForTarget,
  useSwimlanes,
} from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { Bubble } from "./Bubble";
import { ModelEventRow } from "./ModelEventRow";
import { ShimmerBubble } from "./ShimmerBubble";
import { SwimlaneColumn } from "./SwimlaneColumn";
import { useColumnScroll } from "./useColumnScroll";

type Props = {
  branch: BranchId;
  role: Role;
  /** When true, scrolling this column emits `onSync(ts)` (debounced) with the
   *  timestamp of the row nearest the viewport center. */
  linked?: boolean;
  onSync?: (ts: string) => void;
};

export type ColumnHandle = {
  scrollToTimestamp: (ts: string) => void;
  /** Timestamp of the row nearest the viewport center, or null if empty. */
  centeredTimestamp: () => string | null;
};

/**
 * The auditor column: once the server-built session-wide auditor timeline is
 * available, render via the same swimlane/splice path as the target column —
 * `splice()` reconstructs the shared prefix from the parent branch's real
 * events. {@link LinearColumn} remains the no-timeline-yet fallback (and
 * `SwimlaneColumn`'s own fallback for the target column).
 */
export const Column = forwardRef<ColumnHandle, Props>(function Column(
  props,
  ref
): JSX.Element {
  const { branch, role } = props;
  const { timeline, rows } = useSwimlanes(branch, role);
  if (role === "auditor" && timeline && rows.length > 0) {
    return <SwimlaneColumn ref={ref} {...props} />;
  }
  return <LinearColumn ref={ref} {...props} />;
});

/** Linear (non-swimlane) renderer over `byRole[branch][role]`. */
export const LinearColumn = forwardRef<ColumnHandle, Props>(function LinearColumn(
  { branch, role, linked, onSync },
  ref
): JSX.Element {
  const events = useEvents(branch, role);
  const queued = useQueued(branch, role);
  const staged = useStagedForTarget(branch);
  const status = useSession((s) => s.status);

  // One Turn per ModelEvent. Auditor has real ToolEvents (`hasToolEvents`),
  // target relies on resolveMessages pairing instead.
  const turns = useMemo(
    () => eventsToTurns(events, role === "auditor"),
    [events, role]
  );
  const { scrollRef, onScroll, rowRef, highlighted } = useColumnScroll(
    turns, { linked, onSync }, ref
  );

  // Show a shimmer at the column tail in two cases:
  //  1. Running but no pending (streaming) event yet — a generate is expected.
  //  2. Paused with empty column — just-started skeleton (PENDING_ID or PENDING_BRANCH).
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const lastIsPending = lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  const showShimmer =
    (status === "running" && !lastIsPending) ||
    (status === "paused" && events.length === 0);

  return (
    <div className="column" ref={scrollRef} onScroll={onScroll}>
      <div className="column-head">{role}</div>
      {turns.map((turn, i) => (
        <ModelEventRow
          key={turn.ev.uuid}
          turn={turn}
          turnIndex={i}
          auditor={role === "auditor"}
          highlighted={highlighted === turn.ev.uuid}
          rowRef={rowRef(turn.ev.uuid!)}
        />
      ))}
      {role === "target" && staged.map((m, i) => (
        <Bubble key={m.id ?? `s${i}`} msg={m} ghost byline={`staged · ${m.role}`} />
      ))}
      {queued.map((m, i) => (
        <Bubble key={m.id ?? `q${i}`} msg={m} ghost byline="queued" />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
});
