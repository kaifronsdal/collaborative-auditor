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
import { JSONPanel } from "@tsmono/react/components";
import { type JSX, useRef, useState } from "react";

import { CollapsibleContent } from "./CollapsibleContent";
import { Chevron, StatusDot } from "./icons";
import { RewritePanel, SelectionPill, useSelectionPill, type RewriteDraft } from "./RewritePanel";
import { renderTool } from "./tool-renderers";
import { resultText, shortId } from "./tool-renderers/util";

type ToolPairProps = {
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
  /** Apply a ready rewrite draft. Auditor column wires this to
   *  `editAuditorCall(draft.args)`; target column to
   *  `editTargetMessage(draft.content)`. */
  onApplyRewrite?: (draft: RewriteDraft) => void;
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
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const selection = useSelectionPill(bodyRef);

  const status = error ? "err" : pending ? "pending" : "ok";
  const hasArgs = Object.keys(args).length > 0;
  // inspect-view's tool-input resolver: picks the canonical display arg for
  // known tools (bash → cmd, python → code, web_search → query, …) and a
  // content-type for highlighting; falls back to a generic call signature.
  const { input, contentType } = resolveToolInput(fn, args);
  const inputStr = typeof input === "string" ? input : undefined;

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
    selection.clear();
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

  const rewritePanel = (rewriteOpen || rewriteDraft) && onRewrite && (
    <RewritePanel
      draft={rewriteDraft}
      prompt={rewritePrompt}
      setPrompt={setRewritePrompt}
      sel={rewriteSel}
      onSend={sendRewrite}
      onApply={() => {
        if (rewriteDraft) onApplyRewrite?.(rewriteDraft);
        discardRewrite();
      }}
      onDiscard={discardRewrite}
    />
  );

  // Head edit shortcut: pencil opens whichever editor is wired (args on the
  // auditor column, result on the target column).
  const headEditMode: "args" | "result" | null =
    onEdit ? "args" : onEditResult ? "result" : null;

  return (
    <div className={`tool-pair${open ? " open" : ""}`} data-fn={fn}>
      <div
        className="tp-head"
        role="button"
        tabIndex={0}
        onClick={(e) => {
          // Skip the toggle on the second click of a double-click so the
          // dblclick handler (which forces open) doesn't visibly flicker.
          if (e.detail > 1) return;
          setOpen((o) => !o);
        }}
        onDoubleClick={onEdit ? () => openEdit("args") : undefined}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((o) => !o);
          }
        }}
      >
        <Chevron open={open} className="tp-chev" />
        <span className="tp-fn">{fn}</span>
        <span className="tp-sig">{inputStr?.split("\n")[0] ?? signature(args)}</span>
        {(headEditMode || onRewrite) && (
          <span className="tp-head-actions" onClick={(e) => e.stopPropagation()}>
            {headEditMode && (
              <button
                type="button"
                title={headEditMode === "args" ? "edit args" : "edit result"}
                onClick={() => openEdit(headEditMode)}
              >
                <i className="bi bi-pencil" />
              </button>
            )}
            {onRewrite && (
              <button
                type="button"
                title="rewrite with auditor model"
                onClick={() => openRewrite()}
              >
                <i className="bi bi-stars" />
              </button>
            )}
          </span>
        )}
        <StatusDot state={status} />
      </div>
      {open && (
        <div
          className="tp-body"
          ref={bodyRef}
          onMouseUp={(e) => {
            if (onRewrite && editing == null && !rewriteOpen) selection.onMouseUp(e);
          }}
        >
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
                      {inputStr != null ? (
                        <CollapsibleContent>
                          <pre data-ct={contentType}>{inputStr}</pre>
                        </CollapsibleContent>
                      ) : (
                        <JSONPanel data={args} simple />
                      )}
                    </div>
                  )}
                  <div className="tp-slot">
                    <div className="tp-lbl">{error ? "error" : pending ? "awaiting result" : "result"}</div>
                    <CollapsibleContent>
                      <pre className={error ? "tp-err" : undefined}>
                        {error ? error.message : resultText(result) || "—"}
                      </pre>
                    </CollapsibleContent>
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
      {selection.pill && onRewrite && (
        <SelectionPill pill={selection.pill} onPick={openRewrite} />
      )}
    </div>
  );
}
