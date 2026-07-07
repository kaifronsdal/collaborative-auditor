/**
 * `<JobsChip>` — PRODUCT-GAPS P2 background-job panel.
 *
 * A header chip (`N running` / `N jobs`) that opens a hover popover listing
 * every `bash(background=True)` subprocess + every eval run the orchestrator
 * has spawned this session. Rows come from `OrchestratorState.bg_jobs` (only
 * refreshed on `{t:"state"}`), so status is overlaid live from the event
 * stream: `bg_done` / `eval_run` payloads in `turns[].outputs` update the
 * dot without waiting for the next full-state push.
 *
 * Bash rows offer `{t:"cancel_bg", id}` (SIGTERM the process group) via the
 * × button; eval rows jump to their `ProgressCard` (whose
 * `data-display-id` == `eval_id`) where per-sample `stop_sample` lives.
 */
import { useMemo, useState, type JSX } from "react";

import type { BgJob } from "../../lib/wire";
import { WB_MIME, type OrchTurnData } from "./types";

/** Wall-clock now, refreshed once per open (elapsed doesn't need to tick). */
function elapsed(startedAt: number | null | undefined, now: number): string {
  if (startedAt == null) return "";
  const s = Math.max(0, Math.round(now - startedAt));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60 ? ` ${s % 60}s` : ""}`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Overlay `bg_jobs` status from the live event stream so the panel doesn't
 *  lag behind the column between `{t:"state"}` pushes. */
function withLiveStatus(jobs: readonly BgJob[], turns: readonly OrchTurnData[]): BgJob[] {
  if (jobs.length === 0) return [];
  // bash: `bg_done` cards carry `{id, exit}` — id == the panel row's id.
  const bashDone = new Set<string>();
  // eval: `eval_run` cards carry the latest `{finished, error}` per eval_id
  // (== the panel row's id and the card's `data-display-id`).
  const evalState = new Map<string, { finished: boolean; error: string | null }>();
  for (const t of turns) {
    for (const o of t.outputs) {
      const wb = o.data.bundle[WB_MIME];
      if (wb?.kind === "bg_done") bashDone.add(wb.id);
      else if (wb?.kind === "eval_run") {
        evalState.set(wb.id, { finished: wb.finished, error: wb.error });
      }
    }
  }
  return jobs.map((j) => {
    if (j.kind === "bash" && bashDone.has(j.id)) return { ...j, status: "done" };
    if (j.kind === "eval") {
      const live = evalState.get(j.id);
      if (live) {
        const status = live.error ? "error" : live.finished ? "done" : "running";
        return { ...j, status };
      }
    }
    return j;
  });
}

export function JobsChip({
  jobs,
  turns,
  onJump,
  onCancel,
}: {
  jobs: readonly BgJob[];
  turns: readonly OrchTurnData[];
  onJump: (displayId: string) => void;
  onCancel: (id: string) => void;
}): JSX.Element | null {
  const [hover, setHover] = useState(false);
  // Snapshot `now` per hover-open so every row's elapsed is consistent.
  const [now, setNow] = useState(() => Date.now() / 1000);

  const live = useMemo(() => withLiveStatus(jobs, turns), [jobs, turns]);
  const running = live.filter((j) => j.status === "running").length;

  if (live.length === 0) return null;

  const label = running > 0 ? `${running} running` : `${live.length} job${live.length === 1 ? "" : "s"}`;

  return (
    <span
      className="head-jobs-wrap"
      onMouseEnter={() => {
        setNow(Date.now() / 1000);
        setHover(true);
      }}
      onMouseLeave={() => setHover(false)}
    >
      <span className={`head-jobs-chip${running > 0 ? " live" : ""}`}>
        <i
          className={`bi bi-record-fill row-dot row-dot-${running > 0 ? "running" : "done"}`}
        />
        {label}
      </span>
      {hover && (
        <div className="head-jobs-pop">
          {live.map((j) => (
            <JobRow
              key={`${j.kind}-${j.id}`}
              job={j}
              now={now}
              onJump={onJump}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
    </span>
  );
}

function JobRow({
  job,
  now,
  onJump,
  onCancel,
}: {
  job: BgJob;
  now: number;
  onJump: (displayId: string) => void;
  onCancel: (id: string) => void;
}): JSX.Element {
  // Eval rows jump to the run card; bash rows have no stable card until
  // `bg_done` (fresh uuid), so they're inert.
  const jumpable = job.kind === "eval";
  const cancellable = job.kind === "bash" && job.status === "running";
  const title =
    job.kind === "bash"
      ? `pid ${job.pid ?? "?"} · ${job.cmd_or_task}`
      : job.log_dir || job.cmd_or_task;
  return (
    <div
      className={`hjp-row hstack g8${jumpable ? " jumpable" : ""}`}
      title={title}
      onClick={jumpable ? () => onJump(job.id) : undefined}
    >
      <i
        className={`bi ${job.kind === "bash" ? "bi-terminal" : "bi-play-circle"} hjp-kind`}
      />
      <span className="hjp-cmd truncate">{job.cmd_or_task}</span>
      <i className={`bi bi-record-fill row-dot row-dot-${job.status}`} />
      <span className="hjp-elapsed">
        {job.status === "running" ? elapsed(job.started_at, now) : job.status}
      </span>
      <span className="hjp-cancel-slot">
        {cancellable && (
          <button
            className="hjp-cancel"
            title="SIGTERM this subprocess"
            onClick={(e) => {
              e.stopPropagation();
              onCancel(job.id);
            }}
          >
            <i className="bi bi-x-lg" />
          </button>
        )}
      </span>
    </div>
  );
}
