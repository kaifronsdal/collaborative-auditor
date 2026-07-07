/**
 * `ProgressCard` — the shared live-job shell (UI-AUDIT.md §D). One `.out`
 * per handle: `.fx-dot` + task/id header, `.gate-desc` subtitle, an `.fx-out`
 * counter line (bar · `done/total` · `+err` · elapsed), and a variant row
 * list with `{N} more`/`collapse`.
 *
 * Variants (dispatched on `payload.kind`):
 * - `eval_run` — one row per sample. Row click imports it into
 *   the M0 desk (`{t:"import"}` for finished, `{t:"import_running"}` for
 *   live). Opened rows keep a persistent `↗` glyph. When `total > 8` a
 *   `.pc-filter` chip row + `.pc-cols` sortable header appear (M1-FEATURES
 *   §3); when `finished` a `.pc-hist` score sparkline (§8) filters rows by
 *   bin on click. Row-hover `bi-stop-fill` sends `{t:"stop_sample"}` (§9).
 * - `scan` — one row *per scanner* (not one card per scanner). Location in
 *   the footer; on finish each scanner's `df_head` HTML renders inline.
 */
import { Fragment, useRef, useState, type JSX } from "react";

import { basename } from "@tsmono/util";

import type { Up } from "../../../lib/wire";
import { useSession } from "../../../store/session";
import type {
  EvalRunPayload,
  SampleRowPayload,
  ScanPayload,
} from "../types";

type SampleRow = SampleRowPayload;
type ScannerStat = ScanPayload["per_scanner"][string];
type ProgressPayload = EvalRunPayload | ScanPayload;

type Props = {
  payload: ProgressPayload;
  displayId: string;
  send: (msg: Up) => void;
};

const ROW_CAP = 3;

type SortCol = "id" | "score" | "status" | "turns";
type StatusFilter = "all" | SampleRow["status"];

