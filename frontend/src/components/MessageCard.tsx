import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import type {
  ChatMessage,
  ContentPart,
  ContentReasoning,
} from "@/lib/types";
import { extractTextContent, isContentText, isContentReasoning, isContentImage } from "@/lib/contentUtils";
import { useSessionStore, useTurnBranchPoint } from "@/store/session";
import { BranchNavigation } from "@/components/BranchNavigation";
import { ToolCallCard } from "@/components/ToolCallCard";
import { clsx } from "clsx";

const BODY_TEXT_CLASS = "text-[0.9375rem] leading-[var(--line-height-content)]";
const LABEL_TEXT_CLASS = "text-xs uppercase tracking-wider font-medium";

// =============================================================================
// Content Type Helpers
// =============================================================================

interface ReasoningBlockProps {
  reasoning: ContentReasoning;
  defaultCollapsed?: boolean;
}

function ReasoningBlock({ reasoning, defaultCollapsed = true }: ReasoningBlockProps) {
  const PREVIEW_CHARS = 420;
  const [isCollapsed, setIsCollapsed] = useState(defaultCollapsed);

  const label = reasoning.redacted
    ? (reasoning.summary ? "Thinking summary" : "Thinking redacted")
    : "Thinking";
  const text = reasoning.redacted
    ? (reasoning.summary?.trim() ?? "")
    : reasoning.reasoning.trim();

  // Don't render content when fully redacted with no summary
  if (!text) {
    return (
      <div className="my-2 text-xs text-[var(--muted-foreground)] italic">
        [{label}]
      </div>
    );
  }

  const isLong = text.length > PREVIEW_CHARS;
  const shownText = isCollapsed && isLong
    ? `${text.slice(0, PREVIEW_CHARS).trimEnd()}...`
    : text;

  return (
    <div className="my-2 rounded-md bg-[var(--muted)]/40 px-3 py-2">
      <div className={clsx(LABEL_TEXT_CLASS, "text-[var(--muted-foreground)] mb-1")}>
        {label}
      </div>
      <div className={clsx("whitespace-pre-wrap italic text-[var(--muted-foreground)] opacity-80", BODY_TEXT_CLASS)}>
        {shownText}
      </div>
      {isLong && (
        <div className="mt-1">
          <button
            type="button"
            className="text-xs text-[var(--muted-foreground)] hover:text-[var(--foreground)] transition-colors"
            onClick={() => setIsCollapsed(!isCollapsed)}
            aria-expanded={!isCollapsed}
          >
            {isCollapsed ? "more..." : "less..."}
          </button>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Content Placeholder Component
// =============================================================================

function ContentPlaceholder({ type }: { type: string }) {
  return (
    <div className={`content-${type.toLowerCase()}`}>
      <span className="text-xs text-[var(--muted-foreground)] bg-[var(--muted)] px-2 py-1 rounded-md">
        [{type}]
      </span>
    </div>
  );
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

function useAutosizeTextarea(
  textareaRef: React.RefObject<HTMLTextAreaElement | null>,
  value: string,
  enabled: boolean
) {
  useLayoutEffect(() => {
    if (!enabled || !textareaRef.current) return;
    const el = textareaRef.current;
    el.style.height = "0px";
    el.style.height = `${el.scrollHeight}px`;
  }, [textareaRef, value, enabled]);
}

function isInteractiveTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return !!target.closest("button, input, textarea, a, [role='button']");
}

// =============================================================================
// Content Renderer Component
// =============================================================================

interface ContentRendererProps {
  content: string | ContentPart[];
}

function ContentRenderer({ content }: ContentRendererProps) {
  // Handle string content
  if (typeof content === "string") {
    return <div className={clsx("whitespace-pre-wrap", BODY_TEXT_CLASS)}>{content}</div>;
  }

  // Handle array of content parts
  if (!content || content.length === 0) {
    return null;
  }

  return (
    <div className="content-parts space-y-2">
      {content.map((part, index) => {
        // Text content
        if (isContentText(part)) {
          return (
            <div key={index} className={clsx("whitespace-pre-wrap", BODY_TEXT_CLASS)}>
              {part.text}
            </div>
          );
        }

        // Reasoning content
        if (isContentReasoning(part)) {
          return <ReasoningBlock key={index} reasoning={part} />;
        }

        // Image / Audio / Video / Document / Data / Tool Use content
        if (isContentImage(part) || part.type === "audio" || part.type === "video" || part.type === "document" || part.type === "data" || part.type === "tool_use") {
          return <ContentPlaceholder key={index} type={part.type.charAt(0).toUpperCase() + part.type.slice(1)} />;
        }

        // This should be unreachable — all ContentPart types are handled above
        console.error("Unhandled content type:", (part as ContentPart).type);
        return <ContentPlaceholder key={index} type={(part as ContentPart).type} />;
      })}
    </div>
  );
}

// =============================================================================
// Role label colors
// =============================================================================

const roleLabelColors: Record<string, string> = {
  researcher: "text-blue-600 dark:text-blue-400",
  auditor: "text-[var(--foreground)]",
  target: "text-emerald-700 dark:text-emerald-400",
  system: "text-[var(--muted-foreground)]",
};

// =============================================================================
// MessageCard Component
// =============================================================================

interface MessageCardProps {
  message: ChatMessage;
  index: number;
}

function MessageCardInner({ message, index }: MessageCardProps) {
  const [isEditingContent, setIsEditingContent] = useState(false);
  const [editedContent, setEditedContent] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const editTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const isCollapsed = useSessionStore((state) => state.collapsedMessages.has(message.id));
  const toggleMessageCollapsed = useSessionStore((state) => state.toggleMessageCollapsed);
  const branchAtMessage = useSessionStore((state) => state.branchAtMessage);
  const resampleTurn = useSessionStore((state) => state.resampleTurn);
  const send = useSessionStore((state) => state.send);

  // Fine-grained selector: only re-renders when THIS message's branch point changes
  const branchPoint = useTurnBranchPoint(message.id);

  // Extract turn_id from message metadata for resampling
  const turnId = message.metadata?.turn_id;

  const source = message.metadata?.source;
  if (!source && message.role !== "system") {
    console.warn(`Message ${message.id} (role=${message.role}) has no metadata.source — attributing as auditor`);
  }
  const isEdited = message.metadata?.edited;

  const messageType: "researcher" | "auditor" | "target" | "system" =
    source === "Researcher" ? "researcher"
    : source === "Target" ? "target"
    : (source === "System" || message.role === "system") ? "system"
    : "auditor";
  const isResearcher = messageType === "researcher";
  const isAuditor = messageType === "auditor";
  const isTarget = messageType === "target";
  const isSystem = messageType === "system";
  const isInitialInstruction = index === 1 && message.role === "user" && source === "System";
  const isEditableMessage = isInitialInstruction || isResearcher;
  const editableOriginalContent = typeof message.content === "string"
    ? message.content
    : extractTextContent(message.content);
  const isDirty = isEditingContent && editedContent !== editableOriginalContent;
  const queryTargetCalls = (message.tool_calls || []).filter((tc) => tc.function === "query_target");
  const nonQueryToolCalls = (message.tool_calls || []).filter((tc) => tc.function !== "query_target");

  // Extract text content for preview when collapsed
  const textContent = extractTextContent(message.content);

  const handleBranch = () => {
    branchAtMessage(message.id);
  };

  const handleResample = () => {
    if (!turnId) {
      console.error(`Cannot resample: message ${message.id} has no turn_id`);
      return;
    }
    resampleTurn(turnId);
  };

  const openEditor = () => {
    setEditedContent(editableOriginalContent);
    setIsEditingContent(true);
  };

  useAutosizeTextarea(editTextareaRef, editedContent, isEditingContent);

  useEffect(() => {
    if (!isEditingContent) return;

    const onMouseDown = (event: MouseEvent) => {
      if (!rootRef.current) return;
      if (!rootRef.current.contains(event.target as Node) && !isDirty) {
        setIsEditingContent(false);
      }
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isDirty) {
        setIsEditingContent(false);
      }
    };

    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isEditingContent, isDirty]);

  if (isSystem && index === 0) return null;

  return (
    <div
      ref={rootRef}
      className="relative group"
      onDoubleClick={(e) => {
        if (isEditableMessage && !isEditingContent && !isInteractiveTarget(e.target)) {
          openEditor();
        }
      }}
    >
      <div
        className={clsx(
          "rounded-lg px-3 py-1.5 transition-colors duration-200",
          isResearcher && "bg-[var(--researcher-bg)] ml-0 mr-auto max-w-[85%]",
          isAuditor && "w-full",
          isTarget && "bg-[var(--target-bg)] ml-auto mr-0 max-w-[85%]",
          isSystem && clsx("text-[var(--muted-foreground)] italic", BODY_TEXT_CLASS)
        )}
      >
        <div
          className={clsx(
            "flex items-center justify-between mb-0.5",
            isAuditor && "cursor-pointer"
          )}
          onClick={() => {
            if (isAuditor && !isEditingContent) {
              toggleMessageCollapsed(message.id);
            }
          }}
        >
          <div className="flex items-center gap-1.5">
            {/* Collapse chevron for auditor messages — always visible */}
            {isAuditor && (
              <span
                className="text-[var(--muted-foreground)] inline-flex h-4 w-4 shrink-0 items-center justify-center hover:text-[var(--foreground)]"
                aria-label={isCollapsed ? "Expand message" : "Collapse message"}
                aria-expanded={!isCollapsed}
              >
                <CollapseChevron expanded={!isCollapsed} />
              </span>
            )}
            <span className={clsx(
              "font-semibold tracking-wide uppercase text-xs",
              roleLabelColors[messageType]
            )}>
              {isResearcher && "Researcher"}
              {isAuditor && "Auditor"}
              {isTarget && "Target"}
              {isSystem && "System"}
            </span>
            {isEdited && (
              <span className="text-xs text-[var(--muted-foreground)] opacity-70">(edited)</span>
            )}
            {/* Inline branch navigation for messages with branch points */}
            {branchPoint && (
              <BranchNavigation branchPoint={branchPoint} />
            )}
          </div>

          {/* Action buttons — appear on hover */}
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-200">
            {isEditableMessage && !isEditingContent && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  openEditor();
                }}
                className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md transition-colors duration-150"
                title={isInitialInstruction ? "Edit initial instructions" : "Edit message"}
              >
                ✎
              </button>
            )}
            {/* Resample button for auditor messages */}
            {isAuditor && turnId && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleResample();
                }}
                className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md transition-colors duration-150"
                title="Regenerate this response"
              >
                ↻
              </button>
            )}
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleBranch();
              }}
              className="text-xs px-1.5 py-0.5 text-[var(--muted-foreground)] hover:text-[var(--foreground)] hover:bg-[var(--muted)] rounded-md transition-colors duration-150"
              title="Branch from here"
            >
              Branch
            </button>
          </div>
        </div>

        {(!isCollapsed || !isAuditor) && (
          <>
            {isEditingContent ? (
              <div className="space-y-2">
                <textarea
                  ref={editTextareaRef}
                  value={editedContent}
                  onChange={(e) => setEditedContent(e.target.value)}
                  className={clsx(
                    "w-full bg-[var(--muted)] p-2 rounded-lg border border-0.5 border-[var(--border)] focus:outline-none focus:border-[var(--primary)] transition-colors duration-150 resize-none overflow-hidden",
                    BODY_TEXT_CLASS
                  )}
                  rows={1}
                  autoFocus
                />
                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      if (isInitialInstruction) {
                        send({ type: "edit_initial_prompt", new_content: editedContent });
                      } else {
                        send({ type: "edit_message", message_id: message.id, new_content: editedContent });
                      }
                      setIsEditingContent(false);
                    }}
                    className="text-xs px-3 py-1.5 bg-[var(--primary)] text-[var(--primary-foreground)] rounded-lg hover:opacity-90 transition-opacity duration-150"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setIsEditingContent(false)}
                    className="text-xs px-3 py-1.5 bg-[var(--muted)] text-[var(--muted-foreground)] rounded-lg hover:bg-[var(--border)] transition-colors duration-150"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <ContentRenderer content={message.content} />
            )}
            {message.tool_calls && message.tool_calls.length > 0 && (
              <div className="mt-2 space-y-2">
                {message.tool_calls.map((toolCall) => (
                  <ToolCallCard key={toolCall.id} toolCall={toolCall} />
                ))}
              </div>
            )}
          </>
        )}

        {/* Keep target responses visually independent from auditor collapse */}
        {isCollapsed && isAuditor && queryTargetCalls.length > 0 && (
          <div className="mt-2 space-y-2">
            {queryTargetCalls.map((toolCall) => (
              <ToolCallCard key={toolCall.id} toolCall={toolCall} />
            ))}
          </div>
        )}

        {isCollapsed && isAuditor && (nonQueryToolCalls.length > 0 || !!textContent) && (
          <div className={clsx("text-[var(--muted-foreground)] italic", BODY_TEXT_CLASS)}>
            {nonQueryToolCalls.length > 0
              ? "[" + nonQueryToolCalls.length + " tool calls collapsed]"
              : textContent
                ? `[${textContent.slice(0, 50)}${textContent.length > 50 ? "..." : ""}]`
                : ""}
          </div>
        )}
      </div>
    </div>
  );
}

export const MessageCard = React.memo(MessageCardInner, (prev, next) => {
  return prev.message === next.message && prev.index === next.index;
});
