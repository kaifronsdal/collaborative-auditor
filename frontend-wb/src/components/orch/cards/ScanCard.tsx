/**
 * `wb.scan` progress card. Payload from `ScanHandle._repr_mimebundle_`
 * (workbench/m1/run.py): `{kind, id, description, scans_dir, location, done,
 * total, finished, error, per_scanner: {name: {scans, results, errors}}}`.
 *
 * One `.out.scan` per scanner (scenario-b/d) — compact one-liner with a bar.
 * `total` is 0 until the job settles (see the `ScanHandle` docstring), so the
 * bar is indeterminate while running and fills to `done/done` on finish.
 * The `ScanResultsDF` never leaves the kernel, so on finish we show a
 * `location` link rather than a `.df` head.
 */
import type { JSX } from "react";
import type { Up } from "../../../lib/wire";

export type ScanPayload = {
  kind: "scan";
  id: string;
  description: string;
  scans_dir: string;
  location: string | null;
  done: number;
  total: number;
  finished: boolean;
  error: string | null;
  per_scanner: Record<string, { scans: number; results: number; errors: number }>;
};

type Props = {
  payload: ScanPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function ScanCard({ payload }: Props): JSX.Element {
  const names = Object.keys(payload.per_scanner);
  // Before the first poll `per_scanner` is empty — render one row from the
  // aggregate so the card isn't blank.
  const entries: Array<[string, { scans: number; errors: number }]> =
    names.length > 0
      ? names.map((n) => [n, payload.per_scanner[n]])
      : [[payload.description || "scan", { scans: payload.done, errors: 0 }]];

  return (
    <>
      {entries.map(([name, s]) => {
        const total = payload.total || (payload.finished ? s.scans : 0);
        const pct = total > 0 ? Math.min(100, (s.scans / total) * 100) : payload.finished ? 100 : 35;
        return (
          <div
            key={name}
            className={`out scan${payload.finished ? " done" : ""}`}
          >
            <div className="out-head">
              <i
                className="bi bi-funnel"
                style={payload.error ? { color: "var(--danger)" } : undefined}
              />
              <span className="scan-name">{name}</span>
              <span className="scan-prog">
                <span className="scan-bar">
                  <i style={{ width: `${pct}%` }} />
                </span>
                {total > 0 ? `${s.scans}/${total}` : `${s.scans}`}
                {s.errors > 0 && (
                  <span style={{ color: "var(--danger)" }}>({s.errors} err)</span>
                )}
              </span>
              {payload.location && <span className="out-path">→ {payload.location}</span>}
            </div>
            {payload.error && (
              <div className="fx-body" style={{ color: "var(--danger)" }}>
                {payload.error}
              </div>
            )}
          </div>
        );
      })}
      {payload.finished && !payload.error && payload.location && (
        <div className="fx-more">
          <a title={payload.location}>
            view results <i className="bi bi-table" />
          </a>
        </div>
      )}
    </>
  );
}
