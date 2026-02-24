import { useEffect, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { useSessionStore, useViewState, useIsConnected, useLastError } from "@/store/session";
import { Sidebar } from "@/components/Sidebar";
import { NewAuditComposer } from "@/components/NewAuditComposer";
import { Timeline } from "@/components/Timeline";
import { FeedbackInput } from "@/components/FeedbackInput";
import { ConnectionStatus } from "@/components/ConnectionStatus";

function SessionView({ sessionId }: { sessionId: string }) {
  const connect = useSessionStore((s) => s.connect);
  const disconnect = useSessionStore((s) => s.disconnect);
  const connectionStatus = useSessionStore((s) => s.connectionStatus);
  const isConnected = useIsConnected();
  const viewState = useViewState();
  const lastError = useLastError();

  const connectedSessionRef = useRef<string | null>(null);

  useEffect(() => {
    if (connectedSessionRef.current !== sessionId) {
      if (connectedSessionRef.current) {
        disconnect();
      }
      connectedSessionRef.current = sessionId;
      connect(sessionId);
    }

    return () => {
      if (connectedSessionRef.current === sessionId) {
        connectedSessionRef.current = null;
        disconnect();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <header className="border-b border-0.5 border-[var(--border)] px-6 py-3 flex items-center justify-between backdrop-blur-sm bg-[var(--background)]/80 sticky top-0 z-10 shrink-0">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold tracking-tight">Collaborative Auditor</h1>
          {viewState && (
            <div className="flex items-center gap-2">
              <span className="text-xs px-2 py-0.5 rounded-full bg-[var(--muted)] text-[var(--muted-foreground)] font-medium">
                {viewState.auditor_model}
              </span>
              <span className="text-[var(--muted-foreground)] text-xs">&rarr;</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-[var(--muted)] text-[var(--muted-foreground)] font-medium">
                {viewState.target_model}
              </span>
            </div>
          )}
        </div>
        <ConnectionStatus status={connectionStatus} />
      </header>

      {lastError && (
        <div className="bg-red-900/20 border-b border-0.5 border-red-800/40 px-6 py-2 flex items-center justify-between shrink-0">
          <span className="text-sm text-red-400">
            <span className="font-medium">Error:</span> {lastError}
          </span>
          <button
            onClick={() => useSessionStore.getState().clearError()}
            className="text-red-400 hover:text-red-200 text-sm px-2 transition-colors duration-150"
          >
            &#x2715;
          </button>
        </div>
      )}

      <main className="flex-1 overflow-hidden flex flex-col min-h-0">
        {!viewState ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="text-[var(--muted-foreground)] text-sm">
                {isConnected ? (
                  <span className="inline-flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--primary)] animate-pulse" />
                    Loading session...
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" />
                    Connecting...
                  </span>
                )}
              </div>
            </div>
          </div>
        ) : (
          <>
            <div
              className="flex-1 overflow-y-scroll"
              style={{ scrollbarGutter: "stable both-edges" }}
            >
              <Timeline />
            </div>

            <footer className="p-4 pb-5 shrink-0">
              <div className="max-w-4xl mx-auto">
                <FeedbackInput />
              </div>
            </footer>
          </>
        )}
      </main>
    </div>
  );
}

export function AppLayout() {
  const [searchParams] = useSearchParams();
  const sessionId = searchParams.get("session");

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      {sessionId ? (
        <SessionView key={sessionId} sessionId={sessionId} />
      ) : (
        <NewAuditComposer />
      )}
    </div>
  );
}
