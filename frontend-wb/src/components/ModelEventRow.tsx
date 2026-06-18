import type { ModelEvent } from "@tsmono/inspect-common";

import type { JSX } from "react";

import { Bubble, renderContent } from "./Bubble";

type Props = {
  ev: ModelEvent;
  /** `input.length` of the previous same-role ModelEvent — the tail before it
   *  is what this turn newly added (STREAMING.md §D). */
  prevInputLen: number;
  /** If true, show the auditor-column lighter action set (raw · copy only). */
  auditor?: boolean;
};

export function ModelEventRow({ ev, prevInputLen, auditor }: Props): JSX.Element {
  const tail = ev.input.slice(prevInputLen);
  const choice = ev.output.choices[0];
  const content = choice?.message.content;
  const toolCalls = choice?.message.tool_calls ?? [];

  return (
    <div className="model-event-row">
      {tail.map((m, i) => (
        <Bubble key={m.id ?? i} msg={m} byline={m.role} />
      ))}

      <div className="bubble-wrap">
        <div className="bubble assistant">
          {content != null && renderContent(content)}
          {toolCalls.map((tc) => (
            <span key={tc.id} className="tool-call-inline">
              → {tc.function}
            </span>
          ))}
          {ev.pending && <span className="cursor" />}
        </div>
        <div className="bubble-by">assistant</div>
      </div>

      {/* caption action row — always visible at 0.5 opacity, brightens on row hover */}
      <div className="actions">
        {auditor ? (
          <>
            <button disabled title="not implemented (M0)">raw</button>
            <button disabled title="not implemented (M0)">copy</button>
          </>
        ) : (
          <>
            <button disabled title="not implemented (M0)">branch</button>
            <button disabled title="not implemented (M0)">resample</button>
            <button disabled title="not implemented (M0)">edit</button>
            <button disabled title="not implemented (M0)">raw</button>
          </>
        )}
      </div>
    </div>
  );
}
