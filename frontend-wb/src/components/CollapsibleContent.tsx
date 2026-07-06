import { type JSX, type ReactNode, useId } from "react";

import { ExpandablePanel } from "@tsmono/react/components";

/**
 * Thin adapter over `@tsmono/react` `ExpandablePanel` (M1-CLEANUP N4): maps our
 * pixel `maxHeight` to its `lines` (rem-based, root font-size = 16px) and
 * supplies a per-instance `useId` so each bubble's collapsed state is tracked
 * independently in `componentStateHooks` (in-memory — reload-stability of the
 * id is irrelevant).
 */
export function CollapsibleContent({
  children,
  maxHeight = 280,
}: { children: ReactNode; maxHeight?: number }): JSX.Element {
  const id = useId();
  return (
    <ExpandablePanel id={id} collapse lines={maxHeight / 16}>
      {children}
    </ExpandablePanel>
  );
}
