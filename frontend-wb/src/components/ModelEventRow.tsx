import type { ChatMessage, ToolEvent } from "@tsmono/inspect-common";

import { memo, useRef, useState, type JSX } from "react";

import { pairToolCalls, type Turn } from "../lib/events";
import { FORK_KINDS, useIsPending } from "../lib/selectors";
import { useSession } from "../store/session";
import { BranchNav } from "./BranchNav";
import { Bubble, TurnScoreChips, renderContent } from "./Bubble";
import { CandidateCell, useOpenBatchId } from "./CandidateCell";
import { RawModal } from "./RawModal";
import { RewritePanel, SelectionPill, useSelectionPill } from "./RewritePanel";
import { contentText } from "./tool-renderers/util";
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
  /** Briefly pulse the row's background — set after a sync-pill jump lands. */
  highlighted?: boolean;
  rowRef?: (el: HTMLDivElement | null) => void;
};

type EditableRole = "user" | "system" | "tool";

/** A non-assistant lead bubble in the *target* column with hover `edit` /
 *  `rewrite` actions, double-click-to-edit, and a selection-pill. On save
 *  (or rewrite-apply), sends `edit_target_message` (WISHLIST 3c). */
function EditableLeadBubble({ msg }: { msg: ChatMessage }): JSX.Element {
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const rewriteTargetMessage = useSession((s) => s.rewriteTargetMessage);
  const clearRewriteDraft = useSession((s) => s.clearRewriteDraft);
  const draft = useSession((s) => (msg.id != null ? s.rewriteDrafts[msg.id] : undefined));
  // OVERNIGHT-SWEEP W-B: `editTargetMessage` is fork-shaped (`_pendingChild`
  // → `_register_and_spawn`); disable while any fork is in flight.
  const forkPending = useIsPending((c) => FORK_KINDS.has(c.t));

  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState("");
  const [rewriteSel, setRewriteSel] = useState<string | undefined>(undefined);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const selection = useSelectionPill(wrapRef);

  const role = msg.role as EditableRole;
  const tcid = msg.role === "tool" ? (msg.tool_call_id ?? undefined) : undefined;
  const editable =
    msg.id != null && (role === "user" || role === "system" || role === "tool");

  function open() {
    setText(contentText(msg.content));
    setEditing(true);
  }
  function save() {
    if (msg.id == null || forkPending) return;
    editTargetMessage(msg.id, role, text, tcid);
    setEditing(false);
  }
  function openRewrite(selected?: string) {
    setRewriteSel(selected);
    setRewriteOpen(true);
    selection.clear();
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
    if (msg.id == null || draft?.content == null || forkPending) return;
    editTargetMessage(msg.id, role, draft.content, tcid);
    discardRewrite();
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
            <button className="edit-save" onClick={save} disabled={forkPending}>
              save & replay
            </button>
            <button className="edit-cancel" onClick={() => setEditing(false)}>cancel</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className="lead-wrap"
      ref={wrapRef}
      onDoubleClick={editable ? open : undefined}
      onMouseUp={(e) => {
        if (editable && !editing && !rewriteOpen) selection.onMouseUp(e);
      }}
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
      {(rewriteOpen || draft) && (
        <RewritePanel
          className="lead-rewrite"
          draft={draft}
          prompt={rewritePrompt}
          setPrompt={setRewritePrompt}
          sel={rewriteSel}
          onSend={sendRewrite}
          onApply={applyRewrite}
          onDiscard={discardRewrite}
        />
      )}
      {selection.pill && editable && (
        <SelectionPill pill={selection.pill} onPick={openRewrite} />
      )}
    </div>
  );
}

