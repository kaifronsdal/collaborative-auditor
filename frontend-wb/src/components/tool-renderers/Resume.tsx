import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { resultText, str } from "./util";

/**
 * Lets the target continue generating. The full reply already appears in the
 * target column, so here we only show the prefill (if any) plus a muted
 * one-line preview of what came back.
 */
export function Resume({ args, result }: ToolRendererProps): JSX.Element {
  const prefill = str(args.prefill);
  const text = resultText(result).trim();
  const preview = text ? text.slice(0, 120) + (text.length > 120 ? "…" : "") : "";
  return (
    <div className="tr-resume">
      {prefill && <div className="tr-prefill"><em>{prefill}</em></div>}
      <div className="tr-muted">{preview || "target generating…"}</div>
    </div>
  );
}
