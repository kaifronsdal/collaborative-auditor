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

export function ToolEventRow({ ev }: Props): JSX.Element {
  const [open, setOpen] = useState(false);
  const resultPreview = ev.error ? ev.error.message : resultText(ev.result);
  return (
    <div className="tool-row">
      <div className="tool-head" onClick={() => setOpen((o) => !o)}>
        <span className="glyph">⎿</span>
        <span className="fn">{ev.function}</span>
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
