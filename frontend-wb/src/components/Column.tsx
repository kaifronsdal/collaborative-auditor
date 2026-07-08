import { type JSX, forwardRef, useMemo } from "react";

import type { ChatMessage } from "@tsmono/inspect-common";

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
import { contentText } from "./tool-renderers/util";
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
  const rawEvents = useEvents(branch, role);
  const queued = useQueued(branch, role);
  const staged = useStagedForTarget(branch);
  const status = useSession((s) => s.status);
  const generating = useSession((s) => s.generating);

  // RACE-FIXES.md R3 / WS-race #15: drop retracted (interrupted) generates —
  // the backend flips `rewound: true` on the stuck pending event via
  // `retract_pending`; leaving it in would misalign `eventsToTurns`
  // (one extra `models[]` entry with no matching assistant message).
  const events = useMemo(
    () => rawEvents.filter((e) => (e as { rewound?: boolean }).rewound !== true),
    [rawEvents]
  );

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
  //  1. This column is mid-generate but no pending (streaming) event yet.
  //     RACE-FIXES.md R3 gap #3,4: gate on `generating === role` so the
  //     auditor column doesn't shimmer while the *target* is generating.
  //     Fall back to `status === "running"` when the backend didn't send
  //     `generating` (old wire protocol).
  //  2. Paused with empty column — just-started skeleton (PENDING_ID or PENDING_BRANCH).
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const lastIsPending = lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  const isGenerating =
    generating === undefined ? status === "running" : generating === role;
  const showShimmer =
    (isGenerating && !lastIsPending) ||
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
        <QueuedBubble key={m.id ?? `q${i}`} branch={branch} role={role} msg={m} />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
});

/**
 * A queued (injected, not-yet-consumed) ghost bubble with hover actions:
 * × removes it from the branch's queue; edit (auditor only — the sole column
 * with a composer) unqueues then hands the text back to the composer via
 * `composerDraft` so the operator can revise and re-send.
 */
export function QueuedBubble({
  branch,
  role,
  msg,
}: {
  branch: BranchId;
  role: Role;
  msg: ChatMessage;
}): JSX.Element {
  const unqueue = useSession((s) => s.unqueue);
  const setComposerDraft = useSession((s) => s.setComposerDraft);
  // Actions need a stable id to target the backend queue entry; injected
  // messages always carry one (DeskView mints a uuid), but guard anyway.
  const canAct = msg.id != null && role !== "orch";
  const canEdit = canAct && role === "auditor";
  return (
    <div className="lead-wrap">
      <Bubble msg={msg} ghost byline="queued" />
      {canAct && (
        <div className="msg-actions queued-actions">
          {canEdit && (
            <button
              title="edit — unqueue and return text to the composer"
              onClick={() => {
                unqueue(branch, role as "auditor" | "target", msg.id!);
                setComposerDraft(contentText(msg.content));
              }}
            >
              <i className="bi bi-pencil" />
            </button>
          )}
          <button
            title="remove from queue"
            onClick={() => unqueue(branch, role as "auditor" | "target", msg.id!)}
          >
            <i className="bi bi-x-lg" />
          </button>
        </div>
      )}
    </div>
  );
}
