import type { ChatMessage } from "@tsmono/inspect-common";

import { useEffect, useRef, useState, type JSX } from "react";

import { pairToolCalls, type Turn } from "../lib/events";
import { useSession } from "../store/session";
import { BranchNav } from "./BranchNav";
import { Bubble, renderContent } from "./Bubble";
import { CandidateCell, useOpenBatchId } from "./CandidateCell";
import { StatusDot } from "./icons";
import { RawModal } from "./RawModal";
import { ToolPair, fromCall, fromToolEvent, getSelectionWithin } from "./ToolPair";

type Props = {
  turn: Turn;
  /** 0-based ordinal of this `ModelEvent` within its column (= the auditor
   *  tape's `turn_index` for the auditor column). */
  turnIndex: number;
  /** If true, show the auditor-column action set (branch/resample/raw/copy +
   *  per-tool-call edit). Otherwise the target-column set. */
  auditor?: boolean;
  /** When this turn is a fork point: position among its sibling branches.
   *  Drives the inline `‹ idx/total ›` chip. */
  siblingPos?: { idx: number; total: number };
  /** Step to an adjacent sibling at this fork point. */
  onSwitchSibling?: (direction: 1 | -1) => void;
  /** Briefly pulse the row's background — set after a sync-pill jump lands. */
  highlighted?: boolean;
  rowRef?: (el: HTMLDivElement | null) => void;
};

/** Flatten a ChatMessage's content to plain text for the edit textarea. */
function contentText(content: ChatMessage["content"]): string {
  return typeof content === "string"
    ? content
    : content.map((c) => ("text" in c ? c.text : "")).join("");
}

type EditableRole = "user" | "system" | "tool";

/** A non-assistant lead bubble in the *target* column with hover `edit` /
 *  `rewrite` actions, double-click-to-edit, and a selection-pill. On save
 *  (or rewrite-apply), sends `edit_target_message` (WISHLIST 3c). */
