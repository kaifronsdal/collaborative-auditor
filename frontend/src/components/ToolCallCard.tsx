import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ToolCall, ChatMessage, ComputedBranchPoint } from "@/lib/types";
import { extractTextContent, extractTargetResponse, extractTargetToolCalls, parseTargetContent, parseTargetToolCalls } from "@/lib/contentUtils";
import type { ParsedToolCall } from "@/lib/contentUtils";
import {
  useSessionStore,
  useToolCallBranchPoint,
  useTargetBranchPoint,
  useIsGenerating,
  useToolCallRewriteDraft,
} from "@/store/session";
import { BranchNavigation } from "@/components/BranchNavigation";

const BODY_TEXT_CLASS = "text-[0.9375rem] leading-[var(--line-height-content)]";
const LABEL_TEXT_CLASS = "text-xs uppercase tracking-wider font-medium";
const MONO_TEXT_CLASS = "text-xs font-mono";

// =============================================================================
// Shared types & helpers
// =============================================================================

interface SharedToolState {
  toolCall: ToolCall;
  toolResult: ChatMessage | null;
  resultContent: string | null;
  hasError: boolean;
  isGenerating: boolean;
  toolCallBranchPoint: ComputedBranchPoint | null;
  targetResponseBranchPoint: ComputedBranchPoint | null;
  editToolCall: (id: string, newArgs: Record<string, unknown>) => void;
  rewriteToolCall: (
    toolCallId: string,
    instruction: string,
    selectedText?: string,
    targetField?: string
  ) => void;
  clearRewriteDraft: (toolCallId: string) => void;
  handleResampleTarget: () => void;
}

function useAutosizeTextarea(
  textareaRef: React.RefObject<HTMLTextAreaElement | null>,
  value: string,
  enabled: boolean
) {
  useLayoutEffect(() => {
    if (!enabled || !textareaRef.current) return;
    const el = textareaRef.current;
    // Reset first so shrinking works as text is removed.
    el.style.height = "0px";
    el.style.height = `${el.scrollHeight}px`;
  }, [textareaRef, value, enabled]);
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return !!target.closest("button, input, textarea, a, [role='button']");
}

function getSelectionFromRoot(root: HTMLElement | null): { text: string; x: number; y: number } | null {
  if (!root) return null;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  const selectedText = selection.toString().trim();
  if (!selectedText) return null;
  const anchorNode = selection.anchorNode;
  if (!anchorNode || !root.contains(anchorNode)) return null;
  const range = selection.getRangeAt(0);
  const rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height) return null;
  return { text: selectedText, x: rect.right + 8, y: rect.top - 6 };
}

