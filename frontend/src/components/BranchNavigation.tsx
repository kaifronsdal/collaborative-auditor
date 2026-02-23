"use client";

import { useSessionStore } from "@/store/session";
import type { ComputedBranchPoint } from "@/lib/types";
import type { MouseEvent } from "react";

interface BranchNavigationProps {
  branchPoint: ComputedBranchPoint;
}

export function BranchNavigation({ branchPoint }: BranchNavigationProps) {
  const switchBranch = useSessionStore((state) => state.switchBranch);

  const { current_index, branch_ids } = branchPoint;
  const totalBranches = branch_ids.length;

  // Don't render when there's only one branch
  if (totalBranches <= 1) {
    return null;
  }

  const handlePrevious = (e: MouseEvent) => {
    e.stopPropagation(); // Prevent parent click handlers from firing
    if (current_index > 0) {
      switchBranch(branch_ids[current_index - 1]);
    }
  };

  const handleNext = (e: MouseEvent) => {
    e.stopPropagation(); // Prevent parent click handlers from firing
    if (current_index < totalBranches - 1) {
      switchBranch(branch_ids[current_index + 1]);
    }
  };

  // Prevent clicks on the container from bubbling up
  const handleContainerClick = (e: MouseEvent) => {
    e.stopPropagation();
  };

  return (
    <div
      className="flex items-center gap-1 text-xs bg-[var(--muted)] rounded-lg px-1.5 py-0.5 border border-0.5 border-[var(--border)]"
      onClick={handleContainerClick}
    >
      <button
        onClick={handlePrevious}
        disabled={current_index === 0}
        className="px-1 hover:bg-[var(--border)] rounded-md disabled:opacity-30 disabled:cursor-not-allowed transition-colors duration-150"
        title="Previous branch"
        aria-label="Previous branch"
      >
        &lt;
      </button>
      <span className="px-0.5 tabular-nums text-[var(--muted-foreground)]">
        {current_index + 1} / {totalBranches}
      </span>
      <button
        onClick={handleNext}
        disabled={current_index === totalBranches - 1}
        className="px-1 hover:bg-[var(--border)] rounded-md disabled:opacity-30 disabled:cursor-not-allowed transition-colors duration-150"
        title="Next branch"
        aria-label="Next branch"
      >
        &gt;
      </button>
    </div>
  );
}
