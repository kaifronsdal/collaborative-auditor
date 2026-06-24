import type { ChatMessageUser } from "@tsmono/inspect-common";

import { type JSX, useCallback, useEffect, useRef, useState } from "react";

import { useSession } from "../store/session";
import { Column, type ColumnHandle } from "./Column";
import { SwimlaneColumn } from "./SwimlaneColumn";
import { IconClose, IconPause, IconPlay, IconSend, IconStep, IconStop } from "./icons";

function uuid(): string {
  return crypto.randomUUID();
}

/**
 * The running-audit desk: two transcript columns + a thin seed header + the
 * runline (only transport control) + composer at the bottom.
 */
export function DeskView(): JSX.Element {
  const transport = useSession((s) => s.transport);
  const injectMsg = useSession((s) => s.inject);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const branches = useSession((s) => s.branches);
  const branchConfig = useSession((s) =>
    s.current ? s.branchConfig[s.current] : undefined
  );
  const error = useSession((s) => s.error);
  const dismissError = useSession((s) => s.dismissError);

  const auditorRef = useRef<ColumnHandle>(null);
  const targetRef = useRef<ColumnHandle>(null);
  const [linked, setLinked] = useState(false);
  // Track which column the pointer is over so linked-scroll only drives the
  // *other* column (a programmatic scrollIntoView fires onScroll on the
  // receiver, which would otherwise bounce back).
  const hoverRole = useRef<"auditor" | "target" | null>(null);

  const syncFrom = useCallback((from: "auditor" | "target", ts: string) => {
    if (hoverRole.current !== from) return;
    (from === "auditor" ? targetRef : auditorRef).current?.scrollToTimestamp(ts);
  }, []);

  // `.` jumps the counterpart of whichever column is hovered to its centered row.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "." || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) return;
      const from = hoverRole.current;
      if (!from) return;
      const ts = (from === "auditor" ? auditorRef : targetRef).current?.centeredTimestamp();
      if (ts) (from === "auditor" ? targetRef : auditorRef).current?.scrollToTimestamp(ts);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const [feedback, setFeedback] = useState("");

  // Drag-to-resize: vars live on `.desk-body` so the composer (which sits
  // under the auditor column only) tracks the same split as `.columns`.
  const bodyRef = useRef<HTMLDivElement>(null);
  const onMoveRef = useRef<((mv: PointerEvent) => void) | null>(null);
  const onUpRef = useRef<(() => void) | null>(null);

  const onHandlePointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const body = bodyRef.current;
    if (!body) return;
    const cols = body.querySelector<HTMLDivElement>(".columns")!;
    const onMove = (mv: PointerEvent): void => {
      const rect = cols.getBoundingClientRect();
      const ratio = Math.min(0.8, Math.max(0.2, (mv.clientX - rect.left) / rect.width));
      body.style.setProperty("--auditor-width", `${ratio}fr`);
      body.style.setProperty("--target-width", `${1 - ratio}fr`);
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

  const hasText = feedback.trim().length > 0;

  function sendFeedback(): void {
    const message: ChatMessageUser = { id: uuid(), role: "user", content: feedback };
    // Composer only ever talks to the auditor — target messages go via the
    // auditor's `send_message` tool or per-bubble edit actions, never typed raw.
    injectMsg(branch, "auditor", message);
    setFeedback("");
  }

  // The composer's primary button is context-aware: with text it sends; empty
  // it's the play/pause toggle. One affordance, does the obvious thing.
  const primary = hasText
    ? { Icon: IconSend, title: "Send to auditor", onClick: sendFeedback, mode: "send" as const }
    : isRunning
      ? { Icon: IconPause, title: "Pause", onClick: () => transport("pause"), mode: "pause" as const }
      : { Icon: IconPlay, title: "Play", onClick: () => transport("play"), mode: "play" as const };

  return (
    <>
      {error && (
        <div className="error-banner">
          <span>{error}</span>
          <button onClick={dismissError} title="Dismiss"><IconClose /></button>
        </div>
      )}

      {/* Single header bar: status-dot · seed (left) — step / end (right).
          Play/pause lives in the composer's primary button. */}
      <div className={`runline status-${status ?? "idle"}`}>
        <span className={`rl-dot rl-dot-${status ?? "idle"}`} title={status ?? "idle"} />
        <span className="rl-seed" title={seedTitle}>{seedTitle || "—"}</span>
        <div className="rl-controls">
          <button
            className={`rl-link${linked ? " on" : ""}`}
            onClick={() => setLinked((v) => !v)}
            title={linked ? "Unlink scroll (columns scroll independently)" : "Link scroll (scrolling one column tracks the other). Press . for a one-off jump."}
          >
            link scroll
          </button>
          <button
            className="rl-step"
            onClick={() => transport("step")}
            disabled={isRunning || isEnded}
            title="Step one turn"
          >
            <IconStep size={12} /> step
          </button>
          <button
            className="rl-end"
            onClick={() => transport("end")}
            disabled={isEnded}
            title="End audit"
          >
            <IconStop size={12} /> end
          </button>
        </div>
      </div>

      <div className="desk-body" ref={bodyRef}>
        <div className="columns">
          <div onPointerEnter={() => (hoverRole.current = "auditor")} className="col-wrap">
            <Column
              ref={auditorRef}
              branch={branch}
              role="auditor"
              linked={linked}
              onSync={(ts) => syncFrom("auditor", ts)}
            />
          </div>
          <div className="col-drag-handle" onPointerDown={onHandlePointerDown} />
          <div onPointerEnter={() => (hoverRole.current = "target")} className="col-wrap">
            <SwimlaneColumn
              ref={targetRef}
              branch={branch}
              linked={linked}
              onSync={(ts) => syncFrom("target", ts)}
            />
          </div>
        </div>

        {/* Composer docks under the auditor column — the only thing it sends to.
            The grid matches .columns so it tracks the drag split. */}
        <div className="composer-zone">
          <div className="composer">
            <textarea
              className="composer-input"
              rows={1}
              value={feedback}
              onChange={(e) => {
                setFeedback(e.target.value);
                const t = e.target;
                t.style.height = "auto";
                t.style.height = `${Math.min(t.scrollHeight, 140)}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (!isEnded || hasText) primary.onClick();
                }
              }}
              placeholder="Steer the auditor…"
            />
            <div className="composer-lower">
              <span className="composer-hint">enter to send · shift+enter newline</span>
              <button
                className={`primary primary-${primary.mode}`}
                onClick={primary.onClick}
                disabled={!hasText && isEnded}
                title={hasText ? primary.title : `${primary.title} (Enter)`}
              >
                <primary.Icon size={16} />
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
