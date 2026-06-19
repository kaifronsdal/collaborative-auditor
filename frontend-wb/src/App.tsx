import { type JSX, useEffect } from "react";

import { useSession } from "./store/session";
import { Sidebar } from "./components/Sidebar";
import { StartView } from "./components/StartView";
import { DeskView } from "./components/DeskView";

export function App(): JSX.Element {
  const connect = useSession((s) => s.connect);
  const current = useSession((s) => s.current);
  const pendingNewAudit = useSession((s) => s.pendingNewAudit);

  useEffect(() => {
    // App-lifetime singleton. `connect` is idempotent — StrictMode double-invoke
    // opens exactly one socket; we deliberately do NOT close on cleanup.
    connect("default");
  }, [connect]);

  // Show StartView when there's no active branch OR when the user has
  // explicitly requested a new audit (pendingNewAudit shields the null
  // from being overwritten by the next backend state broadcast).
  const showStart = !current || pendingNewAudit;

  return (
    <div className="app">
      <Sidebar />
      <main className="app-main">
        {showStart ? <StartView /> : <DeskView />}
      </main>
    </div>
  );
}