function ModelEventRowImpl({
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

  // A2: any fork-shaped command in flight disables every branch/resample/
  // edit action across all rows — they all repoint `current`, so a second
  // fork before the first's `{t:"ack"}` would race it (chaos s4). Replaces
  // the store-level `_pendingChild` sentinel early-return.
  const forkPending = useIsPending((c) => FORK_KINDS.has(c.t));
  const disabled = anchorId == null || !!ev.pending || forkPending;
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
      {/* P1.8(c) — live per-turn scanner badges on the target's reply. */}
      {!auditor && !ev.pending && <TurnScoreChips uuid={anchorId} />}

      {(tools.length > 0 || callPairs.length > 0) && (
        <div className="tool-pairs">
          {tools.map((t) => (
            <AuditorToolPair
              key={t.uuid ?? t.id}
              ev={t}
              turnIndex={turnIndex}
              anchorId={anchorId}
              disabled={!auditor || disabled}
            />
          ))}
          {callPairs.map((p) => (
            <TargetToolPair key={p.call.id} pair={p} />
          ))}
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
              <button
                key={n}
                type="button"
                disabled={forkPending}
                onClick={() => handleCandidates(n)}
              >
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

/**
 * Per-`ToolEvent` wrapper so the `s.rewriteDrafts[id]` subscription is
 * narrowed to this one call — a rewrite draft landing for tool X no longer
 * re-renders every row (which the pre-C2 whole-dict `useSession(s =>
 * s.rewriteDrafts)` did, defeating the {@link ModelEventRow} memo).
 */
function AuditorToolPair({
  ev, turnIndex, anchorId, disabled,
}: {
  ev: ToolEvent;
  turnIndex: number;
  anchorId: string | null | undefined;
  disabled: boolean;
}): JSX.Element {
  const editAuditorCall = useSession((s) => s.editAuditorCall);
  const rewriteToolCall = useSession((s) => s.rewriteToolCall);
  const clearRewriteDraft = useSession((s) => s.clearRewriteDraft);
  const draft = useSession((s) => (ev.id ? s.rewriteDrafts[ev.id] : undefined));
  const canEdit = !!ev.id && !disabled && anchorId != null;
  const onEdit = (args: Record<string, unknown>): void => {
    if (anchorId != null) editAuditorCall(turnIndex, anchorId, ev.id, args);
  };
  return (
    <ToolPair
      {...fromToolEvent(ev)}
      onEdit={canEdit ? onEdit : undefined}
      onRewrite={
        canEdit ? (inst, sel) => rewriteToolCall(turnIndex, ev.id, inst, sel) : undefined
      }
      rewriteDraft={draft}
      onApplyRewrite={
        canEdit
          ? (d) => {
              clearRewriteDraft(ev.id);
              if (d.args) onEdit(d.args);
            }
          : undefined
      }
      onDiscardRewrite={ev.id ? () => clearRewriteDraft(ev.id) : undefined}
    />
  );
}

/** Target-column analogue of {@link AuditorToolPair}: narrow
 *  `s.rewriteDrafts[rid]` subscription + W-B `forkPending` guard on the
 *  `edit_target_message` triggers (`onEditResult`/`onApplyRewrite`). */
function TargetToolPair({
  pair,
}: {
  pair: ReturnType<typeof pairToolCalls>[number];
}): JSX.Element {
  const editTargetMessage = useSession((s) => s.editTargetMessage);
  const rewriteTargetMessage = useSession((s) => s.rewriteTargetMessage);
  const clearRewriteDraft = useSession((s) => s.clearRewriteDraft);
  const rid = pair.result?.id ?? undefined;
  const tcid = pair.result?.tool_call_id ?? undefined;
  const draft = useSession((s) => (rid != null ? s.rewriteDrafts[rid] : undefined));
  const forkPending = useIsPending((c) => FORK_KINDS.has(c.t));
  return (
    <ToolPair
      {...fromCall(pair.call, pair.result)}
      onEditResult={
        rid != null && !forkPending
          ? (text) => editTargetMessage(rid, "tool", text, tcid)
          : undefined
      }
      onRewrite={
        rid != null
          ? (inst, sel) => rewriteTargetMessage(rid, "tool", inst, sel, tcid)
          : undefined
      }
      rewriteDraft={draft}
      onApplyRewrite={
        rid != null && !forkPending
          ? (d) => {
              clearRewriteDraft(rid);
              if (d.content != null) editTargetMessage(rid, "tool", d.content, tcid);
            }
          : undefined
      }
      onDiscardRewrite={rid != null ? () => clearRewriteDraft(rid) : undefined}
    />
  );
}

/** Store `Event` objects have referential identity across renders
 *  (`assignByRole` only clones the array/replaces the one updated element),
 *  so a length + per-index identity check catches every real change without
 *  a `.rev` field on `Turn`. */
function refsEqual<T>(a: readonly T[], b: readonly T[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * OVERNIGHT-SWEEP P23: `eventsToTurns` rebuilds every `Turn` object each
 * parent render, so plain `React.memo` (shallow `Object.is` on props) never
 * hits. `turn.ev` and `turn.tools[i]` are stable store refs though — a row
 * whose ModelEvent/ToolEvents didn't change is safe to skip. `resolved` is
 * derived from the append-only conversation prefix through `turn.ev`, so
 * `ev`-identity implies `resolved`-content-equality. `rowRef`/
 * `onSwitchSibling` are excluded: both close over stable refs
 * (`rowEls.current`, `forks`) and a change to their captured state that
 * *matters* also changes `siblingPos`/`highlighted`, which ARE compared.
 */
function arePropsEqual(prev: Props, next: Props): boolean {
  return (
    prev.turn.ev === next.turn.ev &&
    refsEqual(prev.turn.tools, next.turn.tools) &&
    prev.turnIndex === next.turnIndex &&
    prev.auditor === next.auditor &&
    prev.highlighted === next.highlighted &&
    prev.siblingPos?.idx === next.siblingPos?.idx &&
    prev.siblingPos?.total === next.siblingPos?.total
  );
}

export const ModelEventRow = memo(ModelEventRowImpl, arePropsEqual);
