/**
 * One tool call + its result, rendered as a single card (Proposal 2).
 *
 * Normalized over both sources:
 *   - Auditor column: a real `ToolEvent` (has args + result + pending/error)
 *   - Target column: a `(ToolCall, ChatMessageTool?)` pair from inspect's
 *     `resolveMessages` (call from `assistant.tool_calls`, result the
 *     auditor-supplied `ChatMessageTool` matched by `tool_call_id`)
 *
 * Header shows `fn  key=val · key=val  • status`; click to expand full args
 * + result. No Unicode glyphs — chevron is SVG, status is a CSS dot.
 */
import type { ChatMessageTool, ToolCall, ToolEvent } from "@tsmono/inspect-common";
import { resolveToolInput } from "@tsmono/inspect-components/chat/tools";
import { type JSX, useEffect, useRef, useState } from "react";

import { Chevron, StatusDot } from "./icons";
import { renderTool } from "./tool-renderers";

export type RewriteDraft = {
  status: "pending" | "ready" | "error";
  args?: Record<string, unknown>;
  raw?: string;
  error?: string;
};

export type ToolPairProps = {
  fn: string;
  args: Record<string, unknown>;
  result?: unknown;
  error?: { message: string } | null;
  pending?: boolean;
  /** When set, an `edit args` action appears in the expanded body; saving
   *  calls this with the parsed JSON args (auditor column — WISHLIST 3a). */
  onEdit?: (args: Record<string, unknown>) => void;
  /** When set, an `edit result` action appears in the expanded body; saving
   *  calls this with the new result text (target column — WISHLIST 3c). */
  onEditResult?: (result: string) => void;
  /** When set, a `bi-stars rewrite` action appears: opens an instruction
   *  textarea, sends the freeform instruction (and optional selected text)
   *  to the auditor model for an LLM-assisted args rewrite. */
  onRewrite?: (instruction: string, selectedText?: string) => void;
  /** Current LLM rewrite draft for this call (pending/ready/error). */
  rewriteDraft?: RewriteDraft;
  /** Apply a ready rewrite draft (caller wires this to `editAuditorCall`). */
  onApplyRewrite?: (args: Record<string, unknown>) => void;
  /** Discard the current rewrite draft. */
  onDiscardRewrite?: () => void;
};

export function fromToolEvent(ev: ToolEvent): ToolPairProps {
  return {
    fn: ev.function,
    args: (ev.arguments ?? {}) as Record<string, unknown>,
    result: ev.result,
    error: ev.error ?? null,
    pending: !!ev.pending,
  };
}

export function fromCall(call: ToolCall, result?: ChatMessageTool): ToolPairProps {
  const text =
    result == null
      ? undefined
      : typeof result.content === "string"
        ? result.content
        : result.content.map((b) => ("text" in b ? b.text : `[${b.type}]`)).join("\n");
  return {
    fn: call.function,
    args: (call.arguments ?? {}) as Record<string, unknown>,
    result: text,
    error: result?.error ?? null,
    pending: result == null,
  };
}

/** Short id for display: last 7 chars, prefixed with `…`. */
const shortId = (s: string): string => (s.length > 9 ? `…${s.slice(-7)}` : s);

/** One-line signature from top-level scalar args. */
function signature(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (parts.length >= 3) { parts.push("…"); break; }
    if (typeof v === "string") {
      const sv = /^(tool_call_id|.*_id|id)$/i.test(k) ? shortId(v) : `"${v}"`;
      parts.push(`${k}=${sv}`);
    } else if (typeof v === "number" || typeof v === "boolean" || v === null) {
      parts.push(`${k}=${String(v)}`);
    } else {
      parts.push(`${k}={…}`);
    }
  }
  return parts.join(" · ");
}

function resultText(result: unknown): string {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  if (Array.isArray(result)) {
    return result
      .map((r) => (r && typeof r === "object" && "text" in r ? r.text : `[${r?.type ?? "?"}]`))
      .join("\n");
  }
  return JSON.stringify(result);
}

