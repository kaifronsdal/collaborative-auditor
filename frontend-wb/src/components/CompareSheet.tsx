/**
 * Full-height side-by-side compare for a Resample-N batch: one
 * `SwimlaneColumn` per candidate (RESAMPLE-N.md). Each column's
 * `splice()` reconstructs the parent prefix + the candidate's divergent
 * tail, so the reader sees N full conversations diverging at one turn.
 */
import type { JSX } from "react";
import { Modal } from "@tsmono/react/components/Modal";

import type { CandidateBatch } from "../lib/wire";
import { useIsPending } from "../lib/selectors";
import { useSession } from "../store/session";
import { SwimlaneColumn } from "./SwimlaneColumn";

type Props = {
  batchId: string;
  batch: CandidateBatch;
  onClose: () => void;
};

export function CompareSheet({ batchId, batch, onClose }: Props): JSX.Element {
  const pickCandidate = useSession((s) => s.pickCandidate);
  const picking = useIsPending((c) => c.t === "pick_candidate");
  return (
    <Modal
      show
      onHide={onClose}
      title={`candidates · ${batch.children.length} · ${batch.kind}`}
      width="96vw"
      bodyClassName="compare-sheet"
      padded={false}
    >
      <div
        className="compare-grid"
        style={{ gridTemplateColumns: `repeat(${batch.children.length}, minmax(320px, 1fr))` }}
      >
        {batch.children.map((cid, i) => (
          <div key={cid} className="compare-col">
            <div className="compare-head">
              <span className="cand-ord">#{i + 1}</span>
              <button
                type="button"
                className="cand-pick"
                disabled={picking}
                onClick={() => {
                  pickCandidate(batchId, cid);
                  onClose();
                }}
              >
                pick this
              </button>
            </div>
            <SwimlaneColumn branch={cid} role={batch.kind} />
          </div>
        ))}
      </div>
    </Modal>
  );
}
