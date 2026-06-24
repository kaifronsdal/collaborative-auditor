import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { str } from "./util";

/** New system prompt for the target — mono, scrollable at 200px. */
export function SetSystemMessage({ args }: ToolRendererProps): JSX.Element {
  const sys = str(args.system_message);
  return <pre className="tr-preview tr-system">{sys}</pre>;
}
