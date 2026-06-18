import type { JSX } from "react";

import { MODELS, modelLabel } from "../lib/presets";
import { useSession } from "../store/session";

/** Format a timestamp as a relative string ("2m ago", "3h ago", etc.). */
function relTime(ts: number): string {
  const diff = Date.now() - ts;
  const s = Math.floor(diff / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/**
 * Persistent left rail. Wordmark, "+ New audit", Recents list with status
 * dots + relative timestamps, and — when an audit is open — an editable
 * config card for the current branch (binds to store.nextConfig).
 */
export function Sidebar(): JSX.Element {
  const newAudit = useSession((s) => s.newAudit);
  const sessionsList = useSession((s) => s.sessionsList);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const branchConfig = useSession((s) => (s.current ? s.branchConfig[s.current] : undefined));
  const nextConfig = useSession((s) => s.nextConfig);
  const setNextConfig = useSession((s) => s.setNextConfig);

  return (
    <aside className="sidebar">
      <div className="wordmark">
        workbench<span className="wordmark-dot">.</span>
      </div>

      <button className="side-new" onClick={newAudit}>
        + New audit
      </button>

      <div className="side-section">Recents</div>
      <div className="side-recents">
        {sessionsList.length === 0 ? (
          <div className="side-empty">No audits yet</div>
        ) : (
          sessionsList.map((s) => {
            const isActive = s.id === current;
            // Use live status for the active entry; treat others as ended/unknown
            const entryStatus = isActive ? (status ?? "ended") : "ended";
            return (
              <button
                key={s.id}
                className={`side-row${isActive ? " active" : ""}`}
                title={s.title}
                onClick={() => {
                  useSession.setState({ current: s.id === "__pending__" ? current : s.id });
                }}
              >
                <span className={`status-dot dot-${entryStatus}`} title={entryStatus} />
                <span className="side-row-title">{s.title}</span>
                <span className="side-row-time">{relTime(s.updatedAt)}</span>
              </button>
            );
          })
        )}
      </div>

      {current && branchConfig && (
        <>
          <div className="side-sep" />
          <div className="side-section">This audit · config</div>
          <div className="cfg">
            <div className="c-row">
              <span className="c-lab">auditor</span>
              <label className="c-val">
                <select
                  className="cfg-select"
                  value={nextConfig.auditor_model}
                  onChange={(e) => setNextConfig({ auditor_model: e.target.value })}
                >
                  {MODELS.map((m) => (
                    <option key={m} value={m}>{modelLabel(m)}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="c-row">
              <span className="c-lab">target</span>
              <label className="c-val">
                <select
                  className="cfg-select"
                  value={nextConfig.target_model}
                  onChange={(e) => setNextConfig({ target_model: e.target.value })}
                >
                  {MODELS.map((m) => (
                    <option key={m} value={m}>{modelLabel(m)}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="c-row">
              <span className="c-lab">turns</span>
              <span className="c-val">
                <input
                  type="number"
                  className="cfg-turns"
                  min={1}
                  max={30}
                  value={nextConfig.max_turns}
                  onChange={(e) => setNextConfig({ max_turns: Math.max(1, Number(e.target.value)) })}
                />
              </span>
            </div>
          </div>
        </>
      )}
    </aside>
  );
}
