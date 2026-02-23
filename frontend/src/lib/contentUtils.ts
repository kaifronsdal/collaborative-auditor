import type { ContentPart, ContentText, ContentReasoning, ContentImage } from "./types";

/** Extract plain text from message content (string or ContentPart array) */
export function extractTextContent(content: string | ContentPart[]): string {
  if (typeof content === "string") return content;
  // content is ContentPart[] here by type narrowing
  return content
    .filter((p): p is ContentText => p.type === "text")
    .map((p) => p.text)
    .join("\n");
}

/** Extract target response text from <target_response> XML tags */
export function extractTargetResponse(content: string): string | null {
  const m = content.match(/<target_response[^>]*>([\s\S]*?)<\/target_response>/);
  return m ? m[1].trim() : null;
}

/** Extract target tool calls from <target_tool_calls> XML tags */
export function extractTargetToolCalls(content: string): string | null {
  const m = content.match(/<target_tool_calls>([\s\S]*?)<\/target_tool_calls>/);
  return m ? m[1].trim() : null;
}

/** Segment types for parsed target response content */
export type TargetContentSegment =
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | { type: "thinking_summary"; text: string }
  | { type: "thinking_redacted" };

/**
 * Parse target response content into segments of text and thinking blocks.
 * The backend formats reasoning as <thinking>...</thinking>,
 * <thinking_summary>...</thinking_summary>, or <thinking_redacted/> XML tags.
 */
export function parseTargetContent(content: string): TargetContentSegment[] {
  const segments: TargetContentSegment[] = [];
  // Match <thinking>...</thinking>, <thinking_summary>...</thinking_summary>, <thinking_redacted/>
  const tagPattern = /<thinking>([\s\S]*?)<\/thinking>|<thinking_summary>([\s\S]*?)<\/thinking_summary>|<thinking_redacted\s*\/>/g;

  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tagPattern.exec(content)) !== null) {
    // Text before this tag
    if (match.index > lastIndex) {
      const text = content.slice(lastIndex, match.index).trim();
      if (text) segments.push({ type: "text", text });
    }

    if (match[1] !== undefined) {
      // <thinking>...</thinking>
      segments.push({ type: "thinking", text: match[1].trim() });
    } else if (match[2] !== undefined) {
      // <thinking_summary>...</thinking_summary>
      segments.push({ type: "thinking_summary", text: match[2].trim() });
    } else {
      // <thinking_redacted/>
      segments.push({ type: "thinking_redacted" });
    }
    lastIndex = match.index + match[0].length;
  }

  // Remaining text after last tag
  if (lastIndex < content.length) {
    const text = content.slice(lastIndex).trim();
    if (text) segments.push({ type: "text", text });
  }

  return segments;
}

/** Parsed tool call from <tool_call> XML */
export interface ParsedToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

/**
 * Parse structured tool calls from the <target_tool_calls> content.
 * The backend now formats each tool call as:
 *   <tool_call id="..." name="...">{ JSON args }</tool_call>
 *
 * Falls back to returning null if the format doesn't match (e.g. old-format data).
 */
export function parseTargetToolCalls(content: string): ParsedToolCall[] | null {
  const pattern = /<tool_call\s+id="([^"]+)"\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/tool_call>/g;
  const results: ParsedToolCall[] = [];
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) !== null) {
    let args: Record<string, unknown> = {};
    try {
      args = JSON.parse(match[3].trim());
    } catch {
      // If JSON parsing fails, store as raw string
      args = { _raw: match[3].trim() };
    }
    results.push({ id: match[1], name: match[2], arguments: args });
  }
  return results.length > 0 ? results : null;
}

/** Type guard: is this a text content part? */
export function isContentText(part: ContentPart): part is ContentText {
  return part.type === "text";
}

/** Type guard: is this a reasoning content part? */
export function isContentReasoning(part: ContentPart): part is ContentReasoning {
  return part.type === "reasoning";
}

/** Type guard: is this an image content part? */
export function isContentImage(part: ContentPart): part is ContentImage {
  return part.type === "image";
}
