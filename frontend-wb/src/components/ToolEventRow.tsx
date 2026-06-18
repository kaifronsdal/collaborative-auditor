import type { ToolEvent } from "@tsmono/inspect-common";

import { type JSX, useState } from "react";

type Props = { ev: ToolEvent };

function resultText(result: ToolEvent["result"]): string {
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") {
    return String(result);
  }
  if (Array.isArray(result)) {
    return result
      .map((r) => (r.type === "text" ? r.text : `[${r.type}]`))
      .join("\n");
  }
  return result.type === "text" ? result.text : `[${result.type}]`;
}

/** First 50 chars of JSON-serialised args, for the collapsed preview. */
function argsPreview(args: ToolEvent["arguments"]): string {
  const raw = JSON.stringify(args);
  return raw.length > 50 ? `${raw.slice(0, 50)}…` : raw;
}

export function ToolEventRow({ ev }: Props): JSX.Element {
  const [open, setOpen] = useState(false);
  const resultPreview = ev.error ? ev.error.message : resultText(ev.result);
  return (
    <div className="tool-row">
      <div className="tool-head" onClick={() => setOpen((o) => !o)}>
        <span className="glyph">⎿</span>
        <span className="fn">{ev.function}</span>
        <span className="args-preview">"{argsPreview(ev.arguments)}"</span>
        {ev.error && <span className="tool-err"> · error</span>}
        <span className="caret">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <>
          <div className="tool-args">{JSON.stringify(ev.arguments, null, 2)}</div>
          <div className="tool-result">{resultPreview}</div>
        </>
      )}
    </div>
  );
}
