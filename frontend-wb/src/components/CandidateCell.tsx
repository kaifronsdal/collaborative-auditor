/**
 * Inline Resample-N picker rendered under the anchor row: header
 * `candidates · {done}/{n} done · [compare] [dismiss all]` plus one
 * `CandidateCard` per child. The original is candidate #0 implicitly —
 * it's the row above; `dismiss all` = pick original (RESAMPLE-N.md).
 */
import { type JSX, useState } from "react";

import { isModelEvent } from "../lib/events";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { CandidateCard } from "./CandidateCard";
import { CompareSheet } from "./CompareSheet";

type Props = {
  /** The parent branch being viewed (the row above is its anchor turn). */
  branch: BranchId;
  /** Assistant message id of the row above. */
  anchor: string;
  /** Which column this cell sits in — matches `batch.kind`. */
  kind: Role;
};

/** id of the open batch at `(parent, anchor, kind)` — `picked == null`.
 *  Returns a stable string (or null) so Zustand's `Object.is` short-circuits
 *  on unrelated store updates. */
export function useOpenBatchId(
  branch: BranchId, anchor: string, kind: Role,
): string | null {
  return useSession((s) => {
    for (const [id, b] of Object.entries(s.candidateBatches)) {
      if (b.parent === branch && b.anchor === anchor && b.kind === kind && b.picked == null) {
        return id;
      }
    }
    return null;
  });
}

export function CandidateCell({ branch, anchor, kind }: Props): JSX.Element | null {
  const batchId = useOpenBatchId(branch, anchor, kind);
  const batch = useSession((s) => (batchId != null ? s.candidateBatches[batchId] : null));
  const byRole = useSession((s) => s.byRole);
  const pickCandidate = useSession((s) => s.pickCandidate);
  const dismissCandidates = useSession((s) => s.dismissCandidates);
  const [compare, setCompare] = useState(false);
  // R1 in-flight flag (button-audit #11): once dismiss is sent the button
  // disables until the batch's `picked` flips (unmounts this cell). Prevents
  // a double-click firing two `dismiss_candidates`.
  const [dismissing, setDismissing] = useState(false);

  if (batchId == null || batch == null) return null;

  const done = batch.children.filter((cid) => {
    const ev = byRole[cid]?.[kind]?.find(isModelEvent);
    return ev != null && !ev.pending;
  }).length;

  return (
    <div className="candidate-cell">
      <div className="cand-cell-head">
        <i className="bi bi-collection" />
        <span>
          candidates · {done}/{batch.children.length} done
        </span>
        <button
          type="button"
          onClick={() => setCompare(true)}
          title="compare side-by-side"
        >
          <i className="bi bi-arrows-fullscreen" /> compare
        </button>
        <button
          type="button"
          disabled={dismissing}
          onClick={() => {
            if (dismissing) return;
            setDismissing(true);
            dismissCandidates(batchId);
          }}
          title="keep the original (the row above); cancel all candidates"
        >
          dismiss all
        </button>
      </div>
      {batch.children.map((cid, i) => (
        <CandidateCard
          key={cid}
          cid={cid}
          kind={kind}
          ordinal={i + 1}
          onPick={() => pickCandidate(batchId, cid)}
        />
      ))}
      {compare && (
        <CompareSheet
          batchId={batchId}
          batch={batch}
          onClose={() => setCompare(false)}
        />
      )}
    </div>
  );
}