function EditableLeadBubble({ msg }: { msg: ChatMessage }): JSX.Element {
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const rewriteTargetMessage = useSession((s) => s.rewriteTargetMessage);
  const clearRewriteDraft = useSession((s) => s.clearRewriteDraft);
  const draft = useSession((s) => (msg.id != null ? s.rewriteDrafts[msg.id] : undefined));

  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState("");
  const [rewriteSel, setRewriteSel] = useState<string | undefined>(undefined);
  const [selPill, setSelPill] = useState<{ text: string; x: number; y: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const role = msg.role as EditableRole;
  const tcid = msg.role === "tool" ? (msg.tool_call_id ?? undefined) : undefined;
  const editable =
    msg.id != null && (role === "user" || role === "system" || role === "tool");

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

  function open() {
    setText(contentText(msg.content));
    setEditing(true);
  }
  function save() {
    if (msg.id == null) return;
    editTargetMessage(msg.id, role, text, tcid);
    setEditing(false);
  }
  function openRewrite(selected?: string) {
    setRewriteSel(selected);
    setRewriteOpen(true);
    setSelPill(null);
  }
  function sendRewrite() {
    if (msg.id == null || !rewritePrompt.trim()) return;
    rewriteTargetMessage(msg.id, role, rewritePrompt.trim(), rewriteSel, tcid);
  }
  function discardRewrite() {
    if (msg.id != null) clearRewriteDraft(msg.id);
    setRewriteOpen(false);
    setRewritePrompt("");
    setRewriteSel(undefined);
  }
  function applyRewrite() {
    if (msg.id == null || draft?.content == null) return;
    editTargetMessage(msg.id, role, draft.content, tcid);
    discardRewrite();
  }
  function handleMouseUp(e: React.MouseEvent) {
    if (!editable || editing || rewriteOpen) return;
    if (e.target instanceof HTMLElement && e.target.closest("button, textarea, input")) {
      return;
    }
    setSelPill(getSelectionWithin(wrapRef.current));
  }

  if (editing) {
    return (
      <div className="bubble-wrap">
        <div className="bubble-by">{role}</div>
        <div className={`bubble ${role} editing`}>
          <textarea
            className="edit-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={Math.min(16, Math.max(3, text.split("\n").length + 1))}
            autoFocus
          />
          <div className="edit-actions">
            <button className="edit-save" onClick={save}>save & replay</button>
            <button className="edit-cancel" onClick={() => setEditing(false)}>cancel</button>
          </div>
        </div>
      </div>
    );
  }

  const rewritePanel = (rewriteOpen || draft) && (
    <div className="lead-rewrite">
      <div className="tp-lbl">
        <i className="bi bi-stars" /> rewrite with auditor model
      </div>
      {draft?.status === "pending" ? (
        <div className="tp-rewrite-spinner">
          <StatusDot state="pending" /> rewriting…
        </div>
      ) : draft?.status === "ready" ? (
        <>
          <pre className="tp-rewrite-preview">{draft.content ?? draft.raw}</pre>
          <div className="tp-edit-actions">
            <button type="button" onClick={applyRewrite} disabled={draft.content == null}>
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
          {draft?.status === "error" && (
            <div className="tp-edit-err">{draft.error}</div>
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
    <div
      className="lead-wrap"
      ref={wrapRef}
      onDoubleClick={editable ? open : undefined}
      onMouseUp={handleMouseUp}
    >
      <Bubble msg={msg} byline={role} />
      {editable && (
        <div className="msg-actions">
          <button onClick={open} title="edit this message and replay">
            <i className="bi bi-pencil" />
          </button>
          <button onClick={() => openRewrite()} title="rewrite with auditor model">
            <i className="bi bi-stars" />
          </button>
        </div>
      )}
      {rewritePanel}
      {selPill && editable && (
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

export function ModelEventRow({
  turn, turnIndex, auditor, siblingPos, onSwitchSibling, highlighted, rowRef,
}: Props): JSX.Element {
  const { ev, resolved, tools } = turn;
  const branchAt = useSession((s) => s.branchAt);
  const resampleAt = useSession((s) => s.resampleAt);
  const branchAuditor = useSession((s) => s.branchAuditor);
  const resampleAuditor = useSession((s) => s.resampleAuditor);
  const requestCandidates = useSession((s) => s.requestCandidates);
  const requestCandidatesAuditor = useSession((s) => s.requestCandidatesAuditor);
  const current = useSession((s) => s.current);
  const editAuditorCall = useSession((s) => s.editAuditorCall);
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const rewriteToolCall = useSession((s) => s.rewriteToolCall);
  const rewriteTargetMessage = useSession((s) => s.rewriteTargetMessage);
  const clearRewriteDraft = useSession((s) => s.clearRewriteDraft);
  const rewriteDrafts = useSession((s) => s.rewriteDrafts);

  // The turn's resolved messages: leading non-assistant (system/user) bubbles,
  // ending in this turn's assistant output (if it's landed). Tool results are
  // already folded onto the assistant by `resolveMessages` — `pairToolCalls`
  // matches them to `tool_calls` for the cards below.
  const assistant = resolved.find((rm) => rm.message.role === "assistant");
  const lead = resolved.filter((rm) => rm.message.role !== "assistant");
  const content = assistant?.message.content;
  const anchorId = ev.output?.choices?.[0]?.message?.id;
  // Target column: tool_calls + paired ChatMessageTool results from inspect's
  // resolution. Auditor column renders real `ToolEvent`s instead (richer —
  // pending state, timing, structured errors); the splice model supplies the
  // parent's real `ToolEvent`s for replayed prefix turns, so there is no
  // no-tools fallback.
  const callPairs = !auditor && assistant ? pairToolCalls(assistant) : [];

  const [showRaw, setShowRaw] = useState(false);
  const [showNPicker, setShowNPicker] = useState(false);

  const disabled = anchorId == null || !!ev.pending;
  const kind = auditor ? "auditor" : "target";
  // Open Resample-N batch at this row (parent = the branch being viewed).
  const openBatch =
    useOpenBatchId(current ?? "", anchorId ?? "", kind) != null && current != null;

  function handleBranch() {
    if (anchorId == null) return;
    if (auditor) branchAuditor(turnIndex, anchorId);
    else branchAt(anchorId);
  }

  function handleResample() {
    if (anchorId == null) return;
    if (auditor) resampleAuditor(turnIndex, anchorId);
    else resampleAt(anchorId);
  }

  function handleCandidates(n: number) {
    if (anchorId == null || current == null) return;
    if (auditor) requestCandidatesAuditor(current, turnIndex, n);
    else requestCandidates(current, anchorId, n);
    setShowNPicker(false);
  }

  function handleToolEdit(callId: string, args: Record<string, unknown>) {
    if (anchorId == null) return;
    editAuditorCall(turnIndex, anchorId, callId, args);
  }

  const hasText =
    content != null &&
    (typeof content === "string"
      ? content.trim().length > 0
      : content.some((b) => "text" in b ? b.text.trim() : true));

  return (
    <div className={`model-event-row${highlighted ? " row-highlight" : ""}`} ref={rowRef}>
      {lead.map((rm, i) =>
        auditor ? (
          <Bubble key={rm.message.id ?? i} msg={rm.message} byline={rm.message.role} />
        ) : (
          <EditableLeadBubble key={rm.message.id ?? i} msg={rm.message} />
        )
      )}

      {/* Byline above the assistant content (header style) so text + tool
          cards read as one unit; bubble is omitted entirely when there's no
          text (tool-only turns), avoiding the stranded-label gap. */}
      <div className="bubble-by turn-head">assistant</div>
      {hasText || ev.pending ? (
        <div className="bubble assistant">
          {content != null && renderContent(content)}
          {ev.pending && <span className="cursor" />}
        </div>
      ) : null}

      {(tools.length > 0 || callPairs.length > 0) && (
        <div className="tool-pairs">
          {tools.map((t) => (
            <ToolPair
              key={t.uuid ?? t.id}
              {...fromToolEvent(t)}
              onEdit={
                auditor && t.id && !disabled
                  ? (args) => handleToolEdit(t.id, args)
                  : undefined
              }
              onRewrite={
                auditor && t.id && !disabled
                  ? (inst, sel) => rewriteToolCall(turnIndex, t.id, inst, sel)
                  : undefined
              }
              rewriteDraft={auditor && t.id ? rewriteDrafts[t.id] : undefined}
              onApplyRewrite={
                auditor && t.id && !disabled
                  ? (draft) => {
                      clearRewriteDraft(t.id);
                      if (draft.args) handleToolEdit(t.id, draft.args);
                    }
                  : undefined
              }
              onDiscardRewrite={
                auditor && t.id ? () => clearRewriteDraft(t.id) : undefined
              }
            />
          ))}
          {callPairs.map((p) => {
            const rid = p.result?.id ?? undefined;
            const tcid = p.result?.tool_call_id ?? undefined;
            return (
              <ToolPair
                key={p.call.id}
                {...fromCall(p.call, p.result)}
                onEditResult={
                  rid != null
                    ? (text) => editTargetMessage(rid, "tool", text, tcid)
                    : undefined
                }
                onRewrite={
                  rid != null
                    ? (inst, sel) =>
                        rewriteTargetMessage(rid, "tool", inst, sel, tcid)
                    : undefined
                }
                rewriteDraft={rid != null ? rewriteDrafts[rid] : undefined}
                onApplyRewrite={
                  rid != null
                    ? (draft) => {
                        clearRewriteDraft(rid);
                        if (draft.content != null) {
                          editTargetMessage(rid, "tool", draft.content, tcid);
                        }
                      }
                    : undefined
                }
                onDiscardRewrite={rid != null ? () => clearRewriteDraft(rid) : undefined}
              />
            );
          })}
        </div>
      )}

      {/* Hover-reveal icon row (claude.ai style). When this turn is a fork
          point the BranchNav chip sits inline and forces the row visible
          (`.actions:has(.branch-nav)`) so fork points stay scannable.
          Target assistant: copy · resample · branch · raw (edit deliberately
          absent — operators may resample but not put words in the target's
          mouth). Auditor assistant: same set. */}
      <div className="actions">
        {siblingPos && onSwitchSibling && (
          <BranchNav
            idx={siblingPos.idx}
            total={siblingPos.total}
            onPrev={() => onSwitchSibling(-1)}
            onNext={() => onSwitchSibling(1)}
          />
        )}
        <button
          onClick={() => navigator.clipboard.writeText(
            typeof content === "string" ? content : JSON.stringify(content)
          )}
          title="copy text"
        >
          <i className="bi bi-clipboard" />
        </button>
        <button
          onClick={handleResample}
          disabled={disabled}
          title="resample — regenerate this response"
        >
          <i className="bi bi-arrow-clockwise" />
        </button>
        {showNPicker ? (
          <span className="n-picker">
            {[3, 5, 8].map((n) => (
              <button key={n} type="button" onClick={() => handleCandidates(n)}>
                {n}
              </button>
            ))}
            <button type="button" onClick={() => setShowNPicker(false)} title="cancel">
              <i className="bi bi-x" />
            </button>
          </span>
        ) : (
          <button
            onClick={() => setShowNPicker(true)}
            disabled={disabled || openBatch}
            title="resample N — generate several alternatives and pick one"
          >
            <i className="bi bi-collection" />
          </button>
        )}
        <button
          onClick={handleBranch}
          disabled={disabled}
          title={auditor ? "branch — fork auditor at this turn" : "branch at this turn"}
        >
          <i className="bi bi-signpost-split" />
        </button>
        <button onClick={() => setShowRaw((v) => !v)} title="raw JSON">
          <i className="bi bi-braces" />
        </button>
      </div>

      {anchorId != null && current != null && (
        <CandidateCell branch={current} anchor={anchorId} kind={kind} />
      )}

      {showRaw && (
        <RawModal
          value={ev}
          title={`${ev.event} · ${ev.model}`}
          onClose={() => setShowRaw(false)}
        />
      )}
    </div>
  );
}
