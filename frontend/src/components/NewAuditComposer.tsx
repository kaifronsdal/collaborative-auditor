import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { v4 as uuidv4 } from "uuid";
import { useSessionStore } from "@/store/session";

const DEFAULT_MODELS = [
  "anthropic/claude-opus-4-5-20251101",
  "anthropic/claude-sonnet-4-5-20250929",
  "anthropic/claude-opus-4-1-20250805",
  "anthropic/claude-haiku-4-5-20251001",
  "openai/gpt-5",
  "openai/gpt-5-mini",
  "openai/o4-mini-2025-04-16",
  "google/gemini-2.5-pro",
  "google/gemini-2.5-flash",
];

function formatModelLabel(model: string): string {
  return model.split("/").pop() ?? model;
}

export function NewAuditComposer() {
  const [, setSearchParams] = useSearchParams();
  const connect = useSessionStore((s) => s.connect);
  const startSession = useSessionStore((s) => s.startSession);
  const [prompt, setPrompt] = useState("");
  const [auditorModel, setAuditorModel] = useState(DEFAULT_MODELS[0]);
  const [targetModel, setTargetModel] = useState(DEFAULT_MODELS[0]);
  const [isCreating, setIsCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  const canSubmit = prompt.trim().length > 0 && !isCreating;

  const handleSubmit = () => {
    if (!canSubmit) return;
    setCreateError(null);
    setIsCreating(true);
    const sessionId = uuidv4();

    connect(sessionId);

    let retries = 0;
    const MAX_RETRIES = 100; // 5 seconds at 50ms intervals

    const waitAndStart = () => {
      if (!mountedRef.current) return;
      const state = useSessionStore.getState();
      if (state.connectionStatus === "connected") {
        startSession(prompt.trim(), auditorModel, targetModel);
        setSearchParams({ session: sessionId });
        setIsCreating(false);
        setPrompt("");
      } else if (state.connectionStatus === "error" || retries >= MAX_RETRIES) {
        useSessionStore.getState().disconnect();
        setIsCreating(false);
        setCreateError(
          state.connectionStatus === "error"
            ? "Failed to connect to server"
            : "Connection timed out"
        );
      } else {
        retries++;
        pollTimerRef.current = setTimeout(waitAndStart, 50);
      }
    };
    pollTimerRef.current = setTimeout(waitAndStart, 50);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div className="flex-1 flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight text-center mb-1">
          Collaborative Auditor
        </h1>
        <p className="text-center text-sm text-[var(--muted-foreground)] mb-8">
          AI safety research audit interface
        </p>

        {/* Claude-style input box */}
        <div className="bg-white dark:bg-[var(--muted)] border border-0.5 border-[var(--border)] rounded-2xl shadow-sm overflow-hidden">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="What behavior do you want to investigate?"
            disabled={isCreating}
            rows={3}
            className="w-full px-4 pt-3.5 pb-1 bg-transparent border-none focus:outline-none disabled:opacity-50 resize-none text-sm placeholder:text-[var(--muted-foreground)]/60"
          />

          {/* Bottom bar: model pickers + send */}
          <div className="flex items-center justify-between px-3 pb-2.5 pt-0.5">
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1">
                <label className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)] font-medium">Target</label>
                <select
                  value={targetModel}
                  onChange={(e) => setTargetModel(e.target.value)}
                  disabled={isCreating}
                  className="text-xs bg-transparent text-[var(--foreground)] border-none focus:outline-none cursor-pointer font-medium py-0.5 pr-4 appearance-none"
                  style={{
                    backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23999'/%3E%3C/svg%3E")`,
                    backgroundRepeat: "no-repeat",
                    backgroundPosition: "right 0 center",
                  }}
                >
                  {DEFAULT_MODELS.map((m) => (
                    <option key={m} value={m}>{formatModelLabel(m)}</option>
                  ))}
                </select>
              </div>
              <span className="text-[var(--muted-foreground)] text-xs">&rarr;</span>
              <div className="flex items-center gap-1">
                <label className="text-[10px] uppercase tracking-wider text-[var(--muted-foreground)] font-medium">Auditor</label>
                <select
                  value={auditorModel}
                  onChange={(e) => setAuditorModel(e.target.value)}
                  disabled={isCreating}
                  className="text-xs bg-transparent text-[var(--foreground)] border-none focus:outline-none cursor-pointer font-medium py-0.5 pr-4 appearance-none"
                  style={{
                    backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23999'/%3E%3C/svg%3E")`,
                    backgroundRepeat: "no-repeat",
                    backgroundPosition: "right 0 center",
                  }}
                >
                  {DEFAULT_MODELS.map((m) => (
                    <option key={m} value={m}>{formatModelLabel(m)}</option>
                  ))}
                </select>
              </div>
            </div>

            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSubmit}
              style={{ borderRadius: canSubmit ? 8 : 16, transition: "all 200ms ease" }}
              className={`w-8 h-8 flex items-center justify-center ${
                canSubmit
                  ? "bg-[var(--foreground)] text-[var(--background)] hover:opacity-90"
                  : "bg-[var(--muted)] text-[var(--muted-foreground)]"
              } disabled:opacity-40 disabled:cursor-not-allowed active:scale-95`}
              title="Start audit"
            >
              <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
                <path d="M3.5 1.5a.5.5 0 0 1 .8-.4l8 6a.5.5 0 0 1 0 .8l-8 6a.5.5 0 0 1-.8-.4v-12z" />
              </svg>
            </button>
          </div>
        </div>
        {createError && (
          <p className="mt-2 text-sm text-red-400 text-center">{createError}</p>
        )}
      </div>
    </div>
  );
}
