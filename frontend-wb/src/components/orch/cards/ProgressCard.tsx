/**
 * `ProgressCard` — the shared live-job shell (UI-AUDIT.md §D). One `.out`
 * per handle: `.fx-dot` + task/id header, `.gate-desc` subtitle, an `.fx-out`
 * counter line (bar · `done/total` · `+err` · elapsed), and a variant row
 * list with `{N} more`/`collapse`.
 *
 * Variants (dispatched on `payload.kind`):
 * - `audit_run`/`eval_run` — one row per sample. Row click imports it into
 *   the M0 desk (`{t:"import"}` for finished, `{t:"import_running"}` for
 *   live). Opened rows keep a persistent `↗` glyph.
 * - `scan` — one row *per scanner* (not one card per scanner). Location in
 *   the footer; on finish each scanner's `df_head` HTML renders inline.
 */
import { useRef, useState, type JSX } from "react";
import type { Up } from "../../../lib/wire";

// -- payload shapes -----------------------------------------------------------

type SampleRow = {
  id: string;
  status: "running" | "done" | "error" | "stopped";
  input: string;
  turns?: number | null;
  error?: string | null;
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
  elapsed?: string;
  rows: { running: SampleRow[]; done: SampleRow[] };
};

type ScannerStat = { scans: number; results: number; errors: number };

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
  elapsed?: string;
  per_scanner: Record<string, ScannerStat>;
  /** name → 3-row HTML `<table>` (`df.head(3).to_html()`), shipped once
   *  `finished` (UI-AUDIT §C). */
  df_head?: Record<string, string>;
};

export type ProgressPayload = RunPayload | ScanPayload;

type Props = {
  payload: ProgressPayload;
  displayId: string;
  send: (msg: Up) => void;
};

const ROW_CAP = 3;

/** `"ValueError: bad seed"` → `"ValueError"`. */
const errClass = (e: string | null | undefined): string =>
  e?.split(/[:(\n]/, 1)[0].trim() || "error";

const basename = (p: string): string => p.replace(/\/+$/, "").split("/").pop() ?? p;

/** First numeric score, rendered as `·NN` (mockup convention). */
function fmtScore(scores: Record<string, unknown>): string {
  for (const v of Object.values(scores)) {
    if (typeof v === "number") return `·${Math.round(v * 100).toString().padStart(2, "0")}`;
  }
  const keys = Object.keys(scores);
  return keys.length ? String(scores[keys[0]]) : "—";
}

// -- shared shell -------------------------------------------------------------

export default function ProgressCard({ payload, displayId, send }: Props): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  // Rows the user has opened in the desk — persistent `↗` glyph, survives the
  // 2s highlight and re-renders (UI-AUDIT §C).
  const opened = useRef<Set<string>>(new Set());
  const [, bump] = useState(0);

  const markOpened = (id: string): void => {
    opened.current.add(id);
    bump((n) => n + 1);
  };

  const pct = payload.total > 0 ? Math.min(100, (payload.done / payload.total) * 100) : 0;

  // Variant supplies the row list + errored count + optional footer; the
  // shell owns show-all/collapse and the counter line.
  let title: string;
  let allRows: JSX.Element[];
  let errored: number;
  let footer: JSX.Element | null = null;

  if (payload.kind === "scan") {
    title = "scan";
    const names = Object.keys(payload.per_scanner);
    // Before the first poll `per_scanner` is empty — synthesize one row from
    // the aggregate so the card isn't blank.
    const rows: Array<{ name: string } & ScannerStat> =
      names.length > 0
        ? names.map((n) => ({ name: n, ...payload.per_scanner[n] }))
        : [{ name: payload.description || "scan", scans: payload.done, results: 0, errors: 0 }];
    errored = rows.reduce((a, r) => a + r.errors, 0);
    allRows = rows.map((r) => <ScanRow key={r.name} row={r} total={payload.total} />);
    footer = (
      <>
        {payload.finished && payload.df_head && (
          <div className="scan-df">
            {Object.entries(payload.df_head).map(([name, html]) => (
              <div
                key={name}
                className="scan-df-head"
                dangerouslySetInnerHTML={{ __html: html }}
              />
            ))}
          </div>
        )}
        {payload.location && (
          <div className="fx-more">
            <span className="fx-more-note" title={payload.location}>
              → {basename(payload.location)}
            </span>
          </div>
        )}
      </>
    );
  } else {
    title = payload.task.toLowerCase();
    const rows = [...(payload.rows.running ?? []), ...(payload.rows.done ?? [])];
    errored = (payload.rows.done ?? []).filter((r) => r.status === "error").length;
    const audit = payload.kind === "audit_run";
    const log = payload.log;
    const onRowClick = (row: SampleRow): void => {
      if (row.status === "running") {
        send({ t: "import_running", sample_id: row.id });
      } else if (log != null) {
        send({ t: "import", path: log, sample_id: row.id });
      } else {
        return;
      }
      markOpened(row.id);
    };
    allRows = rows.map((r) => (
      <RunRow
        key={r.id}
        row={r}
        audit={audit}
        opened={opened.current.has(r.id)}
        onClick={() => onRowClick(r)}
      />
    ));
  }

  const shown = showAll ? allRows : allRows.slice(0, ROW_CAP);
  const hidden = allRows.length - shown.length;

  return (
    <div className="out" data-display-id={displayId}>
      <div className="out-head">
        <i
          className={`bi bi-record-fill fx-dot${payload.finished ? "" : " pending"}`}
          style={payload.error ? { color: "var(--danger)" } : undefined}
        />
        <span className="out-task">{title}</span>
        <span className="out-meta out-id">{payload.id.slice(0, 8)}</span>
      </div>
      {payload.description && <div className="gate-desc">{payload.description}</div>}

      <div className="fx-out">
        {payload.finished && !payload.error ? (
          <span className="stat ok">
            <i className="bi bi-check2" /> done
          </span>
        ) : (
          <span className="fx-bar">
            <i style={{ width: `${pct}%` }} />
          </span>
        )}
        <span className="stat">
          <b>{payload.done}</b>/{payload.total || "?"}
        </span>
        {errored > 0 && (
          <span className="stat err" style={{ color: "var(--danger)" }}>
            +{errored}
          </span>
        )}
        {payload.elapsed && (
          <>
            <span className="sep">·</span>
            <span className="stat">{payload.elapsed}</span>
          </>
        )}
        {payload.error && (
          <span className="stat" style={{ color: "var(--danger)" }}>
            {errClass(payload.error)}
          </span>
        )}
      </div>

      {allRows.length > 0 && (
        <div className={payload.kind === "scan" ? "scan-rows" : "audit-rows"}>{shown}</div>
      )}

      {(hidden > 0 || (showAll && allRows.length > ROW_CAP)) && (
        <div className="fx-more">
          <a onClick={() => setShowAll((v) => !v)}>
            {showAll ? (
              <>
                collapse <i className="bi bi-chevron-up" />
              </>
            ) : (
              <>
                {hidden} more <i className="bi bi-chevron-down" />
              </>
            )}
          </a>
        </div>
      )}

      {footer}
    </div>
  );
}

