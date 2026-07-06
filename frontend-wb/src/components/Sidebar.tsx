import type { JSX } from "react";

import type { BranchId, BranchMeta } from "../lib/wire";
import { type Mode, useSession } from "../store/session";
import { useEffect, useState } from "react";
import { Chevron } from "./icons";

const COLLAPSED_KEY = "workbench.sidebarCollapsed";

/** Sidebar MODES section entries — icon + label, claude.ai "Products" style. */
const MODES: { id: Mode; icon: string; label: string }[] = [
  { id: "desk", icon: "bi-chat-left-text", label: "Collaborative Auditor" },
  { id: "orch", icon: "bi-terminal", label: "Orchestrator" },
];

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
  onExport,
  visited,
}: {
  id: BranchId;
  meta: BranchMeta;
  current: string | null;
  allBranches: Record<BranchId, BranchMeta>;
  depth: number;
  onSwitch: (id: BranchId) => void;
  onExport: (id: BranchId) => void;
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
    <div>
      <div
        className={`side-row${isActive ? " active" : ""}`}
        style={{ paddingLeft: 10 + depth * 14 }}
        title={label}
        onClick={() => onSwitch(id)}
      >
        <span className={`status-dot dot-${meta.status}`} title={meta.status} />
        <span className="side-row-title">{label}</span>
        <button
          className="side-icon-btn"
          title="Export branch as .eval"
          aria-label="Export branch"
          onClick={(e) => { e.stopPropagation(); onExport(id); }}
        >
          <i className="bi bi-download" style={{ fontSize: 11 }} />
        </button>
      </div>
      {children.map(([cid, cmeta]) => (
        <BranchNode
          key={cid}
          id={cid}
          meta={cmeta}
          current={current}
          allBranches={allBranches}
          depth={depth + 1}
          onSwitch={onSwitch}
          onExport={onExport}
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
  const savedSessions = useSession((s) => s.savedSessions);
  const sessionId = useSession((s) => s.sessionId);
  const fetchSessions = useSession((s) => s.fetchSessions);
  const exportBranch = useSession((s) => s.exportBranch);
  const importEval = useSession((s) => s.importEval);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const branches = useSession((s) => s.branches);
  const switchBranch = useSession((s) => s.switchBranch);
  const mode = useSession((s) => s.mode);
  const setMode = useSession((s) => s.setMode);

  useEffect(() => {
    void fetchSessions();
  }, [fetchSessions]);

  function handleExport(id: BranchId): void {
    const path = window.prompt("Export branch to .eval path:", `${id}.eval`);
    if (path) exportBranch(id, path);
  }

  function handleImport(): void {
    const path = window.prompt("Import .eval from path:");
    if (path) importEval(path);
  }

  function openSession(sid: string): void {
    const url = new URL(location.href);
    url.searchParams.set("session", sid);
    location.assign(url.toString());
  }

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

  if (collapsed) {
    return (
      <aside className="sidebar sidebar--collapsed">
        <button
          className="side-icon-btn"
          onClick={toggleCollapsed}
          title="Expand sidebar"
          aria-label="Expand sidebar"
        >
          <Chevron size={12} />
        </button>
        <button
          className="side-icon-btn"
          onClick={() => { toggleCollapsed(); newAudit(); }}
          title="New audit"
          aria-label="New audit"
        >
          <span className="plus">+</span>
        </button>
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
          className="side-icon-btn"
          onClick={toggleCollapsed}
          title="Collapse sidebar"
          aria-label="Collapse sidebar"
        >
          <i className="bi bi-chevron-left" style={{ fontSize: 12 }} />
        </button>
      </div>

      <button className="side-new" onClick={newAudit}>
        <span className="plus">+</span> New audit
      </button>

      <div className="side-section">Modes</div>
      <div className="side-modes">
        {MODES.map((m) => (
          <button
            key={m.id}
            className={`side-mode${mode === m.id ? " active" : ""}`}
            // Sets the "next new" preference. When StartView is up the card
            // swaps immediately; when a session is live the running layout is
            // fixed, so this only affects what `+ New audit` opens next.
            onClick={() => setMode(m.id)}
          >
            <i className={`bi ${m.icon}`} />
            <span>{m.label}</span>
          </button>
        ))}
      </div>

      <div className="side-section">Recents</div>
      <div className="side-recents">
        {savedSessions.length === 0 && sessionsList.length === 0 ? (
          <div className="side-empty">No audits yet</div>
        ) : null}
        {savedSessions.map((s) => {
          const isActive = s.session_id === sessionId;
          const title = s.seed.trim() || s.session_id;
          return (
            <button
              key={s.session_id}
              className={`side-row${isActive ? " active" : ""}`}
              title={title}
              onClick={() => openSession(s.session_id)}
            >
              <span className="side-row-title">{title}</span>
              <span className="side-row-time">
                {s.n_branches} · {relTime(Date.parse(s.created_at))}
              </span>
            </button>
          );
        })}
        {sessionsList.length > 0 &&
          sessionsList.map((s) => {
            const isActive = s.id === current;
            return (
              <button
                key={s.id}
                className={`side-row${isActive ? " active" : ""}`}
                title={s.title}
                onClick={() => {
                  // Pending entry has no backend branch id yet — clicking it
                  // while we're waiting for the first `state` broadcast is a
                  // no-op (the audit is about to become current on its own).
                  if (s.id !== "__pending__") switchBranch(s.id);
                }}
              >
                {isActive && <span className={`status-dot dot-${status ?? "ended"}`} />}
                <span className="side-row-title">{s.title}</span>
                <span className="side-row-time">{relTime(s.updatedAt)}</span>
              </button>
            );
          })}
      </div>

      {current && (
        <>
          {roots.length > 0 && (
            <>
              <div className="side-section">
                Branches
                <button
                  className="side-icon-btn"
                  style={{ float: "right" }}
                  title="Import .eval as new branch"
                  aria-label="Import .eval"
                  onClick={handleImport}
                >
                  <i className="bi bi-upload" style={{ fontSize: 11 }} />
                </button>
              </div>
              <div className="side-branches">
                {roots.map(([id, meta]) => (
                  <BranchNode
                    key={id}
                    id={id}
                    meta={meta}
                    current={current}
                    allBranches={branches}
                    depth={0}
                    onSwitch={switchBranch}
                    onExport={handleExport}
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
