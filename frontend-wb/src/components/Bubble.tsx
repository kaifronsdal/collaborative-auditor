import type { ChatMessage, Content } from "@tsmono/inspect-common";

import type { JSX } from "react";

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
      return <span key={i}>{block.text}</span>;
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
  if (typeof content === "string") return content;
  return content.map(renderBlock);
}

export function Bubble({ msg, role, ghost, byline, children }: Props): JSX.Element {
  const effectiveRole = role ?? msg?.role ?? "user";
  const cls = `bubble ${effectiveRole}${ghost ? " ghost" : ""}`;
  return (
    <div className="bubble-wrap">
      <div className={cls}>{children ?? (msg ? renderContent(msg.content) : null)}</div>
      {byline && <div className="bubble-by">{byline}</div>}
    </div>
  );
}
