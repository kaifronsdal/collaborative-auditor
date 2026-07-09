/**
 * `FindingCard` — a resolved `wb.cite` receipt (UI-AUDIT.md §A). Thin: icon,
 * claim, quote count, `open` link. The full quote list lives in the sidebar
 * bundle; this card is the in-column acknowledgement.
 *
 * Payload from `Finding._repr_mimebundle_` (workbench/m1/cite.py):
 * `{kind, id, claim, quotes: [{sample_id, at, role, text, log}], signed_by}`.
 */
import type { JSX } from "react";
import { FORK_KINDS, useIsPending } from "../../../lib/selectors";
import type { Up } from "../../../lib/wire";
import type { FindingPayload } from "../types";

type Props = {
  payload: FindingPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function FindingCard({ payload, send }: Props): JSX.Element {
  const first = payload.quotes[0];
  const forkPending = useIsPending((c) => FORK_KINDS.has(c.t));
  // Quotes reference *finished* samples → `{t:"import", path: .eval}` (not
  // `import_running`, which needs a `log_dir` to find the ACP socket).
  const open = first?.log
    ? () => send({ t: "import", path: first.log!, sample_id: first.sample_id })
    : undefined;
  return (
    <div className="out finding">
      <div className="out-head hstack g8">
        <i
          className="bi bi-bookmark-check-fill"
          style={{ color: payload.signed_by ? "var(--ok)" : "var(--ink-faint)" }}
        />
        <span className="truncate">
          {payload.claim} · <b>{payload.quotes.length}</b> quotes
          {payload.signed_by && ` · ${payload.signed_by}`}
        </span>
        {open && (
          <span className="out-actions">
            <button type="button" onClick={open} disabled={forkPending}>
              open
            </button>
          </span>
        )}
      </div>
    </div>
  );
}
