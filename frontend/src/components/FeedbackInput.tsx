"use client";

import { useState } from "react";
import { useSessionStore, useIsConnected, useViewState, usePlaybackState, useIsGenerating } from "@/store/session";

export function FeedbackInput() {
  const [feedback, setFeedback] = useState("");
  const sendFeedback = useSessionStore((state) => state.sendFeedback);
  const play = useSessionStore((state) => state.play);
  const pause = useSessionStore((state) => state.pause);
  const step = useSessionStore((state) => state.step);
  const isConnected = useIsConnected();
  const viewState = useViewState();
  const playbackState = usePlaybackState();
  const isGenerating = useIsGenerating();

  const canControl = isConnected && viewState !== null;
  const isPlaying = playbackState === "playing";
  const isStepping = playbackState === "stepping";
  const isRunning = isPlaying || isStepping || isGenerating;
  const hasText = feedback.trim().length > 0;

  const sendAndClear = () => {
    if (hasText && canControl) {
      sendFeedback(feedback.trim());
      setFeedback("");
    }
  };

  const handleSendAndStep = () => {
    sendAndClear();
    step();
  };

  const handleSendAndPlay = () => {
    sendAndClear();
    play();
  };

  const handleSend = () => {
    sendAndClear();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!canControl) return;

      if (hasText && !isRunning) {
        // Send message + step one turn (most common action)
        handleSendAndStep();
      } else if (hasText && isRunning) {
        // Auditor is running — just send the feedback
        handleSend();
      }
      // No text — Enter does nothing
    }
  };

  // ── Render buttons based on state ─────────────────────────
  //
  // The step and play buttons are always in the same two positions.
  // When text is entered they smoothly morph: circle → squircle,
  // muted → primary. The icons stay centered, no overlays needed —
  // the shape+color shift communicates "this will also send."

  const renderButtons = () => {
    if (isRunning && hasText) {
      // State 4: Has text + running → Send+Continue + Pause
      return (
        <>
          <button
            type="button"
            onClick={handleSend}
            disabled={!canControl}
            style={{ borderRadius: 8, transition: "all 200ms ease" }}
            className="w-8 h-8 flex items-center justify-center bg-[var(--foreground)] text-[var(--background)] hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed active:scale-95"
            title="Send message (auditor continues running)"
          >
            <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
              <path d="M3 2.5C3 2.22386 2.77614 2 2.5 2C2.22386 2 2 2.22386 2 2.5V13.5C2 13.7761 2.22386 14 2.5 14C2.77614 14 3 13.7761 3 13.5V2.5ZM5 3.00176C5 2.19 5.91615 1.71648 6.57836 2.18598L13.5788 7.14908C14.1385 7.54593 14.1414 8.37575 13.5845 8.77653L6.58411 13.8142C5.9226 14.2903 5 13.8175 5 13.0026V3.00176ZM13.0004 7.96486L6 3.00175L6 13.0026L13.0004 7.96486Z" />
            </svg>
          </button>
          <button
            type="button"
            onClick={pause}
            disabled={!canControl}
            style={{ borderRadius: 16, transition: "all 200ms ease" }}
            className="w-8 h-8 flex items-center justify-center bg-[var(--muted)] hover:bg-[var(--border)] text-[var(--foreground)] disabled:opacity-40 disabled:cursor-not-allowed active:scale-95"
            title="Pause generation"
          >
            <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
              <path d="M5.5 2.75V13.25C5.5 13.664 5.164 14 4.75 14C4.336 14 4 13.664 4 13.25V2.75C4 2.336 4.336 2 4.75 2C5.164 2 5.5 2.336 5.5 2.75ZM11.25 2C10.836 2 10.5 2.336 10.5 2.75V13.25C10.5 13.664 10.836 14 11.25 14C11.664 14 12 13.664 12 13.25V2.75C12 2.336 11.664 2 11.25 2Z" />
            </svg>
          </button>
        </>
      );
    }

    if (isRunning) {
      // State 2: No text + running → Pause only
      return (
        <button
          type="button"
          onClick={pause}
          disabled={!canControl}
          style={{ borderRadius: 16, transition: "all 200ms ease" }}
          className="w-8 h-8 flex items-center justify-center bg-[var(--muted)] hover:bg-[var(--border)] text-[var(--foreground)] disabled:opacity-40 disabled:cursor-not-allowed active:scale-95"
          title="Pause generation"
        >
          <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
            <path d="M5.5 2.75V13.25C5.5 13.664 5.164 14 4.75 14C4.336 14 4 13.664 4 13.25V2.75C4 2.336 4.336 2 4.75 2C5.164 2 5.5 2.336 5.5 2.75ZM11.25 2C10.836 2 10.5 2.336 10.5 2.75V13.25C10.5 13.664 10.836 14 11.25 14C11.664 14 12 13.664 12 13.25V2.75C12 2.336 11.664 2 11.25 2Z" />
          </svg>
        </button>
      );
    }

    // States 1 & 3: Stopped — step + play always in the DOM.
    // When text is entered: circle → squircle + muted → primary.
    // Same icons, just the button style changes.
    const radius = hasText ? 8 : 16;
    const bg = hasText
      ? "bg-[var(--foreground)] text-[var(--background)] hover:opacity-90"
      : "bg-[var(--muted)] hover:bg-[var(--border)] text-[var(--foreground)]";

    return (
      <>
        <button
          type="button"
          onClick={hasText ? handleSendAndStep : step}
          disabled={!canControl}
          style={{ borderRadius: radius, transition: "all 200ms ease" }}
          className={`w-8 h-8 flex items-center justify-center ${bg} disabled:opacity-40 disabled:cursor-not-allowed active:scale-95`}
          title={hasText ? "Send message and step one turn" : "Step: execute one auditor turn"}
        >
          <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
            <path d="M9.99993 13C9.99993 14.103 9.10293 15 7.99993 15C6.89693 15 5.99993 14.103 5.99993 13C5.99993 11.897 6.89693 11 7.99993 11C9.10293 11 9.99993 11.897 9.99993 13ZM13.2499 2C12.8359 2 12.4999 2.336 12.4999 2.75V4.027C11.3829 2.759 9.75993 2 7.99993 2C5.03293 2 2.47993 4.211 2.06093 7.144C2.00193 7.554 2.28793 7.934 2.69793 7.993C2.73393 7.999 2.76993 8.001 2.80493 8.001C3.17193 8.001 3.49293 7.731 3.54693 7.357C3.86093 5.159 5.77593 3.501 8.00093 3.501C9.52993 3.501 10.9199 4.264 11.7439 5.501H9.75093C9.33693 5.501 9.00093 5.837 9.00093 6.251C9.00093 6.665 9.33693 7.001 9.75093 7.001H13.2509C13.6649 7.001 14.0009 6.665 14.0009 6.251V2.751C14.0009 2.337 13.6649 2.001 13.2509 2.001L13.2499 2Z" />
          </svg>
        </button>
        <button
          type="button"
          onClick={hasText ? handleSendAndPlay : play}
          disabled={!canControl}
          style={{ borderRadius: radius, transition: "all 200ms ease" }}
          className={`w-8 h-8 flex items-center justify-center ${bg} disabled:opacity-40 disabled:cursor-not-allowed active:scale-95`}
          title={hasText ? "Send message and continue" : "Continue until paused or ended"}
        >
          <svg className="w-4 h-4" viewBox="0 0 16 16" fill="currentColor">
            <path d="M3 2.5C3 2.22386 2.77614 2 2.5 2C2.22386 2 2 2.22386 2 2.5V13.5C2 13.7761 2.22386 14 2.5 14C2.77614 14 3 13.7761 3 13.5V2.5ZM5 3.00176C5 2.19 5.91615 1.71648 6.57836 2.18598L13.5788 7.14908C14.1385 7.54593 14.1414 8.37575 13.5845 8.77653L6.58411 13.8142C5.9226 14.2903 5 13.8175 5 13.0026V3.00176ZM13.0004 7.96486L6 3.00175L6 13.0026L13.0004 7.96486Z" />
          </svg>
        </button>
      </>
    );
  };

  return (
    <div className="w-full">
      <div className="bg-white dark:bg-[var(--muted)] border border-0.5 border-[var(--border)] rounded-2xl shadow-sm overflow-hidden">
        {/* Text input */}
        <textarea
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Reply..."
          disabled={!canControl}
          rows={1}
          className="w-full px-4 pt-3 pb-1 bg-transparent border-none focus:outline-none disabled:opacity-50 resize-none text-sm placeholder:text-[var(--muted-foreground)]/60"
          aria-label="Send feedback to auditor"
        />

        {/* Bottom bar — controls */}
        <div className="flex items-center justify-end px-3 pb-2 pt-0.5">
          <div className="flex items-center gap-1.5">
            {renderButtons()}
          </div>
        </div>
      </div>
    </div>
  );
}
