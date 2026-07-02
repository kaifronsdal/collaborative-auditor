/**
 * Live `RunHandle` / `AuditRunHandle` card — both `kind:"eval_run"` and
 * `"audit_run"` land here. Payload from `RunHandle._repr_mimebundle_`
 * (workbench/m1/run.py): `{kind, id, task, description, log_dir, log, total,
 * done, finished, error, rows: [{id, status, input, scores}]}`.
 *
 * `.out` with the description subtitle, an `.fx-out` counter line (shared.css
 * collapses it to bar + done/total), and a per-sample row list. Row click on
 * a completed sample imports it into the M0 desk (`{t:"import", path, sample_id}`);
 * on a running sample it adopts the live task via `{t:"import_running"}`.
 *
 * Row classes follow the mockups: `.audit-row` for `audit_run` (scenario-d
 * turn 1 — has `.ar-seed`/`.ar-grade`), `.eval-row` for `eval_run` (shared.css
 * §run_eval — clickable `.er-id`, `.er-score`).
 */
import { useState, type JSX } from "react";
import type { Up } from "../../../lib/wire";

type SampleRow = {
  id: string;
  status: "running" | "done" | "error" | "stopped";
  input: string;
  scores: Record<string, unknown>;
};

export type RunPayload = {
  kind: "audit_run" | "eval_run";
  id: string;
  task: string;
  description: string;
  log_dir: string;
  log: string | null;
  total: number;
  done: number;
  finished: boolean;
  error: string | null;
  /** `RunHandle._repr_mimebundle_` ships `{running: [...], done: [...]}`. */
  rows: { running: SampleRow[]; done: SampleRow[] };
};

type Props = {
  payload: RunPayload;
  displayId: string;
  send: (msg: Up) => void;
};

/** First numeric score, rendered as `·NN` (mockup convention). */
function fmtScore(scores: Record<string, unknown>): string {
  for (const v of Object.values(scores)) {
    if (typeof v === "number") return `·${Math.round(v * 100).toString().padStart(2, "0")}`;
  }
  const keys = Object.keys(scores);
  return keys.length ? String(scores[keys[0]]) : "—";
}

const ROW_CAP = 3;

export default function RunCard({ payload, send }: Props): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  const allRows = [...(payload.rows.running ?? []), ...(payload.rows.done ?? [])];
  const running = payload.rows.running?.length ?? 0;
  const isAudit = payload.kind === "audit_run";

  const onRowClick = (row: SampleRow): void => {
    if (row.status === "running") {
      send({ t: "import_running", sample_id: row.id });
    } else if (payload.log != null) {
      send({ t: "import", path: payload.log, sample_id: row.id });
    }
  };

  const rows = showAll ? allRows : allRows.slice(0, ROW_CAP);
  const hidden = allRows.length - rows.length;

  return (
    <div className="out">
      <div className="out-head">
        <i
          className={`bi bi-record-fill fx-dot${payload.finished ? "" : " pending"}`}
          style={payload.error ? { color: "var(--danger)" } : undefined}
        />
        <span className="out-kind">run · {isAudit ? "petri" : payload.task}</span>
        <span className="out-meta">{payload.id.slice(0, 8)}</span>
        <span className="out-actions">
          <button type="button" title="open log dir">
            <i className="bi bi-folder2-open" />
          </button>
        </span>
      </div>
      {payload.description && <div className="gate-desc">{payload.description}</div>}
      <div className="fx-out">
        <span className="stat">
          <b>{payload.total}</b> {isAudit ? "audits" : "samples"}
        </span>
        <span className="sep">·</span>
        <span className="stat ok">
          <b>{payload.done}</b> done
        </span>
        <span className="sep">·</span>
        <span className="stat run">
          <b>{payload.finished ? 0 : running}</b> running
        </span>
        {payload.error && (
          <span className="stat" style={{ color: "var(--danger)" }}>
            {payload.error}
          </span>
        )}
      </div>

      {allRows.length > 0 && (
        <div className={isAudit ? "audit-rows" : "eval-rows"}>
          {rows.map((row) =>
            isAudit ? (
              <div key={row.id} className="audit-row" onClick={() => onRowClick(row)}>
                <span className="ar-id">{row.id}</span>
                <span className="ar-seed">{row.input}</span>
                <span className={`ar-status ${row.status}`}>{row.status}</span>
                <span className="ar-grade" title={Object.keys(row.scores).join(", ")}>
                  <b>{fmtScore(row.scores)}</b>
                </span>
                <span className="ar-actions">
                  <button
                    type="button"
                    title="open in desk"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRowClick(row);
                    }}
                  >
                    <i className="bi bi-box-arrow-up-right" />
                  </button>
                </span>
              </div>
            ) : (
              <div key={row.id} className="eval-row" onClick={() => onRowClick(row)}>
                <span className="er-id">{row.id}</span>
                <span className="er-score">
                  {Object.entries(row.scores)
                    .map(([k, v]) => `${k}=${String(v)}`)
                    .join(" ") || row.input}
                </span>
                <span className={`ar-status ${row.status}`}>{row.status}</span>
              </div>
            )
          )}
        </div>
      )}

      {hidden > 0 && (
        <div className="fx-more">
          <a onClick={() => setShowAll(true)}>
            {hidden} more <i className="bi bi-chevron-down" />
          </a>
        </div>
      )}
    </div>
  );
}
