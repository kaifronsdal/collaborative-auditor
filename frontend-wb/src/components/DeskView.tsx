import type { ChatMessageUser } from "@tsmono/inspect-common";

import { type JSX, useCallback, useEffect, useRef, useState } from "react";

import { useSession } from "../store/session";
import { Column, type ColumnHandle } from "./Column";
import { ComposerTextarea } from "./ComposerTextarea";
import { OrchColumn } from "./orch/OrchColumn";
import { ScanControl } from "./ScannerPicker";
import { SwimlaneColumn } from "./SwimlaneColumn";
import { IconClose, IconPause, IconPlay, IconSend } from "./icons";

function uuid(): string {
  return crypto.randomUUID();
}

/**
 * The running-audit desk: two transcript columns + a thin seed header + the
 * composer docked under the auditor column.
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
  const hasOrch = useSession((s) => s.orchestrator != null);

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

  // One-shot sync (pill arrows): scroll `to` so its centered row matches the
  // other column's currently-centered timestamp.
  const syncTo = useCallback((to: "auditor" | "target") => {
    const src = (to === "auditor" ? targetRef : auditorRef).current;
    const dst = (to === "auditor" ? auditorRef : targetRef).current;
    const ts = src?.centeredTimestamp();
    if (ts) dst?.scrollToTimestamp(ts);
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
  // One-shot handoff from a queued-bubble edit action: pick the draft up
  // into the composer, then clear it so the next edit isn't suppressed.
  const composerDraft = useSession((s) => s.composerDraft);
  const setComposerDraft = useSession((s) => s.setComposerDraft);
  useEffect(() => {
    if (composerDraft != null) {
      setFeedback(composerDraft);
      setComposerDraft(null);
    }
  }, [composerDraft, setComposerDraft]);

  // Drag-to-resize: vars live on `.desk-body` so both grid columns track the split.
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
  // R1 debounce (button-audit #5,6; chaos s1): the primary button morphs
  // Send→Play (after clearing text) and Pause→Play (once the backend acks the
  // pause). A double-click's second tap must not fire an accidental `play`.
  // `justSent` arms for 250ms after any Send or Pause and suppresses *only*
  // `play` — pause-after-send stays allowed (legitimate "queue then interrupt";
  // s5 exercises it).
  const [justSent, setJustSent] = useState(false);
  const justSentTimer = useRef<number | undefined>(undefined);
  const armJustSent = (): void => {
    setJustSent(true);
    window.clearTimeout(justSentTimer.current);
    justSentTimer.current = window.setTimeout(() => setJustSent(false), 250);
  };

  function sendFeedback(): void {
    const message: ChatMessageUser = { id: uuid(), role: "user", content: feedback };
    // Composer only ever talks to the auditor — target messages go via the
    // auditor's `send_message` tool or per-bubble edit actions, never typed raw.
    injectMsg(branch, "auditor", message);
    setFeedback("");
  }

  // The composer's primary button is context-aware: with text it sends; empty
  // it's the play/pause toggle. One affordance, does the obvious thing.
  // Pause is an *interrupt* — the backend cancels the in-flight generate, so
  // the optimistic status flip in `transport("pause")` is honest. If input is
  // already queued, the branch immediately re-plays with it (interrupt →
  // redirect); otherwise it parks at the gate.
  const primary = hasText
    ? { Icon: IconSend, title: "Send to auditor", onClick: sendFeedback, mode: "send" as const }
    : isRunning
      ? {
          Icon: IconPause,
          title: "Interrupt & pause (queued input will send)",
          onClick: () => transport("pause"),
          mode: "pause" as const,
        }
      : { Icon: IconPlay, title: "Play", onClick: () => transport("play"), mode: "play" as const };

  const onPrimary = (): void => {
    if (justSent && primary.mode === "play") return;
    primary.onClick();
    // Arm on send + pause (both morph the button toward Play). Not on play —
    // an immediate pause after play is harmless and sometimes intended.
    if (primary.mode !== "play") armJustSent();
  };

  return (
    <>
      {error && (
        <div className="error-banner">
          <span>{error}</span>
          <button onClick={dismissError} title="Dismiss"><IconClose /></button>
        </div>
      )}

      {/* Single header bar: status-dot · seed. Play/pause lives in the
          composer's primary button; step/end are gone. */}
      <div className={`runline status-${status ?? "idle"}`}>
        <span className={`rl-dot rl-dot-${status ?? "idle"}`} title={status ?? "idle"} />
        <span className="rl-seed" title={seedTitle}>{seedTitle || "—"}</span>
      </div>

      <div className={`desk-body${hasOrch ? " has-orch" : ""}`} ref={bodyRef}>
        <div className="columns">
          <div onPointerEnter={() => (hoverRole.current = "auditor")} className="col-wrap">
            <Column
              ref={auditorRef}
              branch={branch}
              role="auditor"
              linked={linked}
              onSync={(ts) => syncFrom("auditor", ts)}
            />
            {/* Composer docks under the auditor column — the only thing it sends to. */}
            <div className="composer">
              <ComposerTextarea
                value={feedback}
                setValue={setFeedback}
                onEnter={() => {
                  if (!isEnded || hasText) onPrimary();
                }}
                placeholder="Steer the auditor…"
              />
              <div className="composer-lower">
                <span className="composer-hint">enter to send · shift+enter newline</span>
                <button
                  className={`primary primary-${primary.mode}`}
                  onClick={onPrimary}
                  disabled={(justSent && primary.mode === "play") || (!hasText && isEnded)}
                  title={hasText ? primary.title : `${primary.title} (Enter)`}
                >
                  <primary.Icon size={16} />
                </button>
              </div>
            </div>
          </div>

          {/* Divider hosts the drag handle (full height) plus the Overleaf-style
              sync pill: ← scrolls auditor to target's center, → the reverse,
              middle toggles linked-scroll. */}
          <div className="col-divider">
            <div className="col-drag-handle" onPointerDown={onHandlePointerDown} />
            <div className="sync-pill">
              <button
                type="button"
                onClick={() => syncTo("auditor")}
                title="Scroll auditor to match target"
              >
                <i className="bi bi-arrow-left" />
              </button>
              <button
                type="button"
                className={linked ? "on" : undefined}
                onClick={() => setLinked((v) => !v)}
                title={
                  linked
                    ? "Unlink scroll (columns scroll independently)"
                    : "Link scroll (scrolling one column tracks the other). Press . for a one-off jump."
                }
              >
                <i className="bi bi-link-45deg" />
              </button>
              <button
                type="button"
                onClick={() => syncTo("target")}
                title="Scroll target to match auditor"
              >
                <i className="bi bi-arrow-right" />
              </button>
            </div>
          </div>

          <div
            onPointerEnter={() => (hoverRole.current = "target")}
            className="col-wrap"
            style={{ position: "relative" }}
          >
            {/* P1.8(b): scan the target's live transcript. Overlaid on the
                column head so it works whichever column component renders
                (SwimlaneColumn / LinearColumn fallback). */}
            <ScanControl branch={branch} />
            <SwimlaneColumn
              ref={targetRef}
              branch={branch}
              linked={linked}
              onSync={(ts) => syncFrom("target", ts)}
            />
          </div>

          {/* M1 orchestrator column (M1-NOTEBOOK.md). Appended as a fourth grid
              track only when an orchestrator is running, so the M0 two-column
              layout (and its drag/sync divider) is untouched. */}
          {hasOrch && <OrchColumn />}
        </div>
      </div>
    </>
  );
}
