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
  const pendingNewAudit = useSession((s) => s.pendingNewAudit);

  useEffect(() => {
    // App-lifetime singleton. `connect` is idempotent — StrictMode double-invoke
    // opens exactly one socket; we deliberately do NOT close on cleanup.
    const sid = new URLSearchParams(location.search).get("session") ?? "default";
    connect(sid);
  }, [connect]);

  // Show StartView when there's no active branch OR when the user has
  // explicitly requested a new audit (pendingNewAudit shields the null
  // from being overwritten by the next backend state broadcast).
  const showStart = !current || pendingNewAudit;

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
