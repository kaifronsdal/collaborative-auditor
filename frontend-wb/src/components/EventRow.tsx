import type { Event } from "@tsmono/inspect-common";

import type { JSX } from "react";

import { ModelEventRow } from "./ModelEventRow";
import { ToolEventRow } from "./ToolEventRow";

type Props = { ev: Event; prevInputLen: number };

export function EventRow({ ev, prevInputLen }: Props): JSX.Element | null {
  switch (ev.event) {
    case "model":
      return <ModelEventRow ev={ev} prevInputLen={prevInputLen} />;
    case "tool":
      return <ToolEventRow ev={ev} />;
    case "span_begin":
    case "span_end":
      return null;
    default:
      return <pre className="event-stub">{JSON.stringify(ev, null, 2)}</pre>;
  }
}
