import type { ChatMessageUser } from "@tsmono/inspect-common";

import { type JSX, useCallback, useEffect, useRef, useState } from "react";

import { useSession } from "../store/session";
import type { Status } from "../lib/wire";
import { Column } from "./Column";

function uuid(): string {
  return crypto.randomUUID();
}

function statusText(status: Status | null, turn?: number): string {
  const turnInfo = turn != null ? ` · turn ${turn}` : "";
  switch (status) {
    case "running":
      return `running${turnInfo} · generating`;
    case "paused":
      return `paused${turnInfo}`;
    case "ended":
      return `ended${turnInfo}`;
    default:
      return "idle";
  }
}

/**
 * The running-audit desk: two transcript columns + a thin seed header + the
 * runline (only transport control) + composer at the bottom.
 */
export function DeskView(): JSX.Element {
  const send = useSession((s) => s.send);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const branches = useSession((s) => s.branches);
  const branchConfig = useSession((s) =>
    s.current ? s.branchConfig[s.current] : undefined
  );
  const error = useSession((s) => s.error);
  const dismissError = useSession((s) => s.dismissError);

  const [feedback, setFeedback] = useState("");
  const [dest, setDest] = useState<"auditor" | "target">("auditor");

  // Drag-to-resize: track pointer down on the handle, update CSS var on move.
  const columnsRef = useRef<HTMLDivElement>(null);
  const onMoveRef = useRef<((mv: PointerEvent) => void) | null>(null);
  const onUpRef = useRef<(() => void) | null>(null);

  const onHandlePointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const container = columnsRef.current;
    if (!container) return;
    const onMove = (mv: PointerEvent): void => {
      const rect = container.getBoundingClientRect();
      const ratio = Math.min(0.8, Math.max(0.2, (mv.clientX - rect.left) / rect.width));
      container.style.setProperty("--auditor-width", `${ratio}fr`);
      container.style.setProperty("--target-width", `${1 - ratio}fr`);
    };
    const onUp = (): void => {
      window.removeEventListener("pointermove", onMoveRef.current!);
      window.removeEventListener("pointerup", onUpRef.current!);
      onMoveRef.current = null;
      onUpRef.current = null;
    };
    onMoveRef.current = onMove;
    onUpRef.current = onUp;
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  useEffect(() => () => {
    if (onMoveRef.current) window.removeEventListener("pointermove", onMoveRef.current);
    if (onUpRef.current) window.removeEventListener("pointerup", onUpRef.current);
  }, []);

  const branch = current!;
  const currentBranch = current ? branches[current] : undefined;
  const seedTitle = currentBranch?.seed ?? branchConfig?.seed ?? "";
  const isRunning = status === "running";
  const isEnded = status === "ended";

  function cycleDest(): void {
    setDest((d) => (d === "auditor" ? "target" : "auditor"));
  }

  return (
    <>
      {/* thin seed/context strip */}
      {seedTitle && (
        <div className="desk-header" title={seedTitle}>
          <span className="desk-seed">{seedTitle}</span>
        </div>
      )}

      {error && (
        <div className="error-banner">
          <span>{error}</span>
          <button onClick={dismissError} title="Dismiss">✕</button>
        </div>
      )}

      {/* runline — the one transport, fixed below seed strip */}
      <div className={`runline status-${status ?? "idle"}`}>
        <div className="rl-controls">
          {/* play/pause toggle */}
          <button
            className={`rl-play${isRunning ? " running" : ""}`}
            onClick={() => send({ t: isRunning ? "pause" : "play" })}
            disabled={isEnded}
            title={isRunning ? "Pause" : "Play"}
          >
            {isRunning ? "⏸" : "▶"}
          </button>

          {/* step — secondary, smaller */}
          <button
            className="rl-step"
            onClick={() => send({ t: "step" })}
            disabled={isRunning || isEnded}
            title="Step one turn"
          >
            · step
          </button>

          {/* end — stop the audit early */}
          <button
            className="rl-end"
            onClick={() => send({ t: "end" })}
            disabled={isEnded}
            title="End audit"
          >
            ⏹ end
          </button>
        </div>

        <span className="rl-status">{statusText(status)}</span>
      </div>

      <div className="columns" ref={columnsRef}>
        <Column branch={branch} role="auditor" />
        <div className="col-drag-handle" onPointerDown={onHandlePointerDown} />
        <Column branch={branch} role="target" />
      </div>

      <div className="composer-zone">
        <div className="composer">
          <textarea
            className="composer-input"
            rows={2}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder={
              dest === "auditor"
                ? "Feedback to the auditor…"
                : "Message as the user…"
            }
          />
          <div className="composer-lower">
            <button
              className="dest-toggle"
              onClick={cycleDest}
              title="Click to switch destination"
            >
              → {dest}
            </button>
            <button
              className="send"
              disabled={!feedback.trim()}
              onClick={() => {
                const message: ChatMessageUser = {
                  id: uuid(),
                  role: "user",
                  content: feedback,
                };
                send({ t: "inject", branch, role: dest, message });
                setFeedback("");
              }}
            >
              ↑
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
