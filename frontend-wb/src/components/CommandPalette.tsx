/**
 * P2 — ⌘K command palette.
 *
 * A `<Modal>` with a fuzzy-filter input over a flat command list. Arrow keys
 * move the selection; Enter runs it; Esc closes (via `Modal.onHide`). "Jump to
 * session…" swaps the list for `savedSessions` in place — Backspace on an
 * empty query returns to the top-level list.
 *
 * All actions go through existing `useSession` store actions / `send()`; the
 * palette holds no state of its own beyond `query`/`selected`/`submode`.
 */
import type { JSX } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Modal } from "@tsmono/react/components/Modal";

import { useSession } from "../store/session";

type Command = {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
  disabled?: boolean;
};

/** Subsequence match — every char of `q` appears in `t` in order. Empty `q`
 *  matches everything. Case-insensitive. */
function fuzzy(q: string, t: string): boolean {
  if (!q) return true;
  const ql = q.toLowerCase();
  const tl = t.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < tl.length && qi < ql.length; ti++) {
    if (tl[ti] === ql[qi]) qi++;
  }
  return qi === ql.length;
}

/** Trigger a browser download for `href` (same behaviour as the sidebar's
 *  `<a download>` — the endpoint sets Content-Disposition). */
function download(href: string): void {
  const a = document.createElement("a");
  a.href = href;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function CommandPalette({
  onClose,
  onOpenSettings,
}: {
  onClose: () => void;
  onOpenSettings: () => void;
}): JSX.Element {
  const setMode = useSession((s) => s.setMode);
  const newAudit = useSession((s) => s.newAudit);
  const send = useSession((s) => s.send);
  const sessionId = useSession((s) => s.sessionId);
  const orch = useSession((s) => s.orchestrator);
  const savedSessions = useSession((s) => s.savedSessions);
  const fetchSessions = useSession((s) => s.fetchSessions);

  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const [submode, setSubmode] = useState<"root" | "sessions">("root");
  const inputRef = useRef<HTMLInputElement>(null);

  // Refresh the sessions list on open so "Jump to session…" is current.
  useEffect(() => { void fetchSessions(); }, [fetchSessions]);

  const orchRunning = orch?.status === "running" || orch?.status === "waiting";

  const rootCommands = useMemo<Command[]>(() => [
    {
      id: "new-audit",
      label: "New audit",
      hint: "Collaborative Auditor",
      run: () => { setMode("desk"); newAudit(); },
    },
    {
      id: "new-orch",
      label: "New orchestrator session",
      hint: "Orchestrator",
      run: () => { setMode("orch"); newAudit(); },
    },
    {
      id: "jump",
      label: "Jump to session…",
      hint: `${savedSessions.length} saved`,
      run: () => { setSubmode("sessions"); setQuery(""); setSel(0); },
    },
    {
      id: "settings",
      label: "Settings",
      run: onOpenSettings,
    },
    {
      id: "export",
      label: "Export findings",
      hint: sessionId ? `findings-${sessionId}.md` : undefined,
      disabled: sessionId == null,
      run: () => { if (sessionId) download(`/sessions/${sessionId}/export.md`); },
    },
    {
      id: "export-ipynb",
      label: "Export as notebook",
      hint: sessionId ? `${sessionId}.ipynb` : undefined,
      disabled: sessionId == null,
      run: () => { if (sessionId) download(`/sessions/${sessionId}/export.ipynb`); },
    },
    {
      id: "orch-toggle",
      label: orchRunning ? "Pause orchestrator" : "Play orchestrator",
      disabled: orch == null,
      run: () => send({ t: orchRunning ? "pause" : "play", target: "orch" }),
    },
    {
      id: "restart-kernel",
      label: "Restart kernel",
      hint: "drop user_ns, keep messages",
      // Matches the OrchColumn toolbar: not while a cell/generate is in flight.
      disabled: orch == null || orchRunning,
      run: () => send({ t: "restart_kernel" }),
    },
  ], [setMode, newAudit, onOpenSettings, sessionId, orch, orchRunning, savedSessions.length, send]);

  const sessionCommands = useMemo<Command[]>(
    () => savedSessions.map((s) => ({
      id: s.session_id,
      label: s.seed.trim() || s.session_id,
      hint: `${s.n_branches} branch${s.n_branches === 1 ? "" : "es"}`,
      run: () => {
        const url = new URL(location.href);
        url.searchParams.set("session", s.session_id);
        location.assign(url.toString());
      },
    })),
    [savedSessions],
  );

  const source = submode === "root" ? rootCommands : sessionCommands;
  const filtered = useMemo(
    () => source.filter((c) => fuzzy(query, c.label)),
    [source, query],
  );

  // Clamp selection when the filtered list shrinks.
  useEffect(() => {
    if (sel >= filtered.length) setSel(Math.max(0, filtered.length - 1));
  }, [filtered.length, sel]);

  function execute(cmd: Command): void {
    if (cmd.disabled) return;
    cmd.run();
    // "Jump to session…" stays open (it swaps to the sub-list); everything
    // else closes the palette.
    if (cmd.id !== "jump") onClose();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>): void {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((i) => (filtered.length ? (i + 1) % filtered.length : 0));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((i) => (filtered.length ? (i - 1 + filtered.length) % filtered.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const cmd = filtered[sel];
      if (cmd) execute(cmd);
    } else if (e.key === "Backspace" && query === "" && submode === "sessions") {
      e.preventDefault();
      setSubmode("root");
      setSel(0);
    }
  }

  return (
    <Modal
      show
      onHide={onClose}
      title={submode === "root" ? "Commands" : "Jump to session"}
      width="min(560px, 92vw)"
      bodyClassName="cmd-palette"
      padded={false}
    >
      <div className="cmd-palette-head">
        <input
          ref={inputRef}
          data-autofocus
          className="cmd-palette-input"
          placeholder={
            submode === "root" ? "Type a command…" : "Filter sessions…"
          }
          value={query}
          onChange={(e) => { setQuery(e.target.value); setSel(0); }}
          onKeyDown={onKeyDown}
        />
        <span className="cmd-palette-count">
          {filtered.length}/{source.length}
        </span>
      </div>
      <div className="cmd-palette-list">
        {filtered.length === 0 ? (
          <div className="cmd-palette-empty">
            {submode === "sessions" ? "No saved sessions" : "No matches"}
          </div>
        ) : (
          filtered.map((c, i) => (
            <div
              key={c.id}
              className={
                "cmd-palette-row" +
                (i === sel ? " selected" : "") +
                (c.disabled ? " disabled" : "")
              }
              onMouseEnter={() => setSel(i)}
              onClick={() => execute(c)}
            >
              <span className="cmd-palette-label">{c.label}</span>
              {c.hint && <span className="cmd-palette-hint">{c.hint}</span>}
            </div>
          ))
        )}
      </div>
      <div className="cmd-palette-foot">
        <span>
          <kbd><i className="bi bi-arrow-up" /></kbd>
          <kbd><i className="bi bi-arrow-down" /></kbd> select
        </span>
        <span><kbd><i className="bi bi-arrow-return-left" /></kbd> run</span>
        <span><kbd>esc</kbd> close</span>
      </div>
    </Modal>
  );
}
