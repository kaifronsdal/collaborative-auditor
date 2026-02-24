import { useRef, useEffect } from "react";
import { useCurrentBranch, useIsGenerating } from "@/store/session";
import { MessageCard } from "@/components/MessageCard";

/**
 * Placeholder shown while the auditor is thinking (before its response appears).
 * Target generating is handled inside ToolCallCard with its own green placeholder.
 */
function GeneratingPlaceholder() {
  return (
    <div className="flex items-start gap-3 py-4 pl-4">
      <div className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]">
        <span className="font-semibold text-xs tracking-wide uppercase text-[var(--foreground)]">Auditor</span>
        <span className="inline-flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" />
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.15s" }} />
          <span className="w-1.5 h-1.5 rounded-full bg-[var(--muted-foreground)] animate-pulse" style={{ animationDelay: "0.3s" }} />
        </span>
        <span className="text-xs">thinking...</span>
      </div>
    </div>
  );
}

export function Timeline() {
  const currentBranch = useCurrentBranch();
  const isGenerating = useIsGenerating();
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevMessageCountRef = useRef(0);

  // Auto-scroll to bottom only when new messages arrive during generation,
  // NOT when switching branches (which changes the message list entirely).
  const messageCount = currentBranch?.auditor_messages.length ?? 0;
  useEffect(() => {
    const prevCount = prevMessageCountRef.current;
    prevMessageCountRef.current = messageCount;

    // Scroll when messages are added (count increased) while generating,
    // or when generation just started (isGenerating flipped on).
    // Skip when the count changed due to a branch switch (not generating).
    if (isGenerating && messageCount > prevCount) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messageCount, isGenerating]);

  if (!currentBranch) {
    return null;
  }

  // Filter out tool messages - they are rendered inline by ToolCallCard
  const visibleMessages = currentBranch.auditor_messages.filter(
    (m) => m.role !== "tool"
  );

  // Show "Auditor generating" placeholder only when waiting for a NEW auditor turn.
  // The last VISIBLE message (excluding tool results) tells us the state:
  // - Last visible is "user" or "system" → auditor hasn't responded yet → show placeholder
  // - Last visible is "assistant" → auditor responded, tools may be executing → no placeholder
  //   (ToolCallCard shows its own "Target responding..." for query_target)
  const lastVisible = visibleMessages[visibleMessages.length - 1];
  const showGeneratingPlaceholder = isGenerating && lastVisible?.role !== "assistant";

  return (
    <div className="max-w-4xl mx-auto px-4 py-3 space-y-2">
      {visibleMessages.map((message, index) => (
        <MessageCard
          key={message.id}
          message={message}
          index={index}
        />
      ))}
      {showGeneratingPlaceholder && <GeneratingPlaceholder />}
      <div ref={bottomRef} />
    </div>
  );
}
