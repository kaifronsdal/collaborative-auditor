import type { JSX } from "react";

import { modelLabel } from "../lib/presets";
import type { BranchId, BranchMeta } from "../lib/wire";
import { useSession } from "../store/session";
import { ModelPicker, readStoredConfig } from "./ModelPicker";
import type { GenerateConfigDict } from "./ModelPicker";
import { useState } from "react";

const COLLAPSED_KEY = "workbench.sidebarCollapsed";

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

/** Render one node of the branch tree, recursing into children. */
function BranchNode({
  id,
  meta,
  current,
  allBranches,
  depth,
  onSwitch,
  visited,
}: {
  id: BranchId;
  meta: BranchMeta;
  current: string | null;
  allBranches: Record<BranchId, BranchMeta>;
  depth: number;
  onSwitch: (id: BranchId) => void;
  visited: Set<string>;
}): JSX.Element {
  if (visited.has(id)) return <></>;
  const nextVisited = new Set(visited).add(id);
  const isActive = id === current;
  const label = meta.branched_at
    ? `branch @ ${meta.branched_at.slice(0, 8)}`
    : meta.seed.length > 0
      ? meta.seed
      : id.slice(0, 8);
  const children = Object.entries(allBranches).filter(([, m]) => m.parent === id);

  return (
    <div style={{ paddingLeft: depth > 0 ? `${depth * 12}px` : undefined }}>
      <button
        className={`side-row${isActive ? " active" : ""}`}
        title={label}
        onClick={() => onSwitch(id)}
      >
        <span className={`status-dot dot-${meta.status}`} title={meta.status} />
        <span className="side-row-title">{label}</span>
      </button>
      {children.map(([cid, cmeta]) => (
        <BranchNode
          key={cid}
          id={cid}
          meta={cmeta}
          current={current}
          allBranches={allBranches}
          depth={depth + 1}
          onSwitch={onSwitch}
          visited={nextVisited}
        />
      ))}
    </div>
  );
}

/**
 * Persistent left rail. Wordmark, "+ New audit", Recents list with status
 * dots + relative timestamps, and — when an audit is open — an editable
 * config card for the current branch (binds to store.nextConfig).
 * Below the config card, a branch tree for the current session.
 */
export function Sidebar(): JSX.Element {
  const newAudit = useSession((s) => s.newAudit);
  const sessionsList = useSession((s) => s.sessionsList);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const nextConfig = useSession((s) => s.nextConfig);
  const setNextConfig = useSession((s) => s.setNextConfig);
  const branches = useSession((s) => s.branches);
  const branchConfig = useSession((s) =>
    s.current ? s.branchConfig[s.current] : undefined
  );
  const send = useSession((s) => s.send);

  const [sideAuditorCfg, setSideAuditorCfg] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("auditor"),
  );
  const [sideTargetCfg, setSideTargetCfg] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("target"),
  );

  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  });

  function toggleCollapsed(): void {
    setCollapsed((v) => {
      const next = !v;
      try { localStorage.setItem(COLLAPSED_KEY, String(next)); } catch { /* ignore */ }
      return next;
    });
  }

  // Roots: branches with no parent.
  const roots = Object.entries(branches).filter(([, m]) => m.parent === null);

  function handleSwitch(id: BranchId) {
    send({ t: "switch", branch: id });
    // Clear pendingNewAudit so the backend state broadcast is allowed to set
    // current again. Also update local current immediately so the highlight
    // responds fast; the server's state broadcast will confirm it.
    useSession.setState({ current: id, pendingNewAudit: false });
  }

  if (collapsed) {
    return (
      <aside className="sidebar sidebar--collapsed">
        <button
          className="sidebar-expand-btn"
          onClick={toggleCollapsed}
          title="Expand sidebar"
          aria-label="Expand sidebar"
        >
          ›
        </button>
        <button
          className="side-new-icon"
          onClick={() => { toggleCollapsed(); newAudit(); }}
          title="New audit"
          aria-label="New audit"
        >
          +
        </button>
        {/* Status dots for recent audits */}
        {sessionsList.slice(0, 6).map((s) => {
          const isActive = s.id === current;
          const entryStatus = isActive ? (status ?? "ended") : "ended";
          return (
            <span
              key={s.id}
              className={`status-dot dot-${entryStatus} sidebar-dot`}
              title={s.title}
            />
          );
        })}
      </aside>
    );
  }

  return (
    <aside className="sidebar">
      <div className="wordmark-row">
        <div className="wordmark">
          workbench<span className="wordmark-dot">.</span>
        </div>
        <button
          className="sidebar-collapse-btn"
          onClick={toggleCollapsed}
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
        >
          ‹
        </button>
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
                  if (s.id === "__pending__") {
                    // Pending entry has no backend branch id yet — clicking it
                    // while we're waiting for the first `state` broadcast is a
                    // no-op (the audit is about to become current on its own).
                    return;
                  }
                  send({ t: "switch", branch: s.id });
                  // Clear pendingNewAudit so the state broadcast can update
                  // current, and update optimistically for instant highlight.
                  useSession.setState({ current: s.id, pendingNewAudit: false });
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

      {current && (
        <>
          <div className="side-sep" />
          <div className="side-section">{current ? "This audit · config" : "Next audit · config"}</div>
          <div className="cfg">
            {current && branchConfig ? (
              // Read-only view of server-authoritative branch config
              <>
                <div className="c-row"><span className="c-lab">auditor</span><span className="c-val">{modelLabel(branchConfig.auditor_model)}</span></div>
                <div className="c-row"><span className="c-lab">target</span><span className="c-val">{modelLabel(branchConfig.target_model)}</span></div>
              </>
            ) : (
              // Editable nextConfig for next audit — compact ModelPicker
              <>
                <div className="c-row">
                  <span className="c-lab">auditor</span>
                  <span className="c-val c-val--picker">
                    <ModelPicker
                      role="auditor"
                      value={nextConfig.auditor_model}
                      config={sideAuditorCfg}
                      compact
                      onChange={(m, cfg) => {
                        setSideAuditorCfg(cfg);
                        setNextConfig({ auditor_model: m, auditor_config: cfg });
                      }}
                    />
                  </span>
                </div>
                <div className="c-row">
                  <span className="c-lab">target</span>
                  <span className="c-val c-val--picker">
                    <ModelPicker
                      role="target"
                      value={nextConfig.target_model}
                      config={sideTargetCfg}
                      compact
                      onChange={(m, cfg) => {
                        setSideTargetCfg(cfg);
                        setNextConfig({ target_model: m, target_config: cfg });
                      }}
                    />
                  </span>
                </div>
              </>
            )}
          </div>

          {roots.length > 0 && (
            <>
              <div className="side-sep" />
              <div className="side-section">Branches</div>
              <div className="side-branches">
                {roots.map(([id, meta]) => (
                  <BranchNode
                    key={id}
                    id={id}
                    meta={meta}
                    current={current}
                    allBranches={branches}
                    depth={0}
                    onSwitch={handleSwitch}
                    visited={new Set()}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </aside>
  );
}
