import { type JSX, useEffect, useLayoutEffect, useRef } from "react";

import { isModelEvent } from "../lib/events";
import { useEvents, useQueued } from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { Bubble } from "./Bubble";
import { EventRow } from "./EventRow";

type Props = { branch: BranchId; role: Role };

export function Column({ branch, role }: Props): JSX.Element {
  const events = useEvents(branch, role);
  const queued = useQueued(branch, role);

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
  });

  // On first mount, pin to bottom regardless.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // prevInputLen[i] = input.length of the most recent ModelEvent before i.
  let prevModelInputLen = 0;

  return (
    <div className="column" ref={scrollRef} onScroll={onScroll}>
      <div className="column-head">{role}</div>
      {events.map((ev) => {
        const prevInputLen = prevModelInputLen;
        if (isModelEvent(ev)) prevModelInputLen = ev.input.length;
        return <EventRow key={ev.uuid} ev={ev} prevInputLen={prevInputLen} />;
      })}
      {queued.map((m, i) => (
        <Bubble key={m.id ?? `q${i}`} msg={m} ghost byline="queued" />
      ))}
    </div>
  );
}
