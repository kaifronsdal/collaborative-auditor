import type { JSX } from "react";

import { CollapsibleContent } from "../CollapsibleContent";
import type { ToolRendererProps } from "./index";
import { str } from "./util";

/** New system prompt for the target — mono, collapsible with sticky toggle. */
export function SetSystemMessage({ args }: ToolRendererProps): JSX.Element {
  const sys = str(args.system_message);
  return (
    <CollapsibleContent maxHeight={200}>
      <pre className="tr-preview tr-system">{sys}</pre>
    </CollapsibleContent>
  );
}
