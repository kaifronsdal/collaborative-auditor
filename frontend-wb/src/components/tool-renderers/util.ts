import type { ChatMessage } from "@tsmono/inspect-common";

/** Cast an arg to string, or `""` if absent / wrong type. */
export const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** Cast an arg to boolean (truthy), defaulting to false. */
export const bool = (v: unknown): boolean => v === true;

/** Last 7 chars of an id, ellipsised. */
export const shortId = (s: string): string => (s.length > 9 ? `…${s.slice(-7)}` : s);

/** Flatten a tool result (ToolEvent.result or ChatMessageTool content) into
 *  plain text for preview. Loosely typed — both sources feed through here. */
export function resultText(result: unknown): string {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  if (Array.isArray(result)) {
    return result
      .map((r) => (r != null && typeof r === "object" && "text" in r ? String(r.text) : `[${(r as { type?: string })?.type ?? "?"}]`))
      .join("\n");
  }
  if (typeof result === "object" && "text" in result) return String((result as { text: unknown }).text);
  return JSON.stringify(result);
}

/** Flatten `ChatMessage.content` (string or block list) to plain text,
 *  dropping non-text blocks (reasoning/image/…). */
export function contentText(content: ChatMessage["content"]): string {
  return typeof content === "string"
    ? content
    : content.map((c) => (c.type === "text" ? c.text : "")).join("");
}
