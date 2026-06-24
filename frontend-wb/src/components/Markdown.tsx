import { marked } from "marked";
import { memo, useMemo, type JSX } from "react";

// Single shared parser config — gfm tables/strikethrough, but keep single
// newlines as <br> so chat-style line breaks survive.
marked.setOptions({ gfm: true, breaks: true });

type Props = { children: string };

function MarkdownImpl({ children }: Props): JSX.Element {
  const html = useMemo(
    () => marked.parse(children, { async: false }),
    [children],
  );
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

export const Markdown = memo(MarkdownImpl);
