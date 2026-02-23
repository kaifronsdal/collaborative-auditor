"use client";

import { useEffect, useRef, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useSessionStore, useViewState, useIsConnected, useIsGenerating, useLastError } from "@/store/session";
import { Timeline } from "@/components/Timeline";
import { FeedbackInput } from "@/components/FeedbackInput";
import { ConnectionStatus } from "@/components/ConnectionStatus";
import { installDebugBridge } from "@/lib/debugBridge";

function SessionContent() {
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("id");
  const initialPrompt = searchParams.get("prompt");
  const auditorModel = searchParams.get("auditor");
  const targetModel = searchParams.get("target");

  const connect = useSessionStore((state) => state.connect);
  const disconnect = useSessionStore((state) => state.disconnect);
  const connectionStatus = useSessionStore((state) => state.connectionStatus);
  const startSession = useSessionStore((state) => state.startSession);
  const isConnected = useIsConnected();
  const viewState = useViewState();
  const isGenerating = useIsGenerating();
  const lastError = useLastError();

  // Track if we've connected for this session to avoid duplicate connections in Strict Mode
  const connectedSessionRef = useRef<string | null>(null);

  // Install debug bridge for text-based UI testing (reads from same Zustand store)
  useEffect(() => {
    installDebugBridge(useSessionStore);
  }, []);

  // Connect to WebSocket when session ID is available
  useEffect(() => {
    if (sessionId && connectedSessionRef.current !== sessionId) {
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

  // Start session once connected if we have params and no viewState yet.
  // handle_start_session on the server is idempotent (checks if session exists
  // and just pushes state), so no guard or delay is needed.
  useEffect(() => {
    if (isConnected && !viewState && initialPrompt && auditorModel && targetModel) {
      startSession(initialPrompt, auditorModel, targetModel);
    }
  }, [isConnected, viewState, initialPrompt, auditorModel, targetModel, startSession]);

  if (!sessionId) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center space-y-2">
          <p className="text-[var(--foreground)] font-medium">Invalid session URL</p>
          <p className="text-sm text-[var(--muted-foreground)]">
            Missing session ID. Please start a new session from the home page.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-0.5 border-[var(--border)] px-6 py-3 flex items-center justify-between backdrop-blur-sm bg-[var(--background)]/80 sticky top-0 z-10">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold tracking-tight">Collaborative Auditor</h1>
          {viewState && (
            <div className="flex items-center gap-2">
              <span className="text-xs px-2 py-0.5 rounded-full bg-[var(--muted)] text-[var(--muted-foreground)] font-medium">
                {viewState.auditor_model}
              </span>
              <span className="text-[var(--muted-foreground)] text-xs">→</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-[var(--muted)] text-[var(--muted-foreground)] font-medium">
                {viewState.target_model}
              </span>
            </div>
          )}
        </div>
        <ConnectionStatus status={connectionStatus} />
      </header>

      {/* Error banner */}
      {lastError && (
        <div className="bg-red-900/20 border-b border-0.5 border-red-800/40 px-6 py-2 flex items-center justify-between">
          <span className="text-sm text-red-400">
            <span className="font-medium">Error:</span> {lastError}
          </span>
          <button
            onClick={() => useSessionStore.getState().clearError()}
            className="text-red-400 hover:text-red-200 text-sm px-2 transition-colors duration-150"
          >
            ✕
          </button>
        </div>
      )}

      {/* Main content */}
      <main className="flex-1 overflow-hidden flex flex-col">
        {!viewState ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="text-[var(--muted-foreground)] text-sm">
                {isConnected ? (
                  <span className="inline-flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--primary)] animate-pulse" />
                    Starting session...
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
            {/* Timeline */}
            <div
              className="flex-1 overflow-y-scroll"
              style={{ scrollbarGutter: "stable both-edges" }}
            >
              <Timeline />
            </div>

            {/* Controls footer */}
            <footer className="p-4 pb-5">
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

export default function SessionPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-sm text-[var(--muted-foreground)]">Loading...</div>
      </div>
    }>
      <SessionContent />
    </Suspense>
  );
}
