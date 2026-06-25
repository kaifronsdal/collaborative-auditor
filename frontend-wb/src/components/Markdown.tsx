import { marked } from "marked";
import { memo, useMemo, type JSX } from "react";

// Single shared parser config — gfm tables/strikethrough, but keep single
// newlines as <br> so chat-style line breaks survive.
marked.setOptions({ gfm: true, breaks: true });

// marked passes raw HTML through, so XML-ish tags in transcripts (e.g. the
// auditor seed's `<seed_instructions>…</seed_instructions>`) silently vanish.
// We never want raw HTML from model output anyway, so escape all angle
// brackets up front — markdown's own syntax (`#`, `**`, `` ` ``, `-`) doesn't
// use them, so headings/lists/code still render.
const escapeAngles = (s: string): string =>
  s.replace(/</g, "&lt;").replace(/>/g, "&gt;");

type Props = { children: string };

function MarkdownImpl({ children }: Props): JSX.Element {
  const html = useMemo(
    () => marked.parse(escapeAngles(children), { async: false }),
    [children],
  );
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

export const Markdown = memo(MarkdownImpl);
