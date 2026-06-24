import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { str } from "./util";

/** A synthetic tool the auditor exposes to the target. */
export function CreateTool({ args }: ToolRendererProps): JSX.Element {
  const name = str(args.name);
  const description = str(args.description);
  const params = args.parameters;
  return (
    <div className="tr-create-tool">
      <div>
        <strong>{name}</strong>
      </div>
      <div className="tr-desc">{description}</div>
      {params != null && (
        <details>
          <summary>parameters</summary>
          <pre>{JSON.stringify(params, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
