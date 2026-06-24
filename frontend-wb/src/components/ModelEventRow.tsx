import type { ChatMessage } from "@tsmono/inspect-common";

import { useState, type JSX } from "react";

import { pairToolCalls, type Turn } from "../lib/events";
import { useSession } from "../store/session";
import { BranchNav } from "./BranchNav";
import { Bubble, renderContent } from "./Bubble";
import { RawModal } from "./RawModal";
import { ToolPair, fromCall, fromToolEvent } from "./ToolPair";

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
  rowRef?: (el: HTMLDivElement | null) => void;
};

/** Flatten a ChatMessage's content to plain text for the edit textarea. */
function contentText(content: ChatMessage["content"]): string {
  return typeof content === "string"
    ? content
    : content.map((c) => ("text" in c ? c.text : "")).join("");
}

type EditableRole = "user" | "system" | "tool";

/** A non-assistant lead bubble in the *target* column with a hover `edit`
 *  action. On save, sends `edit_target_message` (WISHLIST 3c). */
function EditableLeadBubble({ msg }: { msg: ChatMessage }): JSX.Element {
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");

  const role = msg.role as EditableRole;
  const editable =
    msg.id != null && (role === "user" || role === "system" || role === "tool");

  function open() {
    setText(contentText(msg.content));
    setEditing(true);
  }
  function save() {
    if (msg.id == null) return;
    const tcid = msg.role === "tool" ? (msg.tool_call_id ?? undefined) : undefined;
    editTargetMessage(msg.id, role, text, tcid);
    setEditing(false);
  }

  if (editing) {
    return (
      <div className="bubble-wrap">
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
        <div className="bubble-by">{role}</div>
      </div>
    );
  }

  return (
    <div className="lead-wrap">
      <Bubble msg={msg} byline={role} />
      {editable && (
        <div className="msg-actions">
          <button onClick={open} title="edit this message and replay">edit</button>
        </div>
      )}
    </div>
  );
}

export function ModelEventRow({
  turn, turnIndex, auditor, siblingPos, onSwitchSibling, rowRef,
}: Props): JSX.Element {
  const { ev, resolved, tools } = turn;
  const branchAt = useSession((s) => s.branchAt);
  const resampleAt = useSession((s) => s.resampleAt);
  const branchAuditor = useSession((s) => s.branchAuditor);
  const resampleAuditor = useSession((s) => s.resampleAuditor);
  const editAuditorCall = useSession((s) => s.editAuditorCall);
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const rewriteToolCall = useSession((s) => s.rewriteToolCall);
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
  // resolution. Auditor column renders real ToolEvents instead (richer —
  // pending state, timing, structured errors), so suppress message-level
  // pairing there to avoid doubling.
  const callPairs = !auditor && assistant ? pairToolCalls(assistant) : [];

  const [showRaw, setShowRaw] = useState(false);

  const disabled = anchorId == null || !!ev.pending;

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
    <div className="model-event-row" ref={rowRef}>
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
                  ? (args) => {
                      clearRewriteDraft(t.id);
                      handleToolEdit(t.id, args);
                    }
                  : undefined
              }
              onDiscardRewrite={
                auditor && t.id ? () => clearRewriteDraft(t.id) : undefined
              }
            />
          ))}
          {callPairs.map((p) => (
            <ToolPair
              key={p.call.id}
              {...fromCall(p.call, p.result)}
              onEditResult={
                p.result?.id != null
                  ? (text) =>
                      editTargetMessage(
                        p.result!.id!,
                        "tool",
                        text,
                        p.result!.tool_call_id ?? undefined
                      )
                  : undefined
              }
            />
          ))}
        </div>
      )}

      {siblingPos && onSwitchSibling && (
        <BranchNav
          idx={siblingPos.idx}
          total={siblingPos.total}
          onPrev={() => onSwitchSibling(-1)}
          onNext={() => onSwitchSibling(1)}
        />
      )}

      {/* Hover-only action row.
          Target assistant: branch · resample · raw (edit removed — operators
          may resample but not put words in the target's mouth).
          Auditor assistant: branch · resample · raw · copy. */}
      <div className="actions">
        <button
          onClick={handleBranch}
          disabled={disabled}
          title={auditor ? "fork auditor at this turn" : "branch at this turn"}
        >
          branch
        </button>
        <button
          onClick={handleResample}
          disabled={disabled}
          title="regenerate this response"
        >
          resample
        </button>
        <button onClick={() => setShowRaw((v) => !v)} title="toggle raw JSON">raw</button>
        {auditor && (
          <button
            onClick={() => navigator.clipboard.writeText(
              typeof content === "string" ? content : JSON.stringify(content)
            )}
            title="copy text"
          >
            copy
          </button>
        )}
      </div>

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
