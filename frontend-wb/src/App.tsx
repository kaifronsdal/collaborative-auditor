import { type JSX, useEffect, useState } from "react";
import {
  ComponentIconProvider,
  type ComponentIcons,
} from "@tsmono/react/components/ComponentIconContext";

import { useSession } from "./store/session";
import { IconClose } from "./components/icons";
import { Sidebar } from "./components/Sidebar";
import { StartView } from "./components/StartView";
import { DeskView } from "./components/DeskView";
import { CommandPalette } from "./components/CommandPalette";
import { SettingsModal } from "./components/SettingsModal";
import { useKeyboardShortcuts } from "./components/useKeyboardShortcuts";

// bootstrap-icon class names for the @tsmono/react shared components
// (Modal close button, etc). Mirrors inspect-view's baseApplicationIcons.
const componentIcons: ComponentIcons = {
  chevronDown: "bi bi-chevron-down",
  chevronUp: "bi bi-chevron-up",
  clearText: "bi bi-x-circle-fill",
  close: "bi bi-x",
  code: "bi bi-code-slash",
  confirm: "bi bi-check",
  copy: "bi bi-copy",
  error: "bi bi-exclamation-circle-fill",
  menu: "bi bi-list",
  next: "bi bi-chevron-right",
  noSamples: "bi bi-ban",
  play: "bi bi-play-fill",
  previous: "bi bi-chevron-left",
  toggleRight: "bi bi-chevron-right",
};

export function App(): JSX.Element {
  const connect = useSession((s) => s.connect);
  const current = useSession((s) => s.current);
  const hasOrch = useSession((s) => s.orchestrator != null);
  const pendingNewAudit = useSession((s) => s.pendingNewAudit);
  const reconnecting = useSession((s) => s.reconnecting);
  const error = useSession((s) => s.error);
  const dismissError = useSession((s) => s.dismissError);

  useEffect(() => {
    // App-lifetime singleton. `connect` is idempotent — StrictMode double-invoke
    // opens exactly one socket; we deliberately do NOT close on cleanup.
    const sid = new URLSearchParams(location.search).get("session") ?? "default";
    connect(sid);
  }, [connect]);

  // P2 — ⌘K palette + global shortcuts. The palette can open Settings; the
  // Sidebar keeps its own gear-button `SettingsModal` (independent state, but
  // only one is ever mounted at a time in practice).
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  useKeyboardShortcuts({ paletteOpen, setPaletteOpen });

  // Show DeskView when there is an active branch OR an orchestrator running
  // (M1: the orch column can drive the desk before any M0 branch exists —
  // Column/SwimlaneColumn render empty-state on a null branch id via the
  // `?? EMPTY` selectors). `pendingNewAudit` still forces StartView so the
  // user can spin up a new M0 audit alongside a live orchestrator.
  const showStart = (current == null && !hasOrch) || pendingNewAudit;

  return (
    <ComponentIconProvider icons={componentIcons}>
      <div className="app">
        <Sidebar />
        <main className="app-main">
          {/* STRESS-V2 P2: WS dropped and `onclose` scheduled a backoff retry.
              Lives here (next to E25's error banner) so it also renders on
              StartView — a `Session.load` crash-loop or a first-connect
              failure would otherwise show nothing. Fixed strip; reuses the
              existing `pulse` keyframe. */}
          {reconnecting && (
            <div
              style={{
                position: "fixed", top: 0, left: 0, right: 0, zIndex: 1000,
                padding: "4px 0", textAlign: "center",
                background: "hsl(45 90% 88%)", color: "hsl(30 60% 30%)",
                borderBottom: "1px solid hsl(45 60% 70%)",
                font: "var(--fs-sm) var(--sans)",
                animation: "pulse 1.2s ease-in-out infinite",
              }}
            >
              Reconnecting…
            </div>
          )}
          {/* E25: error banner lives here (not DeskView) so it also renders
              over StartView — a `Session.load` failure lands you on StartView
              with an error, and the pre-E25 DeskView-only banner never showed
              it. `.app-main` is the flex column, so it stacks above either. */}
          {error && (
            <div className="error-banner">
              <span>{error}</span>
              <button onClick={dismissError} title="Dismiss" aria-label="Dismiss">
                <IconClose />
              </button>
            </div>
          )}
          {showStart ? <StartView /> : <DeskView />}
        </main>
        {paletteOpen && (
          <CommandPalette
            onClose={() => setPaletteOpen(false)}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        )}
        {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      </div>
    </ComponentIconProvider>
  );
}
