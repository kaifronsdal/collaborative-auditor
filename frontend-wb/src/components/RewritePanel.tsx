/**
 * Shared "rewrite with auditor model" UI (WISHLIST 3a/3c) used by both
 * `EditableLeadBubble` (target-side messages) and `ToolPair` (tool-call
 * args/results). Two pieces:
 *
 *  - {@link useSelectionPill} — tracks a text selection inside a host element
 *    and surfaces a floating `bi-stars` pill at the selection's edge. The
 *    caller wires the pill's click to open the rewrite panel with `sel` set.
 *  - {@link RewritePanel} — the instruction textarea → pending spinner →
 *    ready-preview → apply/regenerate/discard flow. Stateless over the draft;
 *    the store owns `RewriteDraft` and the caller owns `prompt`/`sel`.
 */
import { useEffect, useState, type JSX, type RefObject } from "react";

import { useIsPending } from "../lib/selectors";
import type { RewriteDraft } from "../store/session";
import { StatusDot } from "./icons";

export type { RewriteDraft };

type Pill = { text: string; x: number; y: number };

/** Capture the current `window.getSelection()` if it lives inside `root`. */
function getSelectionWithin(root: HTMLElement | null): Pill | null {
  if (!root) return null;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const text = sel.toString().trim();
  if (!text) return null;
  const anchor = sel.anchorNode;
  if (!anchor || !root.contains(anchor)) return null;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  if (!rect.width && !rect.height) return null;
  return { text, x: rect.right + 6, y: rect.top - 4 };
}

/**
 * Selection-pill state for `hostRef`. Attach `onMouseUp` to the host (guarded
 * by whatever caller-side conditions apply); render the pill via
 * {@link SelectionPill}. The pill auto-clears on any outside mousedown/scroll.
 */
export function useSelectionPill(hostRef: RefObject<HTMLElement | null>): {
  pill: Pill | null;
  clear: () => void;
  onMouseUp: (e: React.MouseEvent) => void;
} {
  const [pill, setPill] = useState<Pill | null>(null);

  useEffect(() => {
    if (!pill) return;
    const clear = () => setPill(null);
    window.addEventListener("mousedown", clear, true);
    window.addEventListener("scroll", clear, true);
    return () => {
      window.removeEventListener("mousedown", clear, true);
      window.removeEventListener("scroll", clear, true);
    };
  }, [pill]);

  return {
    pill,
    clear: () => setPill(null),
    onMouseUp: (e) => {
      if (e.target instanceof HTMLElement && e.target.closest("button, textarea, input")) {
        return;
      }
      setPill(getSelectionWithin(hostRef.current));
    },
  };
}

/** The floating `bi-stars` pill at a captured selection. */
export function SelectionPill({
  pill,
  onPick,
}: {
  pill: Pill;
  onPick: (selected: string) => void;
}): JSX.Element {
  return (
    <button
      type="button"
      className="tp-sel-pill"
      style={{ left: pill.x, top: pill.y }}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={() => onPick(pill.text)}
      title="rewrite this selection"
      aria-label="rewrite this selection"
    >
      <i className="bi bi-stars" />
    </button>
  );
}

type RewritePanelProps = {
  className?: string;
  draft: RewriteDraft | undefined;
  prompt: string;
  setPrompt: (s: string) => void;
  /** Selected text the rewrite is scoped to (from the pill), if any. */
  sel: string | undefined;
  onSend: () => void;
  onApply: () => void;
  onDiscard: () => void;
};

export function RewritePanel({
  className, draft, prompt, setPrompt, sel, onSend, onApply, onDiscard,
}: RewritePanelProps): JSX.Element {
  // W-B: `draft.status === "pending"` already hides the button, but that flip
  // waits on the round-trip; `useIsPending` greys it the instant `send()` runs.
  const sending = useIsPending(
    (c) => c.t === "rewrite_tool_call" || c.t === "rewrite_target_message"
  );
  return (
    <div className={className ?? "tp-slot tp-rewrite"}>
      <div className="tp-lbl">
        <i className="bi bi-stars" /> rewrite with auditor model
      </div>
      {draft?.status === "pending" ? (
        <div className="tp-rewrite-spinner">
          <StatusDot state="pending" /> rewriting…
        </div>
      ) : draft?.status === "ready" ? (
        <>
          <pre className="tp-rewrite-preview">
            {draft.content ?? (draft.args ? JSON.stringify(draft.args, null, 2) : draft.raw)}
          </pre>
          <div className="tp-edit-actions">
            <button
              type="button"
              onClick={onApply}
              disabled={draft.content == null && draft.args == null}
            >
              apply & replay
            </button>
            <button type="button" onClick={onSend} disabled={!prompt.trim() || sending}>
              regenerate
            </button>
            <button type="button" onClick={onDiscard}>discard</button>
          </div>
        </>
      ) : (
        <>
          <textarea
            className="tp-rewrite-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Tell me how to rewrite this…"
            rows={2}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !sending) {
                e.preventDefault();
                onSend();
              }
            }}
          />
          {sel && (
            <div className="tp-rewrite-sel">
              selection: <code>{sel.slice(0, 120)}{sel.length > 120 ? "…" : ""}</code>
            </div>
          )}
          {draft?.status === "error" && (
            <div className="tp-edit-err">{draft.error}</div>
          )}
          <div className="tp-edit-actions">
            <button type="button" onClick={onSend} disabled={!prompt.trim() || sending}>
              rewrite
            </button>
            <button type="button" onClick={onDiscard}>cancel</button>
          </div>
        </>
      )}
    </div>
  );
}
