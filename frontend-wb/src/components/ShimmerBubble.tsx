import type { JSX } from "react";

/**
 * Shimmer placeholder — shown at the tail of a column when we know a generate
 * is about to arrive but the first streaming event hasn't landed yet.
 *
 * Two scenarios (Column.tsx):
 *  1. status === "running" and the last event in this role is not pending
 *     (generate started but first partial hasn't arrived).
 *  2. status === "paused" and the column is empty (just-started audit skeleton).
 */
export function ShimmerBubble(): JSX.Element {
  return <div className="shimmer-bubble" aria-hidden="true" />;
}
