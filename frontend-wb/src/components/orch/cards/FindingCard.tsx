/**
 * `FindingCard` — a resolved `wb.cite` receipt (UI-AUDIT.md §A). Thin: icon,
 * claim, quote count, `open` link. The full quote list lives in the sidebar
 * bundle; this card is the in-column acknowledgement.
 *
 * Payload from `Finding._repr_mimebundle_` (workbench/m1/cite.py):
 * `{kind, id, claim, quotes: [{sample_id, at, role, text}], signed_by}`.
 */
import type { JSX } from "react";
import type { Up } from "../../../lib/wire";
import type { Quote } from "./GateCard";

export type FindingPayload = {
  kind: "finding";
  id: string;
  claim: string;
  quotes: Quote[];
  signed_by: string | null;
};

type Props = {
  payload: FindingPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function FindingCard({ payload, send }: Props): JSX.Element {
  const first = payload.quotes[0];
  // Best-effort "open": jump the desk to the first supporting quote.
  const open = (): void => {
    if (first) send({ t: "import_running", sample_id: first.sample_id });
  };
  return (
    <div className="out finding">
      <div className="out-head">
        <i
          className="bi bi-bookmark-check-fill"
          style={{ color: payload.signed_by ? "var(--ok)" : "var(--ink-faint)" }}
        />
        <span className="gate-resolved">
          {payload.claim} · <b>{payload.quotes.length}</b> quotes
          {payload.signed_by && ` · ${payload.signed_by}`}
        </span>
        <span className="out-actions">
          <a onClick={open}>open</a>
        </span>
      </div>
    </div>
  );
}
