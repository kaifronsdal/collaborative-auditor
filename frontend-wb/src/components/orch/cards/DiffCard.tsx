/**
 * `DiffCard` — P3 run diff (`wb.diff(a, b)` → `DiffHandle`). Static result
 * card (no polling): `a_task vs b_task` header, an `.fx-out` counter line
 * (N joined · N flipped · only-a/b), one `.scan-row` per scorer with its
 * mean delta, and a `.scan-df` HTML preview of the flipped samples. Reuses
 * the `ProgressCard` scan-variant classes so the visual language matches.
 */
import type { JSX } from "react";

import type { Up } from "../../../lib/wire";
import type { DiffPayload } from "../types";

type Props = {
  payload: DiffPayload;
  displayId: string;
  send: (msg: Up) => void;
};

const fmtDelta = (d: number | null): string =>
  d == null ? "—" : `${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(3)}`;

export default function DiffCard({ payload, displayId }: Props): JSX.Element {
  const s = payload.summary;
  const scorers = Object.entries(s.mean_delta);
  return (
    <div className="out" data-display-id={displayId}>
      <div className="out-head hstack g8">
        <i className="bi bi-record-fill fx-dot" />
        <span className="out-task">diff</span>
        <span className="out-meta truncate">
          {payload.a_task} <span className="sep">vs</span> {payload.b_task}
        </span>
      </div>

      <div className="fx-out hstack g6">
        <span className="stat">
          <b>{payload.n}</b> samples
        </span>
        <span className="sep">·</span>
        <span className={`stat${payload.n_flipped > 0 ? " err" : ""}`}>
          <b>{payload.n_flipped}</b> flipped
        </span>
        {s.n_only_a > 0 && (
          <>
            <span className="sep">·</span>
            <span className="stat" title={`only in ${payload.a_task}`}>
              only-a <b>{s.n_only_a}</b>
            </span>
          </>
        )}
        {s.n_only_b > 0 && (
          <>
            <span className="sep">·</span>
            <span className="stat" title={`only in ${payload.b_task}`}>
              only-b <b>{s.n_only_b}</b>
            </span>
          </>
        )}
      </div>

      {scorers.length > 0 && (
        <>
          <div className="pc-cols hstack g10">
            <span className="pc-col" style={{ flex: 1 }}>
              scorer
            </span>
            <span className="pc-col diff-delta">mean Δ (b − a)</span>
          </div>
          <div className="scan-rows">
            {scorers.map(([name, d]) => (
              <div key={name} className="scan-row hstack g10">
                <span className="scan-name truncate">{name}</span>
                <span className={`stat diff-delta${d != null && d < 0 ? " neg" : ""}`}>
                  <b>{fmtDelta(d)}</b>
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="scan-df">
        <div
          className="scan-df-head"
          dangerouslySetInnerHTML={{ __html: payload.df_head }}
        />
      </div>
      <div className="fx-more hstack g8">
        <span className="fx-more-note">
          {payload.n_flipped > 0 ? "flipped samples" : "sample"} · <code>.df</code> for
          the full join
        </span>
      </div>
    </div>
  );
}
