import type { JSX } from "react";

import { isModelEvent } from "../lib/events";
import { useEvents, useQueued } from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { Bubble } from "./Bubble";
import { EventRow } from "./EventRow";

type Props = { branch: BranchId; role: Role };

export function Column({ branch, role }: Props): JSX.Element {
  const events = useEvents(branch, role);
  const queued = useQueued(branch, role);

  // prevInputLen[i] = input.length of the most recent ModelEvent before i.
  let prevModelInputLen = 0;

  return (
    <div className="column">
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
