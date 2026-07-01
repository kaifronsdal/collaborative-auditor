/**
 * `wb.run_audits` pre-launch gate. Payload from `RunProposal._repr_mimebundle_`
 * (workbench/m1/run.py): `{kind, id, description, n, n_per_seed, seeds,
 * config, pending, verdict}`.
 *
 * While pending: `.out.gated` with the description as `.gate-desc`, a
 * strikeable `.seed-preview` (click a row to toggle), and the
 * approve/edit/deny `.gate-bar` (scenario-b.html turn 3). Approve sends the
 * surviving seed list; deny sends `{denied: true, reason}`. Once resolved
 * the backend flips this same `display_id` to a live `RunHandle` card, so the
 * post-approval state here is only seen briefly (or on a denied proposal).
 */
import { useState, type JSX } from "react";
import type { Up } from "../../../lib/wire";

export type RunProposalPayload = {
  kind: "run_proposal";
  id: string;
  description: string;
  n: number;
  n_per_seed: number;
  seeds: string[];
  config: Record<string, unknown>;
  pending: boolean;
  verdict: { denied?: boolean; seeds?: string[]; reason?: string } | null;
};

type Props = {
  payload: RunProposalPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function RunProposalCard({
  payload,
  displayId,
  send,
}: Props): JSX.Element {
  // Struck-by-index (seeds are truncated strings, not guaranteed unique).
  const [struck, setStruck] = useState<Set<number>>(new Set());
  const [expanded, setExpanded] = useState(false);
  const [denyReason, setDenyReason] = useState<string | null>(null);

  const surviving = payload.seeds.filter((_, i) => !struck.has(i));
  const nLive = surviving.length * payload.n_per_seed;

  const toggle = (i: number): void =>
    setStruck((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });

  const approve = (): void =>
    send({ t: "approve", display_id: displayId, verdict: { seeds: surviving } });

  const deny = (reason: string): void =>
    send({
      t: "approve",
      display_id: displayId,
      verdict: { denied: true, reason },
    });

  const resolved = !payload.pending;
  const denied = resolved && payload.verdict?.denied;

  return (
    <div className={resolved ? "out" : "out gated"}>
      <div className="out-head">
        <i
          className={resolved ? "bi bi-record-fill fx-dot" : "bi bi-hourglass-split"}
          style={resolved ? undefined : { color: "var(--gate-text)" }}
        />
        <span className="out-kind">
          run · {resolved ? (denied ? "denied" : "approved") : "proposed"}
        </span>
        <span className="out-meta">{nLive} audits</span>
      </div>
      <div className="gate-desc">{payload.description}</div>

      <div className={`seed-preview${expanded ? "" : " collapsed"}`}>
        {payload.seeds.map((seed, i) => {
          const isStruck = struck.has(i);
          return (
            <div
              key={i}
              className="sp-row"
              style={
                isStruck ? { textDecoration: "line-through", opacity: 0.6 } : undefined
              }
              onClick={payload.pending ? () => toggle(i) : undefined}
              title={payload.pending ? (isStruck ? "restore" : "strike") : seed}
            >
              <span className="sp-id">{String(i).padStart(2, "0")}</span>
              {seed}
            </div>
          );
        })}
        <div className="sp-toggle" onClick={() => setExpanded((v) => !v)}>
          <i className={`bi bi-chevron-${expanded ? "down" : "right"}`} />{" "}
          <b>
            {surviving.length} seed{surviving.length === 1 ? "" : "s"}
            {payload.n_per_seed > 1 ? ` × ${payload.n_per_seed}` : ""}
          </b>
          {struck.size > 0 && (
            <span style={{ color: "var(--ink-faint)" }}> · {struck.size} struck</span>
          )}
          <button
            type="button"
            className="sp-edit"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(true);
            }}
          >
            edit seeds
          </button>
        </div>
      </div>

      {payload.pending ? (
        denyReason == null ? (
          <div className="gate-bar">
            <span className="gate-reason">
              {payload.n} audits — approve to launch
            </span>
            <button type="button" className="gate-btn primary" onClick={approve}>
              <i className="bi bi-check2" /> approve
            </button>
            <button
              type="button"
              className="gate-btn"
              onClick={() => setExpanded(true)}
            >
              edit
            </button>
            <button
              type="button"
              className="gate-btn deny"
              onClick={() => setDenyReason("")}
            >
              deny
            </button>
          </div>
        ) : (
          <div className="gate-bar">
            <input
              autoFocus
              className="gate-reason"
              style={{ border: "none", background: "none", outline: "none" }}
              placeholder="reason (optional) — enter to deny, esc to cancel"
              value={denyReason}
              onChange={(e) => setDenyReason(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") deny(denyReason);
                if (e.key === "Escape") setDenyReason(null);
              }}
            />
            <button type="button" className="gate-btn deny" onClick={() => deny(denyReason)}>
              deny
            </button>
            <button type="button" className="gate-btn" onClick={() => setDenyReason(null)}>
              cancel
            </button>
          </div>
        )
      ) : (
        <div className="fx-approved">
          <i className={`bi bi-${denied ? "x" : "check"}-circle-fill`} />
          {denied
            ? `denied${payload.verdict?.reason ? ` — ${payload.verdict.reason}` : ""}`
            : `approved · ${payload.n} audits`}
        </div>
      )}
    </div>
  );
}
