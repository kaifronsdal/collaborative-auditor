import {
  type JSX,
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
} from "react";

import { bisectTurns, eventsToTurns, isModelEvent } from "../lib/events";
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

  const scrollRef = useRef<HTMLDivElement>(null);
  // Follow the live edge: stick to the bottom as new content streams in, but
  // only while the user is already near the bottom — if they've scrolled up to
  // read history, don't yank them back down.
  const stick = useRef(true);

  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  // Follow the live tail on every render where content could have grown —
  // including streaming `update`s that mutate the last event in place (so
  // `events.length` is unchanged). `events` itself is replaced on every
  // reducer update (assignByRole clones the array), so depending on the
  // reference catches both new events and partial-output flushes.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  // On first mount, pin to bottom regardless.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // One Turn per ModelEvent. Auditor has real ToolEvents (`hasToolEvents`),
  // target relies on resolveMessages pairing instead.
  const turns = useMemo(
    () => eventsToTurns(events, role === "auditor"),
    [events, role]
  );
  const rowEls = useRef(new Map<string, HTMLElement>());

  const centeredTimestamp = (): string | null => {
    const sc = scrollRef.current;
    if (!sc) return null;
    const mid = sc.getBoundingClientRect().top + sc.clientHeight / 2;
    let best: { d: number; ts: string } | null = null;
    for (const turn of turns) {
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
        let i = bisectTurns(turns, ts);
        while (i >= 0 && !rowEls.current.has(turns[i].ev.uuid!)) i--;
        const el = i >= 0 ? rowEls.current.get(turns[i].ev.uuid!) : undefined;
        if (!el) return;
        stick.current = false;
        el.scrollIntoView({ block: "center", behavior: "auto" });
      },
      centeredTimestamp,
    }),
    [turns]
  );

  // Linked-scroll: lockstep — emit on every scroll frame (rAF-coalesced so we
  // don't thrash on high-frequency wheel events, but no perceptible delay).
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

  // Show a shimmer at the column tail in two cases:
  //  1. Running but no pending (streaming) event yet — a generate is expected.
  //  2. Paused with empty column — just-started skeleton (PENDING_ID or PENDING_BRANCH).
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const lastIsPending = lastEvent != null && isModelEvent(lastEvent) && !!lastEvent.pending;
  const showShimmer =
    (status === "running" && !lastIsPending) ||
    (status === "paused" && events.length === 0);

  return (
    <div className="column" ref={scrollRef} onScroll={onScrollLinked}>
      <div className="column-head">{role}</div>
      {turns.map((turn, i) => (
        <ModelEventRow
          key={turn.ev.uuid}
          turn={turn}
          turnIndex={i}
          auditor={role === "auditor"}
          rowRef={(el) => {
            if (el) rowEls.current.set(turn.ev.uuid!, el);
            else rowEls.current.delete(turn.ev.uuid!);
          }}
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
