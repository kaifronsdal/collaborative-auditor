import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { str } from "./util";

export function RemoveTool({ args }: ToolRendererProps): JSX.Element {
  return (
    <div className="tr-line">
      removed <code>{str(args.tool_name)}</code>
    </div>
  );
}
