/**
 * `wb.excerpt` — a small window of transcript messages rendered as M0
 * `<Bubble>`s (not the inspect-view iframe). Payload from
 * `Excerpt._repr_mimebundle_` (workbench/m1/read.py): `{kind, log, sample_id,
 * at, messages: ChatMessage[]}`.
 *
 * The `.out > .excerpt` wrapper gets its header stripped by shared.css's
 * `:has(> .excerpt)` rule, so bubbles self-identify (scenario-b turn 2 /
 * scenario-d turn 6). "open full" imports the sample into the M0 desk.
 */
import type { ChatMessage } from "@tsmono/inspect-common";

import type { JSX } from "react";

import { Bubble } from "../../Bubble";
import type { Up } from "../../../lib/wire";

export type ExcerptPayload = {
  kind: "excerpt";
  log: string;
  sample_id: string;
  at: number;
  messages: ChatMessage[];
};

type Props = {
  payload: ExcerptPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function ExcerptCard({ payload, send }: Props): JSX.Element {
  const openFull = (): void =>
    send({ t: "import", path: payload.log, sample_id: payload.sample_id });

  return (
    <div className="out">
      <div className="out-head">
        <span className="out-kind">excerpt</span>
        <span className="out-meta">
          {payload.sample_id} · {payload.messages.length} message
          {payload.messages.length === 1 ? "" : "s"} · at t{payload.at}
        </span>
        <span className="out-actions">
          <button type="button" title="open full" onClick={openFull}>
            <i className="bi bi-box-arrow-up-right" />
          </button>
        </span>
      </div>
      <div className="excerpt">
        {payload.messages.map((m, i) => (
          // M0's Bubble already handles role styling, reasoning blocks, and
          // collapsible long user/tool content. `byline` supplies the role tag.
          <Bubble key={m.id ?? i} msg={m} byline={`${m.role} · t${payload.at - Math.floor(payload.messages.length / 2) + i}`} />
        ))}
      </div>
      <div className="excerpt-foot">
        <span className="ef-from">from {payload.sample_id} turn {payload.at}</span>
        <a onClick={openFull}>
          open full <i className="bi bi-arrow-right" />
        </a>
      </div>
    </div>
  );
}
