import { type JSX, useEffect } from "react";

import { useSession } from "./store/session";
import { Sidebar } from "./components/Sidebar";
import { StartView } from "./components/StartView";
import { DeskView } from "./components/DeskView";

export function App(): JSX.Element {
  const connect = useSession((s) => s.connect);
  const current = useSession((s) => s.current);

  useEffect(() => {
    // App-lifetime singleton. `connect` is idempotent — StrictMode double-invoke
    // opens exactly one socket; we deliberately do NOT close on cleanup.
    connect("default");
  }, [connect]);

  return (
    <div className="app">
      <Sidebar />
      <main className="app-main">
        {!current ? <StartView /> : <DeskView />}
      </main>
    </div>
  );
}