// -- run row (audit_run / eval_run) -------------------------------------------

function RunRow({
  row,
  audit,
  opened,
  onClick,
}: {
  row: SampleRow;
  audit: boolean;
  opened: boolean;
  onClick: () => void;
}): JSX.Element {
  // Status slot per UI-AUDIT §C: `done` → nothing (dot suffices); `running` →
  // `t{turns}`; `error` → exception class; `stopped` → literal.
  const status =
    row.status === "done" ? null : row.status === "running" ? (
      <span className="ar-turns">t{row.turns ?? "…"}</span>
    ) : row.status === "error" ? (
      <span className="ar-status error">{errClass(row.error)}</span>
    ) : (
      <span className="ar-status stopped">stopped</span>
    );

  return (
    <div
      className={audit ? "audit-row" : "eval-row"}
      role="button"
      tabIndex={0}
      title={row.status === "running" ? "watch live in desk" : "open transcript in desk"}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
    >
      <i className={`bi bi-record-fill row-dot row-dot-${row.status}`} />
      <a
        className={`qref ${audit ? "ar-id" : "er-id"}`}
        href={`wb://audit/${row.id}`}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onClick();
        }}
      >
        {row.id}
      </a>
      <span className="ar-seed">{row.input}</span>
      {audit ? (
        <span className="ar-grade" title={Object.keys(row.scores).join(", ")}>
          <b>{fmtScore(row.scores)}</b>
        </span>
      ) : (
        <span className="er-score">
          {Object.entries(row.scores)
            .map(([k, v]) => `${k}=${String(v)}`)
            .join(" ")}
        </span>
      )}
      {status}
      {opened && (
        <span className="ar-opened" title="in desk">
          <i className="bi bi-box-arrow-up-right" />
        </span>
      )}
    </div>
  );
}

// -- scan row -----------------------------------------------------------------

function ScanRow({
  row,
  total,
}: {
  row: { name: string } & ScannerStat;
  total: number;
}): JSX.Element {
  const pct = total > 0 ? Math.min(100, (row.scans / total) * 100) : 0;
  return (
    <div className="scan-row">
      <span className="scan-name">{row.name}</span>
      <span className="scan-found">
        <b>{row.results}</b> found
      </span>
      <span className="stat">
        {row.scans}/{total || "?"}
      </span>
      <span className="fx-bar">
        <i style={{ width: `${pct}%` }} />
      </span>
      {row.errors > 0 && (
        <span className="stat err" style={{ color: "var(--danger)" }}>
          +{row.errors}
        </span>
      )}
    </div>
  );
}
