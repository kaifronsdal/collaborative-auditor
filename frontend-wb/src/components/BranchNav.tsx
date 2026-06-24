/**
 * Inline `‹ idx/total ›` chip for stepping between sibling branches at a
 * fork point. Same UX as the legacy collaborative-auditor `BranchNavigation`
 * (ea1a5be), but self-contained: the caller computes `idx`/`total` and owns
 * what "switch" means (swimlane row for the target column, workbench
 * `Branch` for the auditor column).
 *
 * inspect-view ships a related widget — `transcript/BranchPoint.tsx` — but
 * that's a full-width segmented control labelled with branch names, not a
 * compact pager; we want the small chip here so it sits in the per-turn
 * action strip.
 */
import type { JSX, MouseEvent } from "react";

export type BranchNavProps = {
  /** 0-based position of the currently-viewed sibling. */
  idx: number;
  /** Total siblings at this fork (including the one being viewed). */
  total: number;
  onPrev: () => void;
  onNext: () => void;
};

export function BranchNav({ idx, total, onPrev, onNext }: BranchNavProps): JSX.Element | null {
  if (total <= 1) return null;

  const stop = (fn: () => void) => (e: MouseEvent) => {
    e.stopPropagation();
    fn();
  };

  return (
    <div
      className="branch-nav"
      role="group"
      aria-label="Branch point"
      onClick={(e) => e.stopPropagation()}
    >
      <button
        type="button"
        className="branch-nav-btn"
        onClick={stop(onPrev)}
        disabled={idx <= 0}
        aria-label="Previous branch"
        title="Previous branch"
      >
        <i className="bi bi-chevron-left" />
      </button>
      <span className="branch-nav-pos">
        {idx + 1}/{total}
      </span>
      <button
        type="button"
        className="branch-nav-btn"
        onClick={stop(onNext)}
        disabled={idx >= total - 1}
        aria-label="Next branch"
        title="Next branch"
      >
        <i className="bi bi-chevron-right" />
      </button>
    </div>
  );
}