function CollapseChevron({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className="block w-3 h-3 transition-transform duration-150"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      style={{ transform: expanded ? "rotate(0deg)" : "rotate(-90deg)" }}
    >
      <path d="M5 7l5 6 5-6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Status indicator: checkmark, X, pulsing dot, or nothing */
function StatusIndicator({ hasResult, hasError, isWaiting }: { hasResult: boolean; hasError: boolean; isWaiting: boolean }) {
  if (hasError) return <span className="text-red-400 text-xs font-medium">✗</span>;
  if (hasResult) return <span className="text-emerald-500 text-xs font-medium">✓</span>;
  if (isWaiting) return <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse inline-block" />;
  return null;
}

/** Save / Cancel buttons for edit UIs */
function EditButtons({ onSave, onCancel, error }: { onSave: () => void; onCancel: () => void; error?: string | null }) {
  return (
    <div className="space-y-1">
      <div className="flex gap-2">
        <button
          onClick={onSave}
          className="text-xs px-3 py-1 bg-[var(--primary)] text-[var(--primary-foreground)] rounded-lg hover:opacity-90 transition-opacity duration-150"
        >
          Save
        </button>
        <button
          onClick={onCancel}
          className="text-xs px-3 py-1 bg-[var(--muted)] text-[var(--muted-foreground)] rounded-lg hover:bg-[var(--border)] transition-colors duration-150"
        >
          Cancel
        </button>
      </div>
      {error && <div className="text-xs text-red-400">{error}</div>}
    </div>
  );
}

/** Error display */
function ErrorDisplay({ error }: { error: { type: string; message: string } }) {
  return (
    <div className="p-2 rounded-lg text-xs bg-[var(--muted)]/70 text-red-400 border border-0.5 border-red-500/45 whitespace-pre-wrap">
      <span className="font-medium text-red-300">Error ({error.type}): </span>
      {error.message}
    </div>
  );
}

// =============================================================================
// Target content renderer — handles text + thinking blocks
// =============================================================================

function TargetContentRenderer({ content }: { content: string }) {
  const segments = parseTargetContent(content);

  if (segments.length === 0) {
    return null;
  }

  return (
    <div className="space-y-1">
      {segments.map((seg, i) => {
        switch (seg.type) {
          case "text":
            return (
              <div key={i} className={`whitespace-pre-wrap ${BODY_TEXT_CLASS}`}>
                {seg.text}
              </div>
            );
          case "thinking":
            return (
              <div key={i} className={`whitespace-pre-wrap italic text-[var(--muted-foreground)] ${BODY_TEXT_CLASS}`}>
                <span className={`${LABEL_TEXT_CLASS} not-italic text-[var(--muted-foreground)]`}>Thinking </span>
                {seg.text}
              </div>
            );
          case "thinking_summary":
            return (
              <div key={i} className={`whitespace-pre-wrap italic text-[var(--muted-foreground)] ${BODY_TEXT_CLASS}`}>
                <span className={`${LABEL_TEXT_CLASS} not-italic text-[var(--muted-foreground)]`}>Thinking Summary </span>
                {seg.text}
              </div>
            );
          case "thinking_redacted":
            return (
              <div key={i} className={`${BODY_TEXT_CLASS} text-[var(--muted-foreground)]`}>
                <span className={`${LABEL_TEXT_CLASS} text-[var(--muted-foreground)]`}>Thinking </span>
                <span className="italic">[redacted]</span>
              </div>
            );
        }
      })}
    </div>
  );
}

/** Render a nicely formatted value — handles strings with newlines, numbers, booleans, objects */
function FormattedValue({ value }: { value: unknown }) {
  if (typeof value === "string") {
    // Multi-line strings get a code block
    if (value.includes("\n")) {
      return (
        <pre className="bg-[var(--muted)]/50 p-2 rounded-lg text-xs font-mono border border-0.5 border-[var(--border)] whitespace-pre-wrap mt-0.5 overflow-x-auto">
          {value}
        </pre>
      );
    }
    return <span className={`${BODY_TEXT_CLASS} text-[var(--foreground)]`}>{value}</span>;
  }
  if (typeof value === "boolean") {
    return <span className={`${BODY_TEXT_CLASS} text-[var(--foreground)]`}>{value ? "true" : "false"}</span>;
  }
  if (typeof value === "number") {
    return <span className={`${BODY_TEXT_CLASS} text-[var(--foreground)]`}>{value}</span>;
  }
  // Objects/arrays — render as compact JSON
  return (
    <pre className="bg-[var(--muted)]/50 p-2 rounded-lg text-xs font-mono border border-0.5 border-[var(--border)] whitespace-pre-wrap mt-0.5 overflow-x-auto">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Render parsed tool calls in a clean format */
function ToolCallDisplay({ toolCalls }: { toolCalls: ParsedToolCall[] }) {
  return (
    <div className="mt-2 space-y-1">
      {toolCalls.map((tc) => (
        <div key={tc.id} className="rounded-lg bg-[var(--background)]/60 border border-0.5 border-[var(--border)] px-2.5 py-1.5">
          <div className="flex items-center gap-1.5 mb-1">
            <code className="text-xs font-mono font-semibold text-[var(--foreground)]">{tc.name}</code>
            <span className="text-xs text-[var(--muted-foreground)] font-mono">{tc.id}</span>
          </div>
          {Object.keys(tc.arguments).length > 0 && !("_raw" in tc.arguments) && (
            <div className="space-y-0.5">
              {Object.entries(tc.arguments).map(([key, val]) => (
                <div key={key}>
                  <span className="text-xs font-semibold text-[var(--muted-foreground)]">{key}: </span>
                  <FormattedValue value={val} />
                </div>
              ))}
            </div>
          )}
          {"_raw" in tc.arguments && (
            <div>
              <pre className="text-xs font-mono whitespace-pre-wrap">{tc.arguments._raw as string}</pre>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// =============================================================================
// 1. QueryTargetRenderer — standalone floating target message (NOT editable)
// =============================================================================

function QueryTargetRenderer({ state }: { state: SharedToolState }) {
  const { toolResult, resultContent, hasError, isGenerating, toolCall, targetResponseBranchPoint } = state;
  const isWaiting = toolCall.function === "query_target" && !toolResult && isGenerating;
  const [isCollapsed, setIsCollapsed] = useState(false);
  const targetBubbleClass =
    "ml-auto max-w-[92%] bg-[var(--muted)] border border-0.5 border-[var(--border)] rounded-2xl px-3 py-2";

  if (isWaiting) {
    return (
      <div className="group/target py-1">
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-xs tracking-wide uppercase text-emerald-600 dark:text-emerald-400">Target</span>
          <span className="inline-flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" style={{ animationDelay: "0.15s" }} />
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" style={{ animationDelay: "0.3s" }} />
          </span>
          <span className="text-xs text-[var(--muted-foreground)]">responding...</span>
        </div>
      </div>
    );
  }

  if (hasError && toolResult?.error) {
    return (
      <div className="py-1">
        <div className="ml-auto max-w-[92%] flex items-center gap-1.5 mb-0.5">
          <button
            type="button"
            className="font-semibold text-xs tracking-wide uppercase text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1"
            onClick={() => setIsCollapsed(!isCollapsed)}
            aria-expanded={!isCollapsed}
          >
            <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center">
              <CollapseChevron expanded={!isCollapsed} />
            </span>
            TARGET
          </button>
          <StatusIndicator hasResult hasError isWaiting={false} />
        </div>
        {!isCollapsed && (
          <div className="ml-auto max-w-[92%]">
            <ErrorDisplay error={toolResult.error} />
          </div>
        )}
      </div>
    );
  }

  if (!resultContent) return null;

  const hasTargetResponse = resultContent.includes("<target_response");
  if (!hasTargetResponse) {
    return (
      <div className="py-1">
        <div className="ml-auto max-w-[92%] flex items-center gap-1.5 mb-0.5">
          <button
            type="button"
            className="font-semibold text-xs tracking-wide uppercase text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1"
            onClick={() => setIsCollapsed(!isCollapsed)}
            aria-expanded={!isCollapsed}
          >
            <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center">
              <CollapseChevron expanded={!isCollapsed} />
            </span>
            TARGET
          </button>
        </div>
        {!isCollapsed && (
          <div className={targetBubbleClass}>
            <div className={`${BODY_TEXT_CLASS} whitespace-pre-wrap`}>{resultContent}</div>
          </div>
        )}
      </div>
    );
  }

  const targetContent = extractTargetResponse(resultContent);
  const targetToolCallsRaw = extractTargetToolCalls(resultContent);
  const parsedToolCalls = targetToolCallsRaw ? parseTargetToolCalls(targetToolCallsRaw) : null;

  if (targetContent === null) {
    console.error("Failed to extract target response from content containing <target_response> tag");
    return null;
  }

  return (
    <div className="group/target py-1">
      <div className="ml-auto max-w-[92%] flex items-center justify-between mb-0.5">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            className="font-semibold text-xs tracking-wide uppercase text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1"
            onClick={() => setIsCollapsed(!isCollapsed)}
            aria-expanded={!isCollapsed}
          >
            <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center">
              <CollapseChevron expanded={!isCollapsed} />
            </span>
            TARGET
          </button>
          {targetResponseBranchPoint && (
            <BranchNavigation branchPoint={targetResponseBranchPoint} />
          )}
        </div>
        <button
          onClick={state.handleResampleTarget}
          className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md opacity-0 group-hover/target:opacity-100 transition-all duration-150"
          title="Regenerate target response"
        >
          ↻
        </button>
      </div>
      {!isCollapsed && (
        <div className={targetBubbleClass}>
          {targetContent ? (
            <TargetContentRenderer content={targetContent} />
          ) : (
            !parsedToolCalls && !targetToolCallsRaw && (
              <div className={`${BODY_TEXT_CLASS} text-[var(--muted-foreground)] italic`}>(no text content)</div>
            )
          )}
          {parsedToolCalls ? (
            <ToolCallDisplay toolCalls={parsedToolCalls} />
          ) : targetToolCallsRaw ? (
            // Fallback for old-format tool calls
            <div className="mt-1.5 text-xs font-mono text-[var(--muted-foreground)] whitespace-pre-wrap">
              {targetToolCallsRaw}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

// =============================================================================
// 2. CreateToolRenderer — formatted Python function (editable)
// =============================================================================

function CreateToolRenderer({ state }: { state: SharedToolState }) {
  const [isExpanded, setIsExpanded] = useState(true);
  const [isEditing, setIsEditing] = useState(false);
  const [editCode, setEditCode] = useState("");
  const [editEnvDesc, setEditEnvDesc] = useState("");
  const [isRewritePromptOpen, setIsRewritePromptOpen] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState("");
  const [rewriteSelection, setRewriteSelection] = useState<string>("");
  const [selectionPill, setSelectionPill] = useState<{ text: string; x: number; y: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const editCodeRef = useRef<HTMLTextAreaElement | null>(null);
  const { toolCall, toolResult, hasError, isGenerating, toolCallBranchPoint } = state;
  const rewriteDraft = useToolCallRewriteDraft(toolCall.id);

  const functionCode = typeof toolCall.arguments.function_code === "string" ? toolCall.arguments.function_code : "";
  const envDesc = typeof toolCall.arguments.environment_description === "string" ? toolCall.arguments.environment_description : "";

  const hasResult = toolResult !== null;
  const isWaiting = !hasResult && isGenerating;

  const fnNameMatch = functionCode.match(/^def\s+(\w+)/);
  const fnName = fnNameMatch ? fnNameMatch[1] : null;

  const handleStartEdit = () => {
    setEditCode(functionCode);
    setEditEnvDesc(envDesc);
    setIsEditing(true);
  };

  const handleOpenRewritePrompt = () => {
    setIsRewritePromptOpen(true);
  };

  const handleSave = () => {
    state.editToolCall(toolCall.id, {
      ...toolCall.arguments,
      function_code: editCode,
      environment_description: editEnvDesc,
    });
    setIsEditing(false);
  };

  const handleCancel = () => {
    setIsEditing(false);
  };

  useAutosizeTextarea(editCodeRef, editCode, isEditing);

  const isDirty = isEditing && (editCode !== functionCode || editEnvDesc !== envDesc);

  useEffect(() => {
    if (!isEditing) return;

    const onMouseDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node) && !isDirty) {
        setIsEditing(false);
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isDirty) {
        setIsEditing(false);
      }
    };

    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isEditing, isDirty]);

  useEffect(() => {
    if (!rewriteDraft) return;
    if (rewriteDraft.status === "ready") {
      const rewritten = rewriteDraft.rewrittenArguments ?? {};
      const nextCode = typeof rewritten.function_code === "string" ? rewritten.function_code : functionCode;
      const nextEnvDesc = typeof rewritten.environment_description === "string"
        ? rewritten.environment_description
        : envDesc;
      setEditCode(nextCode);
      setEditEnvDesc(nextEnvDesc);
      setIsRewritePromptOpen(false);
      setIsEditing(true);
      state.clearRewriteDraft(toolCall.id);
    } else if (rewriteDraft.status === "error") {
      setIsRewritePromptOpen(true);
    }
  }, [rewriteDraft, functionCode, envDesc, state, toolCall.id]);

  return (
    <div
      ref={rootRef}
      className="group/tool"
      onDoubleClick={(e) => {
        if (!isEditing && !isInteractiveTarget(e.target)) {
          handleStartEdit();
        }
      }}
    >
      <button
        type="button"
        className="flex items-center justify-between w-full text-left py-0.5"
        onClick={() => !isEditing && setIsExpanded(!isExpanded)}
      >
        <div className="flex items-center gap-1.5">
          <span className="text-[var(--muted-foreground)] inline-flex h-4 w-4 shrink-0 items-center justify-center">
            <CollapseChevron expanded={isExpanded} />
          </span>
          <code className="text-xs font-mono text-[var(--muted-foreground)]">create_tool</code>
          {fnName && (
            <code className="text-xs font-mono text-[var(--foreground)]">{fnName}</code>
          )}
          <StatusIndicator hasResult={hasResult} hasError={!!hasError} isWaiting={isWaiting} />
          {toolCallBranchPoint && (
            <BranchNavigation branchPoint={toolCallBranchPoint} />
          )}
        </div>
        {!isEditing && (
          <div className="flex items-center gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleOpenRewritePrompt();
              }}
              className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md opacity-0 group-hover/tool:opacity-100 transition-all duration-150"
              title="Inline edit with model"
            >
              ✦
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); handleStartEdit(); }}
              className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md opacity-0 group-hover/tool:opacity-100 transition-all duration-150"
              title="Edit tool definition"
            >
              ✎
            </button>
          </div>
        )}
      </button>

      {isExpanded && (
        <div className="mt-1 space-y-1.5">
          {isEditing ? (
            <div className="space-y-2">
              <div>
                <label className="text-xs text-[var(--muted-foreground)] block mb-0.5">Description</label>
                <input
                  type="text"
                  value={editEnvDesc}
                  onChange={(e) => setEditEnvDesc(e.target.value)}
                  className="w-full bg-[var(--muted)] px-2 py-1 rounded-lg text-xs border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150"
                />
              </div>
              <div>
                <label className="text-xs text-[var(--muted-foreground)] block mb-0.5">Function code</label>
                <textarea
                  ref={editCodeRef}
                  value={editCode}
                  onChange={(e) => setEditCode(e.target.value)}
                  className="w-full bg-[var(--muted)] p-2 rounded-lg text-xs font-mono border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150 resize-none overflow-hidden"
                  rows={1}
                  autoFocus
                />
              </div>
              <EditButtons onSave={handleSave} onCancel={handleCancel} />
            </div>
          ) : (
            <>
              {envDesc && (
                <div className="text-xs text-[var(--muted-foreground)] italic">{envDesc}</div>
              )}
              {functionCode && (
                <pre
                  className="bg-[var(--muted)]/50 p-2 rounded-lg text-xs overflow-x-auto font-mono border border-0.5 border-[var(--border)] whitespace-pre-wrap"
                  onMouseUp={() => {
                    if (isEditing || isRewritePromptOpen) return;
                    setSelectionPill(getSelectionFromRoot(rootRef.current));
                  }}
                >
                  {functionCode}
                </pre>
              )}
            </>
          )}
          {!isEditing && isRewritePromptOpen && (
            <div className="rounded-lg border border-0.5 border-[var(--border)] bg-[var(--background)] p-2 space-y-2">
              {rewriteDraft?.status === "rewriting" ? (
                <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
                  <span className="inline-flex items-center gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" />
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.15s" }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.3s" }} />
                  </span>
                  <span>Generating...</span>
                </div>
              ) : (
                <>
                  <textarea
                    value={rewritePrompt}
                    onChange={(e) => setRewritePrompt(e.target.value)}
                    className="w-full bg-[var(--muted)] p-2 rounded-lg text-xs border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150 resize-none overflow-hidden"
                    placeholder="Describe how the model should rewrite this tool call..."
                    rows={2}
                    autoFocus
                  />
                  {rewriteSelection && (
                    <div className="text-xs text-[var(--muted-foreground)]">
                      Selection: <code className="font-mono">{rewriteSelection.slice(0, 120)}{rewriteSelection.length > 120 ? "..." : ""}</code>
                    </div>
                  )}
                  {rewriteDraft?.status === "error" && (
                    <div className="text-xs text-red-400">{rewriteDraft.error}</div>
                  )}
                  <div className="flex gap-2">
                    <button
                      onClick={() => {
                        state.rewriteToolCall(toolCall.id, rewritePrompt, rewriteSelection, "function_code");
                      }}
                      disabled={!rewritePrompt.trim()}
                      className="text-xs px-3 py-1 bg-[var(--primary)] text-[var(--primary-foreground)] rounded-lg hover:opacity-90 transition-opacity duration-150 disabled:opacity-50"
                    >
                      Rewrite
                    </button>
                    <button
                      onClick={() => {
                        setIsRewritePromptOpen(false);
                        setRewriteSelection("");
                        setSelectionPill(null);
                        state.clearRewriteDraft(toolCall.id);
                      }}
                      className="text-xs px-3 py-1 bg-[var(--muted)] text-[var(--muted-foreground)] rounded-lg hover:bg-[var(--border)] transition-colors duration-150"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
          {hasError && toolResult?.error && (
            <ErrorDisplay error={toolResult.error} />
          )}
        </div>
      )}
      {selectionPill && !isEditing && !isRewritePromptOpen && (
        <button
          type="button"
          className="fixed z-30 text-xs px-2 py-1 rounded-md border border-0.5 border-[var(--border)] bg-[var(--background)] text-[var(--foreground)] shadow-sm"
          style={{ left: selectionPill.x, top: selectionPill.y }}
          onClick={() => {
            setRewriteSelection(selectionPill.text);
            setIsRewritePromptOpen(true);
            setSelectionPill(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          Inline edit
        </button>
      )}
    </div>
  );
}

// =============================================================================
// 3. CompactToolRenderer — tools with text content, editable inline
// =============================================================================

/** Which tools support inline text editing */
const EDITABLE_TOOLS: Record<string, string> = {
  send_message: "message",
  set_target_system_message: "system_message",
  send_tool_call_result: "result",
};

/** Format args nicely for a specific tool, returning JSX content */
function formatToolContent(fn: string, args: Record<string, unknown>): React.ReactNode {
  switch (fn) {
    case "set_target_system_message": {
      const msg = typeof args.system_message === "string" ? args.system_message : "";
      return <div className={`${BODY_TEXT_CLASS} whitespace-pre-wrap`}>{msg}</div>;
    }
    case "send_message": {
      const msg = typeof args.message === "string" ? args.message : "";
      return <div className={`${BODY_TEXT_CLASS} whitespace-pre-wrap`}>{msg}</div>;
    }
    case "send_tool_call_result": {
      const toolCallId = typeof args.tool_call_id === "string" ? args.tool_call_id : "";
      const result = typeof args.result === "string" ? args.result : "";
      const isError = args.is_error === true;
      return (
        <div className="space-y-1">
          <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
            <span>tool_call_id: <code className="font-mono">{toolCallId}</code></span>
            {isError && <span className="text-red-400 font-medium">(error response)</span>}
          </div>
          {isError ? (
            <div className={`p-2 rounded-lg ${MONO_TEXT_CLASS} bg-[var(--muted)]/70 text-red-400 border border-0.5 border-red-500/45 whitespace-pre-wrap`}>
              {result}
            </div>
          ) : (
            <div className={`${MONO_TEXT_CLASS} whitespace-pre-wrap`}>{result}</div>
          )}
        </div>
      );
    }
    case "end_conversation":
      return null;
    default: {
      return (
        <pre className="bg-[var(--muted)]/50 p-2 rounded-lg text-xs overflow-x-auto font-mono border border-0.5 border-[var(--border)]">
          {JSON.stringify(args, null, 2)}
        </pre>
      );
    }
  }
}

/** Inline summary for the header line */
function headerSummary(fn: string, args: Record<string, unknown>): React.ReactNode {
  switch (fn) {
    case "set_target_system_message":
    case "send_message":
      return null;
    case "send_tool_call_result":
      return null;
    case "end_conversation":
      return null;
    default:
      return (
        <span className="text-[var(--muted-foreground)] text-xs font-mono">
          ({Object.keys(args).join(", ")})
        </span>
      );
  }
}

function CompactToolRenderer({ state }: { state: SharedToolState }) {
  const [isExpanded, setIsExpanded] = useState(true);
  const [isEditing, setIsEditing] = useState(false);
  const [editValue, setEditValue] = useState("");
  const [isRewritePromptOpen, setIsRewritePromptOpen] = useState(false);
  const [rewritePrompt, setRewritePrompt] = useState("");
  const [rewriteSelection, setRewriteSelection] = useState<string>("");
  const [selectionPill, setSelectionPill] = useState<{ text: string; x: number; y: number } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const editValueRef = useRef<HTMLTextAreaElement | null>(null);
  const { toolCall, toolResult, hasError, isGenerating, toolCallBranchPoint } = state;
  const rewriteDraft = useToolCallRewriteDraft(toolCall.id);

  const hasResult = toolResult !== null;
  const isWaiting = !hasResult && isGenerating;
  const isEndConversation = toolCall.function === "end_conversation";

  const content = formatToolContent(toolCall.function, toolCall.arguments);
  const hasContent = content !== null && !isEndConversation;

  // Determine if this tool is editable and which field to edit
  const editableField = EDITABLE_TOOLS[toolCall.function];
  const isEditable = !!editableField;
  const isMonospaceEditable = toolCall.function === "send_tool_call_result";

  const handleStartEdit = () => {
    const currentValue = typeof toolCall.arguments[editableField] === "string"
      ? (toolCall.arguments[editableField] as string)
      : "";
    setEditValue(currentValue);
    setIsEditing(true);
    if (!isExpanded) setIsExpanded(true);
  };

  const handleSave = () => {
    state.editToolCall(toolCall.id, {
      ...toolCall.arguments,
      [editableField]: editValue,
    });
    setIsEditing(false);
  };

  const handleCancel = () => {
    setIsEditing(false);
  };

  useAutosizeTextarea(editValueRef, editValue, isEditing);

  const originalValue = typeof toolCall.arguments[editableField] === "string"
    ? (toolCall.arguments[editableField] as string)
    : "";
  const isDirty = isEditing && editValue !== originalValue;

  useEffect(() => {
    if (!isEditing) return;

    const onMouseDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node) && !isDirty) {
        setIsEditing(false);
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isDirty) {
        setIsEditing(false);
      }
    };

    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isEditing, isDirty]);

  useEffect(() => {
    if (!rewriteDraft) return;
    if (rewriteDraft.status === "ready") {
      const rewritten = rewriteDraft.rewrittenArguments ?? {};
      const draftedValue = typeof rewritten[editableField] === "string"
        ? (rewritten[editableField] as string)
        : originalValue;
      setEditValue(draftedValue);
      setIsRewritePromptOpen(false);
      setIsEditing(true);
      if (!isExpanded) setIsExpanded(true);
      state.clearRewriteDraft(toolCall.id);
    } else if (rewriteDraft.status === "error") {
      setIsRewritePromptOpen(true);
    }
  }, [rewriteDraft, editableField, originalValue, isExpanded, state, toolCall.id]);

  return (
    <div
      ref={rootRef}
      className="group/tool"
      onDoubleClick={(e) => {
        if (isEditable && !isEditing && !isInteractiveTarget(e.target)) {
          handleStartEdit();
        }
      }}
    >
      {/* Header: click anywhere (except action controls) to toggle */}
      <div
        className="flex items-center justify-between py-0.5"
        onClick={() => {
          if (hasContent && !isEditing) {
            setIsExpanded(!isExpanded);
          }
        }}
      >
        <div className="flex items-center gap-1.5">
          {hasContent && (
            <span className="text-[var(--muted-foreground)] inline-flex h-4 w-4 shrink-0 items-center justify-center">
              <CollapseChevron expanded={isExpanded} />
            </span>
          )}
          <code className="text-xs font-mono text-[var(--muted-foreground)]">{toolCall.function}</code>
          {headerSummary(toolCall.function, toolCall.arguments)}
          <StatusIndicator hasResult={hasResult} hasError={!!hasError} isWaiting={isWaiting} />
          {toolCallBranchPoint && (
            <BranchNavigation branchPoint={toolCallBranchPoint} />
          )}
        </div>
        {isEditable && !isEditing && (
          <div className="flex items-center gap-1">
            <button
              onClick={(e) => {
                e.stopPropagation();
                setIsRewritePromptOpen(true);
              }}
              className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md opacity-0 group-hover/tool:opacity-100 transition-all duration-150"
              title={`Inline edit ${editableField} with model`}
            >
              ✦
            </button>
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleStartEdit();
              }}
              className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md opacity-0 group-hover/tool:opacity-100 transition-all duration-150"
              title={`Edit ${editableField}`}
            >
              ✎
            </button>
          </div>
        )}
      </div>

      {/* Formatted content or edit UI — toggled by chevron */}
      {isExpanded && hasContent && (
        <div className="mt-0.5">
          {isEditing ? (
            <div className="space-y-1.5">
              <textarea
                ref={editValueRef}
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                className={`w-full bg-[var(--muted)] p-2 rounded-lg border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150 resize-none overflow-hidden ${isMonospaceEditable ? MONO_TEXT_CLASS : BODY_TEXT_CLASS}`}
                rows={1}
                autoFocus
              />
              <EditButtons onSave={handleSave} onCancel={handleCancel} />
            </div>
          ) : (
            <div
              onMouseUp={() => {
                if (!isEditable || isEditing || isRewritePromptOpen) return;
                setSelectionPill(getSelectionFromRoot(rootRef.current));
              }}
            >
              {content}
            </div>
          )}
        </div>
      )}

      {isEditable && !isEditing && isRewritePromptOpen && (
        <div className="mt-1 rounded-lg border border-0.5 border-[var(--border)] bg-[var(--background)] p-2 space-y-2">
          {rewriteDraft?.status === "rewriting" ? (
            <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
              <span className="inline-flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" />
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.15s" }} />
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.3s" }} />
              </span>
              <span>Generating...</span>
            </div>
          ) : (
            <>
              <textarea
                value={rewritePrompt}
                onChange={(e) => setRewritePrompt(e.target.value)}
                className="w-full bg-[var(--muted)] p-2 rounded-lg text-xs border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150 resize-none overflow-hidden"
                placeholder="Describe how the model should rewrite this tool call..."
                rows={2}
                autoFocus
              />
              {rewriteSelection && (
                <div className="text-xs text-[var(--muted-foreground)]">
                  Selection: <code className="font-mono">{rewriteSelection.slice(0, 120)}{rewriteSelection.length > 120 ? "..." : ""}</code>
                </div>
              )}
              {rewriteDraft?.status === "error" && (
                <div className="text-xs text-red-400">{rewriteDraft.error}</div>
              )}
              <div className="flex gap-2">
                <button
                  onClick={() => state.rewriteToolCall(toolCall.id, rewritePrompt, rewriteSelection, editableField)}
                  disabled={!rewritePrompt.trim()}
                  className="text-xs px-3 py-1 bg-[var(--primary)] text-[var(--primary-foreground)] rounded-lg hover:opacity-90 transition-opacity duration-150 disabled:opacity-50"
                >
                  Rewrite
                </button>
                <button
                  onClick={() => {
                    setIsRewritePromptOpen(false);
                    setRewriteSelection("");
                    setSelectionPill(null);
                    state.clearRewriteDraft(toolCall.id);
                  }}
                  className="text-xs px-3 py-1 bg-[var(--muted)] text-[var(--muted-foreground)] rounded-lg hover:bg-[var(--border)] transition-colors duration-150"
                >
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {/* Error from tool execution */}
      {hasError && toolResult?.error && (
        <div className="mt-1">
          <ErrorDisplay error={toolResult.error} />
        </div>
      )}
      {selectionPill && isEditable && !isEditing && !isRewritePromptOpen && (
        <button
          type="button"
          className="fixed z-30 text-xs px-2 py-1 rounded-md border border-0.5 border-[var(--border)] bg-[var(--background)] text-[var(--foreground)] shadow-sm"
          style={{ left: selectionPill.x, top: selectionPill.y }}
          onClick={() => {
            setRewriteSelection(selectionPill.text);
            setIsRewritePromptOpen(true);
            setSelectionPill(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          Inline edit
        </button>
      )}
    </div>
  );
}

// =============================================================================
// Main ToolCallCard — router + shared state
// =============================================================================

interface ToolCallCardProps {
  toolCall: ToolCall;
}

export function ToolCallCard({ toolCall }: ToolCallCardProps) {
  const editToolCallFn = useSessionStore((state) => state.editToolCall);
  const rewriteToolCallFn = useSessionStore((state) => state.rewriteToolCall);
  const clearRewriteDraftFn = useSessionStore((state) => state.clearRewriteDraft);
  const resampleTargetResponse = useSessionStore((state) => state.resampleTargetResponse);

  const toolResult = useSessionStore((state) => {
    const branch = state.viewState?.current_branch;
    if (!branch) return null;
    return branch.auditor_messages.find(
      (m) => m.role === "tool" && m.tool_call_id === toolCall.id
    ) ?? null;
  });

  const toolCallBranchPoint = useToolCallBranchPoint(toolCall.id);
  const targetResponseBranchPoint = useTargetBranchPoint(toolCall.id);
  const isGenerating = useIsGenerating();

  const resultContent = toolResult ? extractTextContent(toolResult.content) : null;
  const hasError = !!toolResult?.error;

  const handleResampleTarget = () => {
    if (!toolResult) return;
    resampleTargetResponse(toolResult.id, toolCall.id);
  };

  const sharedState: SharedToolState = {
    toolCall,
    toolResult,
    resultContent,
    hasError,
    isGenerating,
    toolCallBranchPoint,
    targetResponseBranchPoint,
    editToolCall: editToolCallFn,
    rewriteToolCall: rewriteToolCallFn,
    clearRewriteDraft: clearRewriteDraftFn,
    handleResampleTarget,
  };

  switch (toolCall.function) {
    case "query_target":
      return <QueryTargetRenderer state={sharedState} />;
    case "create_tool_for_target":
      return <CreateToolRenderer state={sharedState} />;
    default:
      return <CompactToolRenderer state={sharedState} />;
  }
}
