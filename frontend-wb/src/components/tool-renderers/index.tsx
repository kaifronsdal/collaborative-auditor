/**
 * Per-tool body renderers. `renderTool` returns the JSX to fill `.tp-body`
 * for a known auditor tool, or `null` so `ToolPair` falls back to generic
 * args/result JSON.
 *
 * Props are source-agnostic — they cover both real `ToolEvent`s (auditor) and
 * `(ToolCall, ChatMessageTool)` pairs from inspect's `resolveMessages`
 * (target), so `result` is loosely typed and renderers stringify defensively.
 */
import type { ReactNode } from "react";

import { ToolCallView, type ToolCallViewProps } from "@tsmono/inspect-components/chat";
import { resolveToolInput } from "@tsmono/inspect-components/chat/tools";
import { getDefaultCustomToolView } from "@tsmono/inspect-components/chat/tools/custom";

import { CreateTool } from "./CreateTool";
import { End } from "./End";
import { RemoveTool } from "./RemoveTool";
import { Restart } from "./Restart";
import { Resume } from "./Resume";
import { Rollback } from "./Rollback";
import { SendMessage } from "./SendMessage";
import { SendToolCallResult } from "./SendToolCallResult";
import { SetSystemMessage } from "./SetSystemMessage";

export type ToolRendererProps = {
  fn: string;
  args: Record<string, unknown>;
  result?: unknown;
  error?: { message: string } | null;
};

export function renderTool(p: ToolRendererProps): ReactNode | null {
  switch (p.fn) {
    case "send_message":          return <SendMessage {...p} />;
    case "set_system_message":    return <SetSystemMessage {...p} />;
    case "create_tool":           return <CreateTool {...p} />;
    case "send_tool_call_result": return <SendToolCallResult {...p} />;
    case "resume":                return <Resume {...p} />;
    case "rollback_conversation": return <Rollback {...p} />;
    case "restart_conversation":  return <Restart {...p} />;
    case "end_conversation":      return <End {...p} />;
    case "remove_tool":           return <RemoveTool {...p} />;
  }

  // Not a petri auditor tool — render via inspect-view's `ToolCallView`
  // (syntax-highlighted bash/python/web_search/etc. via `resolveToolInput`,
  // plus answer/submit/tool_search special-cases). This is the same component
  // `ChatMessageRow` uses for tool calls, so target-side tools look identical
  // to inspect-view.
  return (
    <div className="tr-inspect">
      <ToolCallView {...toToolCallViewProps(p)} getCustomToolView={getDefaultCustomToolView} />
    </div>
  );
}

/** Adapt our source-agnostic props to inspect's `ToolCallViewProps`. */
function toToolCallViewProps(p: ToolRendererProps): ToolCallViewProps {
  const { functionCall, input, description, contentType } = resolveToolInput(
    p.fn,
    p.args,
  );
  return {
    id: `tr-${p.fn}`,
    tool: p.fn,
    functionCall,
    input: input ?? p.args,
    description,
    contentType,
    output: toOutput(p.result),
  };
}

/**
 * Coerce our loosely-typed `result` into inspect's tool-output union.
 * `ToolEvent.result` already conforms (string / Content[]); `fromCall`
 * flattens to a string; anything unexpected gets stringified.
 */
function toOutput(result: unknown): ToolCallViewProps["output"] {
  if (result == null) return "";
  if (
    typeof result === "string" ||
    typeof result === "number" ||
    typeof result === "boolean"
  ) {
    return result;
  }
  if (Array.isArray(result)) {
    return result as ToolCallViewProps["output"];
  }
  return JSON.stringify(result);
}
