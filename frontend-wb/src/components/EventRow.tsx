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
    default:
      // Every other inspect event (span_begin/end, state, store, info, step,
      // logger, …) is internal bookkeeping, not conversation content — the M0
      // columns show model output and tool calls only. Dumping the raw JSON
      // (the old behaviour) littered the target column with state/store blobs.
      return null;
  }
}
