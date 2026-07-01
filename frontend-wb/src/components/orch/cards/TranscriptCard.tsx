/**
 * `wb.transcript` — a *pointer* into a `.eval` sample, rendered as an
 * embedded viewer. Payload from `TranscriptRef._repr_mimebundle_`
 * (workbench/m1/read.py): `{kind, log, sample_id, at}` — note there is **no**
 * `messages` list (the model sees a one-line `text/plain`; the human is
 * meant to see inspect-view).
 *
 * M0 has no mountable inspect-view component yet (the `SwimlaneColumn`
 * consumes the live event pipe, not a `.eval` file), so this is the
 * `.iv-embed` shell from scenario-b turn 1 with an "open in DeskView" action
 * that imports the sample into the M0 desk. The iframe path
 * (`/logs/?log_file=…&sample_id=…`) is left as a TODO for when the server
 * serves `inspect view --port` alongside the WS.
 */
import type { JSX } from "react";
import type { Up } from "../../../lib/wire";

export type TranscriptPayload = {
  kind: "transcript";
  log: string;
  sample_id: string;
  at: number | null;
};

type Props = {
  payload: TranscriptPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function TranscriptCard({ payload, send }: Props): JSX.Element {
  const openInDesk = (): void =>
    send({ t: "import", path: payload.log, sample_id: payload.sample_id });

  return (
    <div className="out">
      <div className="out-head">
        <span className="out-kind">transcript</span>
        <span className="out-meta">
          {payload.sample_id}
          {payload.at != null && ` · scrolled to t${payload.at}`}
        </span>
        <span className="out-actions">
          <button type="button" title="open full" onClick={openInDesk}>
            <i className="bi bi-box-arrow-up-right" />
          </button>
        </span>
      </div>
      <div className="iv-embed">
        <div className="iv-msg">
          <div className="iv-gutter">
            <span className="iv-role">log</span>
          </div>
          <div className="iv-body">
            <code>{payload.log}</code>
            <div style={{ marginTop: 6, color: "var(--ink-faint)" }}>
              embedded inspect-view not yet mounted — open in DeskView to read
            </div>
          </div>
        </div>
      </div>
      <div className="iv-foot">
        <span className="iv-url">{payload.log}</span>
        <a onClick={openInDesk}>
          open in DeskView <i className="bi bi-arrow-right" />
        </a>
      </div>
    </div>
  );
}
