import { type JSX, useEffect } from "react";
import {
  ComponentIconProvider,
  type ComponentIcons,
} from "@tsmono/react/components/ComponentIconContext";

import { useSession } from "./store/session";
import { Sidebar } from "./components/Sidebar";
import { StartView } from "./components/StartView";
import { DeskView } from "./components/DeskView";

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

  useEffect(() => {
    // App-lifetime singleton. `connect` is idempotent — StrictMode double-invoke
    // opens exactly one socket; we deliberately do NOT close on cleanup.
    const sid = new URLSearchParams(location.search).get("session") ?? "default";
    connect(sid);
  }, [connect]);

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
          {showStart ? <StartView /> : <DeskView />}
        </main>
      </div>
    </ComponentIconProvider>
  );
}
