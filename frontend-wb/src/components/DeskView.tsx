import type { ChatMessageUser } from "@tsmono/inspect-common";

import { type JSX, useCallback, useRef, useState } from "react";

import { useSession } from "../store/session";
import type { Status } from "../lib/wire";
import { Column } from "./Column";

function uuid(): string {
  return crypto.randomUUID();
}

function statusText(status: Status | null, turn?: number, maxTurns?: number): string {
  const turnInfo = (turn != null && maxTurns != null) ? ` · turn ${turn}/${maxTurns}` : "";
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
  const branchConfig = useSession((s) =>
    s.current ? s.branchConfig[s.current] : undefined
  );

  const [feedback, setFeedback] = useState("");
  const [dest, setDest] = useState<"auditor" | "target">("auditor");

  // Drag-to-resize: track pointer down on the handle, update CSS var on move.
  const columnsRef = useRef<HTMLDivElement>(null);
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
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  const branch = current!;
  const seedTitle = branchConfig?.seed ?? "";
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
            onClick={() => send({ t: "play" })}
            disabled={isRunning || isEnded}
            title="Step one turn"
          >
            · step
          </button>
        </div>

        <span className="rl-status">{statusText(status, undefined, branchConfig?.max_turns)}</span>
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
