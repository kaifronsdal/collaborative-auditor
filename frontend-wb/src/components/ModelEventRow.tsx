import type { ModelEvent } from "@tsmono/inspect-common";

import type { JSX } from "react";

import { Bubble, renderContent } from "./Bubble";

type Props = {
  ev: ModelEvent;
  /** `input.length` of the previous same-role ModelEvent — the tail before it
   *  is what this turn newly added (STREAMING.md §D). */
  prevInputLen: number;
};

export function ModelEventRow({ ev, prevInputLen }: Props): JSX.Element {
  const tail = ev.input.slice(prevInputLen);
  const choice = ev.output.choices[0];
  const content = choice?.message.content;
  const toolCalls = choice?.message.tool_calls ?? [];

  return (
    <div className="model-row">
      {tail.map((m, i) => (
        <Bubble key={m.id ?? i} msg={m} byline={m.role} />
      ))}

      <div className="bubble-wrap">
        <div className="bubble assistant">
          {content != null && renderContent(content)}
          {/* compact one-liner per tool call — the full args/result live in the
              ToolEventRow below, so we don't dump raw JSON inline. */}
          {toolCalls.map((tc) => (
            <span key={tc.id} className="tool-call-inline">
              → {tc.function}
            </span>
          ))}
          {ev.pending && <span className="cursor" />}
        </div>
        <div className="bubble-by">assistant</div>
      </div>

      <div className="actions">
        <button disabled title="not implemented (M0)">resample</button>
        <button disabled title="not implemented (M0)">edit</button>
        <button disabled title="not implemented (M0)">branch</button>
      </div>
    </div>
  );
}
