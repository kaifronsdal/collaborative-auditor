import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { shortId, str } from "./util";

export function Rollback({ args }: ToolRendererProps): JSX.Element {
  return (
    <div className="tr-line">
      rolled back to <code>{shortId(str(args.message_id))}</code>
    </div>
  );
}
