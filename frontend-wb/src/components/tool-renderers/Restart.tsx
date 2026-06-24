import type { JSX } from "react";

import type { ToolRendererProps } from "./index";
import { bool } from "./util";

export function Restart({ args }: ToolRendererProps): JSX.Element {
  const kept = bool(args.keep_tools);
  return <div className="tr-line">restarted{kept ? " (kept tools)" : ""}</div>;
}
