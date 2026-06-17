import type { JSX } from "react";

import { modelLabel } from "../lib/presets";
import { useSession } from "../store/session";

/**
 * Persistent left rail (claude.ai-style). Wordmark, "+ New audit", a Recents
 * list of prior audits, and — when an audit is open — a read-only config card
 * for the current branch (editing is M1).
 */
export function Sidebar(): JSX.Element {
  const newAudit = useSession((s) => s.newAudit);
  const sessionsList = useSession((s) => s.sessionsList);
  const current = useSession((s) => s.current);
  const branchConfig = useSession((s) => (s.current ? s.branchConfig[s.current] : undefined));

  return (
    <aside className="sidebar">
      <div className="wordmark">
        workbench<span className="wordmark-dot">.</span>
      </div>

      <button className="side-new" onClick={newAudit}>
        <span className="plus">+</span> New audit
      </button>

      <div className="side-section">Recents</div>
      <div className="side-recents">
        {sessionsList.length === 0 ? (
          <div className="side-empty">No audits yet</div>
        ) : (
          sessionsList.map((s) => (
            <button
              key={s.id}
              className={`side-row${s.id === current ? " active" : ""}`}
              title={s.title}
              onClick={() => {
                // Re-view a prior branch. Same socket; the desk re-renders once
                // the store points `current` at it again. (M0: the backend keeps
                // one live branch, so this mainly returns from the empty state.)
                useSession.setState({ current: s.id === "__pending__" ? current : s.id });
              }}
            >
              {s.title}
            </button>
          ))
        )}
      </div>

      {current && branchConfig && (
        <>
          <div className="side-sep" />
          <div className="side-section">This audit · config</div>
          <div className="cfg">
            <div className="c-row">
              <span className="c-lab">auditor</span>
              <span className="c-val mono">{modelLabel(branchConfig.auditor_model)}</span>
            </div>
            <div className="c-row">
              <span className="c-lab">target</span>
              <span className="c-val mono">{modelLabel(branchConfig.target_model)}</span>
            </div>
            <div className="c-row">
              <span className="c-lab">turns</span>
              <span className="c-val">max {branchConfig.max_turns}</span>
            </div>
          </div>
        </>
      )}
    </aside>
  );
}
