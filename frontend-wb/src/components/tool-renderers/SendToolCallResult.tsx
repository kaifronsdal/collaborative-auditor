import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { shortId, str } from "./util";

/** Synthetic tool result the auditor injects into the target's transcript. */
export function SendToolCallResult({ args }: ToolRendererProps): JSX.Element {
  const id = str(args.tool_call_id);
  const status = str(args.status) || "ok";
  const result =
    typeof args.result === "string" ? args.result : JSON.stringify(args.result, null, 2);
  return (
    <div className="tp-slot">
      <div className="tp-lbl">
        <span className={`tr-pill tr-pill-${status === "error" ? "err" : "ok"}`}>{status}</span>
        <code className="tr-id">{shortId(id)}</code>
      </div>
      <pre>{result}</pre>
    </div>
  );
}