/** Capture the current `window.getSelection()` if it lives inside `root`. */
function getSelectionWithin(
  root: HTMLElement | null
): { text: string; x: number; y: number } | null {
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

export function ToolPair({
  fn, args, result, error, pending, onEdit, onEditResult,
  onRewrite, rewriteDraft, onApplyRewrite, onDiscardRewrite,
}: ToolPairProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<"args" | "result" | null>(null);
  const [editText, setEditText] = useState("");
  const [editErr, setEditErr] = useState<string | null>(null);
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState("");
  const [rewriteSel, setRewriteSel] = useState<string | undefined>(undefined);
  const [selPill, setSelPill] = useState<{ text: string; x: number; y: number } | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const status = error ? "err" : pending ? "pending" : "ok";
  const hasArgs = Object.keys(args).length > 0;
  // inspect-view's tool-input resolver: picks the canonical display arg for
  // known tools (bash → cmd, python → code, web_search → query, …) and a
  // content-type for highlighting; falls back to a generic call signature.
  const { input, contentType } = resolveToolInput(fn, args);
  const inputStr = typeof input === "string" ? input : undefined;

  // Hide the selection pill on outside mousedown / scroll.
  useEffect(() => {
    if (!selPill) return;
    const clear = () => setSelPill(null);
    window.addEventListener("mousedown", clear, true);
    window.addEventListener("scroll", clear, true);
    return () => {
      window.removeEventListener("mousedown", clear, true);
      window.removeEventListener("scroll", clear, true);
    };
  }, [selPill]);

  function openEdit(mode: "args" | "result") {
    setEditText(
      mode === "args" ? JSON.stringify(args, null, 2) : resultText(result)
    );
    setEditErr(null);
    setEditing(mode);
    setOpen(true);
  }

  function saveEdit() {
    if (editing === "result") {
      onEditResult?.(editText);
      setEditing(null);
      return;
    }
    try {
      const parsed = JSON.parse(editText);
      if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("args must be a JSON object");
      }
      onEdit?.(parsed as Record<string, unknown>);
      setEditing(null);
    } catch (e) {
      setEditErr(e instanceof Error ? e.message : String(e));
    }
  }

  function openRewrite(selected?: string) {
    setRewriteSel(selected);
    setRewriteOpen(true);
    setOpen(true);
    setSelPill(null);
  }

  function sendRewrite() {
    if (!rewritePrompt.trim() || !onRewrite) return;
    onRewrite(rewritePrompt.trim(), rewriteSel);
  }

  function discardRewrite() {
    onDiscardRewrite?.();
    setRewriteOpen(false);
    setRewritePrompt("");
    setRewriteSel(undefined);
  }

  function handleBodyMouseUp(e: React.MouseEvent) {
    if (!onRewrite || editing != null || rewriteOpen) return;
    if (e.target instanceof HTMLElement && e.target.closest("button, textarea, input")) {
      return;
    }
    setSelPill(getSelectionWithin(bodyRef.current));
  }

  const rewritePanel = (rewriteOpen || rewriteDraft) && onRewrite && (
    <div className="tp-slot tp-rewrite">
      <div className="tp-lbl">
        <i className="bi bi-stars" /> rewrite with auditor model
      </div>
      {rewriteDraft?.status === "pending" ? (
        <div className="tp-rewrite-spinner">
          <StatusDot state="pending" /> rewriting…
        </div>
      ) : rewriteDraft?.status === "ready" ? (
        <>
          <pre className="tp-rewrite-preview">
            {JSON.stringify(rewriteDraft.args, null, 2)}
          </pre>
          <div className="tp-edit-actions">
            <button
              type="button"
              onClick={() => {
                if (rewriteDraft.args) onApplyRewrite?.(rewriteDraft.args);
                discardRewrite();
              }}
            >
              apply & replay
            </button>
            <button type="button" onClick={sendRewrite} disabled={!rewritePrompt.trim()}>
              regenerate
            </button>
            <button type="button" onClick={discardRewrite}>discard</button>
          </div>
        </>
      ) : (
        <>
          <textarea
            className="tp-rewrite-input"
            value={rewritePrompt}
            onChange={(e) => setRewritePrompt(e.target.value)}
            placeholder="Tell me how to rewrite this…"
            rows={2}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                sendRewrite();
              }
            }}
          />
          {rewriteSel && (
            <div className="tp-rewrite-sel">
              selection: <code>{rewriteSel.slice(0, 120)}{rewriteSel.length > 120 ? "…" : ""}</code>
            </div>
          )}
          {rewriteDraft?.status === "error" && (
            <div className="tp-edit-err">{rewriteDraft.error}</div>
          )}
          <div className="tp-edit-actions">
            <button type="button" onClick={sendRewrite} disabled={!rewritePrompt.trim()}>
              rewrite
            </button>
            <button type="button" onClick={discardRewrite}>cancel</button>
          </div>
        </>
      )}
    </div>
  );

  return (
    <div className={`tool-pair${open ? " open" : ""}`} data-fn={fn}>
      <button className="tp-head" onClick={() => setOpen((o) => !o)} type="button">
        <Chevron open={open} className="tp-chev" />
        <span className="tp-fn">{fn}</span>
        <span className="tp-sig">{inputStr?.split("\n")[0] ?? signature(args)}</span>
        <StatusDot state={status} />
      </button>
      {open && (
        <div className="tp-body" ref={bodyRef} onMouseUp={handleBodyMouseUp}>
          {editing != null ? (
            <div className="tp-slot tp-edit">
              <div className="tp-lbl">
                {editing === "args" ? "edit args (json)" : "edit result"}
              </div>
              <textarea
                className="tp-edit-textarea"
                value={editText}
                onChange={(e) => { setEditText(e.target.value); setEditErr(null); }}
                rows={Math.min(20, Math.max(4, editText.split("\n").length))}
                autoFocus
                spellCheck={false}
              />
              {editErr && <div className="tp-edit-err">{editErr}</div>}
              <div className="tp-edit-actions">
                <button type="button" onClick={saveEdit}>save & replay</button>
                <button type="button" onClick={() => setEditing(null)}>cancel</button>
              </div>
            </div>
          ) : (
            <>
              {renderTool({ fn, args, result, error }) ?? (
                <>
                  {hasArgs && (
                    <div className="tp-slot">
                      <div className="tp-lbl">args</div>
                      <pre data-ct={contentType}>
                        {inputStr ?? JSON.stringify(args, null, 2)}
                      </pre>
                    </div>
                  )}
                  <div className="tp-slot">
                    <div className="tp-lbl">{error ? "error" : pending ? "awaiting result" : "result"}</div>
                    <pre className={error ? "tp-err" : undefined}>
                      {error ? error.message : resultText(result) || "—"}
                    </pre>
                  </div>
                </>
              )}
              {rewritePanel}
              {(onEdit || onEditResult || onRewrite) && (
                <div className="tp-slot tp-edit-actions">
                  {onEdit && (
                    <button type="button" onClick={() => openEdit("args")}>edit args</button>
                  )}
                  {onRewrite && !rewriteOpen && !rewriteDraft && (
                    <button type="button" onClick={() => openRewrite()}>
                      <i className="bi bi-stars" /> rewrite
                    </button>
                  )}
                  {onEditResult && (
                    <button type="button" onClick={() => openEdit("result")}>edit result</button>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}
      {selPill && onRewrite && (
        <button
          type="button"
          className="tp-sel-pill"
          style={{ left: selPill.x, top: selPill.y }}
          onMouseDown={(e) => e.stopPropagation()}
          onClick={() => openRewrite(selPill.text)}
          title="rewrite this selection"
        >
          <i className="bi bi-stars" />
        </button>
      )}
    </div>
  );
}
