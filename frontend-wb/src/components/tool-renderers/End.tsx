import type { JSX } from "react";

import type { ToolRendererProps } from "./index";

export function End(_: ToolRendererProps): JSX.Element {
  return <div className="tr-line">ended audit</div>;
}
