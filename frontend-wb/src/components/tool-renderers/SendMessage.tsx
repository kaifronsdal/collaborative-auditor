import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { str } from "./util";

/** Auditor → target user message, shown as a user-styled preview bubble. */
export function SendMessage({ args }: ToolRendererProps): JSX.Element {
  const message = str(args.message);
  return <div className="tr-preview tr-user">{message}</div>;
}
