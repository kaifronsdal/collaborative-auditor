import { type JSX, useEffect, useLayoutEffect, useRef } from "react";

import { isModelEvent } from "../lib/events";
import { useEvents, useQueued } from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { Bubble } from "./Bubble";
import { EventRow } from "./EventRow";
import { ShimmerBubble } from "./ShimmerBubble";

type Props = { branch: BranchId; role: Role };

export function Column({ branch, role }: Props): JSX.Element {
  const events = useEvents(branch, role);
  const queued = useQueued(branch, role);
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

  // useLayoutEffect so the scroll lands before paint (no visible jump). Depends
  // on the rendered content length so it fires on every streaming flush.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events.length, queued.length]);

  // On first mount, pin to bottom regardless.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // prevInputLen[i] = input.length of the most recent ModelEvent before i.
  let prevModelInputLen = 0;

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
      {events.map((ev) => {
        const prevInputLen = prevModelInputLen;
        if (isModelEvent(ev)) prevModelInputLen = ev.input.length;
        return <EventRow key={ev.uuid} ev={ev} prevInputLen={prevInputLen} role={role} />;
      })}
      {queued.map((m, i) => (
        <Bubble key={m.id ?? `q${i}`} msg={m} ghost byline="queued" />
      ))}
      {showShimmer && <ShimmerBubble />}
    </div>
  );
}
