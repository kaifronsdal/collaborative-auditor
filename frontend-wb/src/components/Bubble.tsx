import type { ChatMessage, Content } from "@tsmono/inspect-common";

import type { JSX } from "react";

import { CollapsibleContent } from "./CollapsibleContent";
import { Markdown } from "./Markdown";

const COLLAPSIBLE_ROLES: ReadonlySet<ChatMessage["role"]> = new Set(["system", "user", "tool"]);

type Props = {
  msg?: ChatMessage;
  /** Override role styling (e.g. force "assistant" for a streaming output). */
  role?: ChatMessage["role"];
  ghost?: boolean;
  byline?: string;
  children?: React.ReactNode;
};

/** Render one Content block. */
function renderBlock(block: Content, i: number): JSX.Element | null {
  switch (block.type) {
    case "text":
      return <Markdown key={i}>{block.text}</Markdown>;
    case "reasoning":
      return (
        <span key={i} className="reasoning">
          {block.reasoning}
        </span>
      );
    case "tool_use":
      return (
        <span key={i} className="reasoning">
          [tool_use {block.name}]
        </span>
      );
    default:
      return (
        <span key={i} className="reasoning">
          [{block.type}]
        </span>
      );
  }
}

export function renderContent(content: ChatMessage["content"]): React.ReactNode {
  if (typeof content === "string") return <Markdown>{content}</Markdown>;
  return content.map(renderBlock);
}

export function Bubble({ msg, role, ghost, byline, children }: Props): JSX.Element {
  const effectiveRole = role ?? msg?.role ?? "user";
  const cls = `bubble ${effectiveRole}${ghost ? " ghost" : ""}`;
  const body = children ?? (msg ? renderContent(msg.content) : null);
  // Assistant output streams and is the thing being read — never clip it.
  const inner = COLLAPSIBLE_ROLES.has(effectiveRole) ? (
    <CollapsibleContent>{body}</CollapsibleContent>
  ) : (
    body
  );
  return (
    <div className="bubble-wrap">
      <div className={cls}>{inner}</div>
      {byline && <div className="bubble-by">{byline}</div>}
    </div>
  );
}
