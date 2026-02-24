import { useSessionStore, usePlaybackState, useIsGenerating, useIsConnected, useViewState } from "@/store/session";

export function PlaybackControls() {
  const play = useSessionStore((state) => state.play);
  const pause = useSessionStore((state) => state.pause);
  const step = useSessionStore((state) => state.step);
  const playbackState = usePlaybackState();
  const isGenerating = useIsGenerating();
  const isConnected = useIsConnected();
  const viewState = useViewState();

  const canControl = isConnected && viewState !== null;

  const isPlaying = playbackState === "playing";
  const isStepping = playbackState === "stepping";
  const isRunning = isPlaying || isStepping || isGenerating;

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={step}
        disabled={!canControl || isRunning}
        className="px-4 py-2.5 bg-[var(--muted)] hover:bg-[var(--border)] border border-0.5 border-[var(--border)] rounded-lg font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 ease-snappy active:scale-[0.97]"
        title="Execute one auditor turn"
      >
        Step
      </button>

      {isRunning ? (
        <button
          onClick={pause}
          disabled={!canControl}
          className="px-4 py-2.5 bg-amber-500 hover:bg-amber-600 text-white rounded-lg font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 ease-snappy active:scale-[0.97] flex items-center gap-2"
          title="Pause generation"
        >
          <PauseIcon />
          Pause
        </button>
      ) : (
        <button
          onClick={play}
          disabled={!canControl}
          className="px-4 py-2.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-medium text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 ease-snappy active:scale-[0.97] flex items-center gap-2"
          title="Continue until paused or ended"
        >
          <PlayIcon />
          Play
        </button>
      )}
    </div>
  );
}

function PlayIcon() {
  return (
    <svg
      className="w-3.5 h-3.5"
      fill="currentColor"
      viewBox="0 0 24 24"
    >
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg
      className="w-3.5 h-3.5"
      fill="currentColor"
      viewBox="0 0 24 24"
    >
      <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
    </svg>
  );
}