/** `"ValueError: bad seed"` → `"ValueError"`. */
const errClass = (e: string | null | undefined): string =>
  e?.split(/[:(\n]/, 1)[0].trim() || "error";

/** First numeric value in a row's score dict, or `null`. */
const firstNumeric = (scores: Record<string, unknown>): number | null => {
  for (const v of Object.values(scores)) if (typeof v === "number") return v;
  return null;
};

/** `12_345` → `"12k"`; `1_234_567` → `"1.2M"`. */
const fmtTokens = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : `${Math.round(n / 1000)}k`;

// -- shared shell -------------------------------------------------------------

export default function ProgressCard({ payload, displayId, send }: Props): JSX.Element {
  const [showAll, setShowAll] = useState(false);
  // Rows the user has opened in the desk — persistent `↗` glyph, survives the
  // 2s highlight and re-renders (UI-AUDIT §C).
  const opened = useRef<Set<string>>(new Set());
  // §9 optimistic stop: id stays here until the next `dh.update` moves it out
  // of `rows.running` (status flips → the set entry goes inert, never cleared).
  const stopping = useRef<Set<string>>(new Set());
  const [, bump] = useState(0);
  const rerender = (): void => bump((n) => n + 1);

  // §3 sort/filter — local, run-variant only.
  const [sortCol, setSortCol] = useState<SortCol>("id");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [textFilter, setTextFilter] = useState("");
  // §8 histogram bin click — `[lo, hi]` inclusive.
  const [scoreRange, setScoreRange] = useState<[number, number] | null>(null);

  // P2 pin/bookmark — store-direct so parent chain (OrchColumn/Output) needn't
  // thread it. Only `eval_run` rows with a written `.eval` (`log != null`) are
  // pinnable; running samples get their pin once the log lands.
  const pins = useSession((s) => s.pins);
  const togglePin = useSession((s) => s.togglePin);

  const markOpened = (id: string): void => {
    opened.current.add(id);
    rerender();
  };

  const clickSort = (col: SortCol): void => {
    if (col === sortCol) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortCol(col);
      setSortDir("asc");
    }
  };

  const pct = payload.total > 0 ? Math.min(100, (payload.done / payload.total) * 100) : 0;

  // Variant supplies the row list + errored count + optional footer/controls;
  // the shell owns show-all/collapse and the counter line.
  let title: string;
  let allRows: JSX.Element[];
  let errored: number;
  let controls: JSX.Element | null = null;
  let hist: JSX.Element | null = null;
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
          <div className="fx-more hstack g8">
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
    const log = payload.log;

    // -- §3 filter → sort ----
    const q = textFilter.trim().toLowerCase();
    const filtered = rows.filter((r) => {
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (q && !r.id.toLowerCase().includes(q) && !r.input.toLowerCase().includes(q)) return false;
      if (scoreRange) {
        const s = firstNumeric(r.scores);
        if (s == null || s < scoreRange[0] || s > scoreRange[1]) return false;
      }
      return true;
    });
    const sorted = [...filtered].sort((a, b) => {
      const d =
        sortCol === "id"
          ? a.id.localeCompare(b.id, undefined, { numeric: true })
          : sortCol === "status"
            ? a.status.localeCompare(b.status)
            : sortCol === "turns"
              ? (a.turns ?? -1) - (b.turns ?? -1)
              : (firstNumeric(a.scores) ?? -Infinity) - (firstNumeric(b.scores) ?? -Infinity);
      return sortDir === "asc" ? d : -d;
    });

    const onRowClick = (row: SampleRow): void => {
      if (row.status === "running") {
        send({ t: "import_running", sample_id: row.id, log_dir: payload.log_dir });
      } else if (log != null) {
        send({ t: "import", path: log, sample_id: row.id });
      } else {
        return;
      }
      markOpened(row.id);
    };
    const onStop = (row: SampleRow): void => {
      send({ t: "stop_sample", id: row.id, log_dir: payload.log_dir });
      stopping.current.add(row.id);
      rerender();
    };
    const pinnedIds =
      log != null
        ? new Set(pins.filter((p) => p.log === log).map((p) => p.sample_id))
        : null;
    allRows = sorted.map((r) => (
      <RunRow
        key={r.id}
        row={r}
        opened={opened.current.has(r.id)}
        stopping={r.status === "running" && stopping.current.has(r.id)}
        pinned={pinnedIds?.has(r.id) ?? false}
        onClick={() => onRowClick(r)}
        onStop={r.status === "running" ? () => onStop(r) : undefined}
        onPin={log != null ? () => togglePin(log, r.id) : undefined}
      />
    ));

    // -- §3 controls (filter chips + search + sortable header) ----
    if (payload.total > 8) {
      // `rows` is only what the poller has streamed so far — `total` is the
      // authoritative count for the `all(N)` chip.
      const counts = {
        all: Math.max(payload.total || 0, rows.length),
        running: 0,
        done: 0,
        error: 0,
      };
      for (const r of rows) if (r.status in counts) counts[r.status as keyof typeof counts]++;
      const chips: StatusFilter[] = ["all", "running", "done", "error"];
      // Header mirrors the row skeleton: id | flex-1 spacer | score / status /
      // turns (each sized to its data column via `.pc-col-{c}`) | `.ar-slot`.
      const cols: SortCol[] = ["id", "score", "status", "turns"];
      controls = (
        <>
          <div className="pc-filter hstack g4">
            {chips.map((c, i) => (
              <a
                key={c}
                className={`pc-chip${statusFilter === c ? " active" : ""}`}
                onClick={() => setStatusFilter(c)}
              >
                {i > 0 && <span className="sep">·</span>}
                {c} <span className="pc-n">({counts[c as keyof typeof counts]})</span>
              </a>
            ))}
            {scoreRange && (
              <a className="pc-chip pc-range" onClick={() => setScoreRange(null)}>
                <i className="bi bi-x" /> score {scoreRange[0].toFixed(2)}–{scoreRange[1].toFixed(2)}
              </a>
            )}
            <input
              className="pc-search"
              placeholder="filter id/seed…"
              value={textFilter}
              onChange={(e) => setTextFilter(e.target.value)}
            />
          </div>
          <div className="pc-cols hstack g10">
            {cols.map((c, i) => (
              <Fragment key={c}>
                {i === 1 && <span style={{ flex: 1 }} />}
                <a className={`pc-col pc-col-${c}`} onClick={() => clickSort(c)}>
                  {c}
                  {sortCol === c && (
                    <i className={`bi bi-caret-${sortDir === "asc" ? "up" : "down"}-fill`} />
                  )}
                </a>
              </Fragment>
            ))}
            <span className="ar-slot" />
          </div>
        </>
      );
    }

    // -- §8 histogram ----
    if (payload.finished && payload.scores) {
      hist = (
        <Histogram
          scores={payload.scores}
          range={scoreRange}
          onPick={setScoreRange}
          smallCard={payload.total <= 8}
        />
      );
    }
  }

  const shown = showAll ? allRows : allRows.slice(0, ROW_CAP);
  const hidden = allRows.length - shown.length;

  return (
    <div className="out" data-display-id={displayId}>
      <div className="out-head hstack g8">
        <i
          className={`bi bi-record-fill fx-dot${payload.error ? " err" : payload.finished ? "" : " pending"}`}
        />
        <span className="out-task">{title}</span>
        <span className="out-meta out-id">{payload.id.slice(0, 8)}</span>
      </div>
      {payload.description && <div className="gate-desc">{payload.description}</div>}

      <div className="fx-out hstack g6">
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
        {errored > 0 && <span className="stat err">+{errored}</span>}
        {payload.kind === "eval_run" && payload.elapsed && (
          <>
            <span className="sep">·</span>
            <span className="stat">{payload.elapsed}</span>
          </>
        )}
        {payload.kind === "eval_run" && payload.usage && (
          <>
            <span className="sep">·</span>
            <span
              className="stat"
              title={`in ${payload.usage.input_tokens.toLocaleString()} · out ${payload.usage.output_tokens.toLocaleString()}`}
            >
              {fmtTokens(payload.usage.total_tokens)} tok
            </span>
          </>
        )}
        {payload.error && <span className="stat err">{errClass(payload.error)}</span>}
      </div>

      {hist}
      {controls}

      {allRows.length > 0 && (
        <div className={payload.kind === "scan" ? "scan-rows" : "audit-rows"}>{shown}</div>
      )}

      {(hidden > 0 || (showAll && allRows.length > ROW_CAP)) && (
        <div className="fx-more hstack g8">
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

// -- run row (eval_run) -------------------------------------------------------

function RunRow({
  row,
  opened,
  stopping,
  pinned,
  onClick,
  onStop,
  onPin,
}: {
  row: SampleRow;
  opened: boolean;
  stopping: boolean;
  pinned: boolean;
  onClick: () => void;
  onStop?: () => void;
  onPin?: () => void;
}): JSX.Element {
  // Status slot per UI-AUDIT §C: `done` → nothing (dot suffices); `running` →
  // `t{turns}`; `error` → exception class; `stopped` → literal. §9 optimistic
  // `stopping…` overrides while the row is still `running` locally.
  const status = stopping ? (
    <span className="ar-status stopping">stopping…</span>
  ) : row.status === "done" ? (
    // Empty spacers so the flex skeleton keeps the header's status/turns
    // column widths — without them the score column drifts right on done rows.
    <>
      <span className="ar-status" />
      <span className="ar-turns" />
    </>
  ) : row.status === "running" ? (
    <span className="ar-turns">t{row.turns ?? "…"}</span>
  ) : row.status === "error" ? (
    <span className="ar-status error">{errClass(row.error)}</span>
  ) : (
    <span className="ar-status stopped">stopped</span>
  );

  return (
    <div
      className="eval-row hstack g10"
      role="button"
      tabIndex={0}
      title={row.status === "running" ? "watch live in auditor" : "open transcript in auditor"}
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
        className="qref er-id"
        href={`wb://audit/${row.id}`}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onClick();
        }}
      >
        {row.id}
      </a>
      <span className="ar-seed truncate">{row.input}</span>
      <span className="er-score truncate">
        {Object.entries(row.scores)
          .map(([k, v]) => `${k}=${String(v)}`)
          .join(" ")}
      </span>
      {status}
      <span className="ar-slot">
        {onPin && (
          <button
            className={`ar-pin${pinned ? " pinned" : ""}`}
            title={pinned ? "unpin" : "pin transcript"}
            aria-label={pinned ? "unpin" : "pin transcript"}
            onClick={(e) => {
              e.stopPropagation();
              onPin();
            }}
          >
            <i className={`bi ${pinned ? "bi-star-fill" : "bi-star"}`} />
          </button>
        )}
        {onStop && !stopping && (
          <button
            className="ar-stop"
            title="stop this sample"
            onClick={(e) => {
              e.stopPropagation();
              onStop();
            }}
          >
            <i className="bi bi-stop-fill" />
          </button>
        )}
        {opened && (
          <span className="ar-opened" title="opened in auditor">
            <i className="bi bi-box-arrow-up-right" />
          </span>
        )}
      </span>
    </div>
  );
}

// -- §8 score histogram -------------------------------------------------------

function Histogram({
  scores,
  range,
  onPick,
  smallCard,
}: {
  scores: (number | null)[];
  range: [number, number] | null;
  onPick: (r: [number, number] | null) => void;
  /** `total ≤ 8` — no `.pc-filter` row, so the `× clear` chip lives here. */
  smallCard: boolean;
}): JSX.Element | null {
  const nums = scores.filter((s): s is number => s != null);
  if (nums.length < 2) return null;
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  if (hi === lo) return null;
  const span = hi - lo;
  const bins = new Array<number>(10).fill(0);
  for (const s of nums) bins[Math.min(9, Math.floor(((s - lo) / span) * 10))]++;
  const peak = Math.max(...bins);
  const edge = (i: number): number => lo + (span * i) / 10;

  return (
    <div className="pc-hist-wrap">
      <svg className="pc-hist" height={32} viewBox="0 0 100 32" preserveAspectRatio="none">
        {bins.map((n, i) => {
          const h = n > 0 ? Math.max(2, (n / peak) * 30) : 0;
          const bLo = edge(i);
          const bHi = i === 9 ? hi : edge(i + 1);
          const active = range != null && range[0] === bLo && range[1] === bHi;
          return (
            <rect
              key={i}
              x={i * 10 + 0.5}
              y={32 - h}
              width={9}
              height={h}
              className={active ? "active" : undefined}
              onClick={() => onPick(active ? null : [bLo, bHi])}
            >
              <title>
                {bLo.toFixed(2)}–{bHi.toFixed(2)}: {n}
              </title>
            </rect>
          );
        })}
      </svg>
      {smallCard && range && (
        <a className="pc-chip pc-range" onClick={() => onPick(null)}>
          <i className="bi bi-x" /> {range[0].toFixed(2)}–{range[1].toFixed(2)}
        </a>
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
    <div className="scan-row hstack g10">
      <span className="scan-name truncate">{row.name}</span>
      <span className="scan-found">
        <b>{row.results}</b> found
      </span>
      <span className="stat">
        {row.scans}/{total || "?"}
      </span>
      <span className="fx-bar">
        <i style={{ width: `${pct}%` }} />
      </span>
      {row.errors > 0 && <span className="stat err">+{row.errors}</span>}
    </div>
  );
}
