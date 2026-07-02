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
  /** Exception class name for `status === "error"` (backend TODO — optional
   *  so the row falls back to the literal `error` when absent). */
  error?: string;
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
  /** Wall-clock elapsed, pre-formatted (`"2m14s"`). Backend TODO. */
  elapsed?: string;
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

/** `"ValueError: bad seed"` → `"ValueError"`. */
const errClass = (e: string | undefined): string | undefined =>
  e?.split(/[:(\n]/, 1)[0].trim() || undefined;

const ROW_CAP = 3;

export default function RunCard({ payload, displayId, send }: Props): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  // Row id most recently opened in the desk — draws the transient
  // `in desk →` chip so the click reads as having done something.
  const [opened, setOpened] = useState<string | null>(null);

  const allRows = [...(payload.rows.running ?? []), ...(payload.rows.done ?? [])];
  const errored = (payload.rows.done ?? []).filter((r) => r.status === "error").length;
  const isAudit = payload.kind === "audit_run";
  const pct = payload.total > 0 ? Math.min(100, (payload.done / payload.total) * 100) : 0;

  const onRowClick = (row: SampleRow): void => {
    if (row.status === "running") {
      send({ t: "import_running", sample_id: row.id });
    } else if (payload.log != null) {
      send({ t: "import", path: payload.log, sample_id: row.id });
    } else {
      return;
    }
    setOpened(row.id);
    window.setTimeout(() => setOpened((cur) => (cur === row.id ? null : cur)), 2000);
  };

  const rows = showAll ? allRows : allRows.slice(0, ROW_CAP);
  const hidden = allRows.length - rows.length;

  const renderRow = (row: SampleRow): JSX.Element => {
    const running = row.status === "running";
    const title = running ? "watch live in desk" : "open transcript in desk";
    const href = `wb://audit/${row.id}`;
    const status =
      row.status === "error" ? (errClass(row.error) ?? "error") : row.status;
    const common = {
      role: "button" as const,
      tabIndex: 0,
      title,
      onClick: () => onRowClick(row),
      onKeyDown: (e: React.KeyboardEvent) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onRowClick(row);
        }
      },
    };
    const dot = <i className={`bi bi-record-fill row-dot row-dot-${row.status}`} />;
    const chip =
      opened === row.id ? (
        <span className="ar-in-desk">
          in desk <i className="bi bi-arrow-right" />
        </span>
      ) : null;

    return isAudit ? (
      <div key={row.id} className="audit-row" {...common}>
        {dot}
        <a
          className="qref ar-id"
          href={href}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onRowClick(row);
          }}
        >
          {row.id}
        </a>
        <span className="ar-seed">{row.input}</span>
        <span className={`ar-status ${row.status}`}>{status}</span>
        <span className="ar-grade" title={Object.keys(row.scores).join(", ")}>
          <b>{fmtScore(row.scores)}</b>
        </span>
        {chip}
      </div>
    ) : (
      <div key={row.id} className="eval-row" {...common}>
        {dot}
        <a
          className="qref er-id"
          href={href}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onRowClick(row);
          }}
        >
          {row.id}
        </a>
        <span className="er-score">
          {Object.entries(row.scores)
            .map(([k, v]) => `${k}=${String(v)}`)
            .join(" ") || row.input}
        </span>
        <span className={`ar-status ${row.status}`}>{status}</span>
        {chip}
      </div>
    );
  };

  return (
    <div className="out" data-display-id={displayId}>
      <div className="out-head">
        <i
          className={`bi bi-record-fill fx-dot${payload.finished ? "" : " pending"}`}
          style={payload.error ? { color: "var(--danger)" } : undefined}
        />
        <span className="out-task">{payload.task.toLowerCase()}</span>
        <span className="out-meta out-id">{payload.id.slice(0, 8)}</span>
        {!payload.finished && <span className="out-live">live</span>}
        <span className="out-actions">
          <button type="button" title="open log dir">
            <i className="bi bi-folder2-open" />
          </button>
        </span>
      </div>
      {payload.description && <div className="gate-desc">{payload.description}</div>}

      <div className="fx-out">
        <span className="fx-bar">
          <i style={{ width: `${pct}%` }} />
        </span>
        <span className="stat">
          <b>{payload.done}</b>/{payload.total}
        </span>
        {errored > 0 && (
          <span className="stat err" style={{ color: "var(--danger)" }}>
            +{errored}
          </span>
        )}
        {payload.finished && payload.elapsed && (
          <>
            <span className="sep">·</span>
            <span className="stat">{payload.elapsed}</span>
          </>
        )}
        {payload.error && (
          <span className="stat" style={{ color: "var(--danger)" }}>
            {errClass(payload.error) ?? payload.error}
          </span>
        )}
      </div>

      {allRows.length > 0 && (
        <div className={isAudit ? "audit-rows" : "eval-rows"}>
          {rows.map(renderRow)}
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
