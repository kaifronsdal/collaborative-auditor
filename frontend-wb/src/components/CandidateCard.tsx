/**
 * One Resample-N candidate: capped 6-line preview of the divergent
 * message, `ShimmerBubble` while pending, grade-caption slot, and a
 * `[pick this]` button (RESAMPLE-N.md).
 *
 * The divergent message is the first `ModelEvent` in `byRole[cid][kind]`:
 * the `_on_event` splice gate drops replayed auditor-role events until the
 * first live one, and replayed target generates emit no `ModelEvent` (the
 * tape serves them), so the first model event in the candidate's own
 * column IS the fresh response past the branch point.
 */
import type { JSX } from "react";

import { isModelEvent } from "../lib/events";
import { useEvents, useIsPending } from "../lib/selectors";
import type { BranchId, Role } from "../lib/wire";
import { useSession } from "../store/session";
import { ShimmerBubble } from "./ShimmerBubble";

type Props = {
  cid: BranchId;
  kind: Role;
  ordinal: number;
  onPick: () => void;
};

export function CandidateCard({ cid, kind, ordinal, onPick }: Props): JSX.Element {
  const events = useEvents(cid, kind);
  const grades = useSession((s) => s.branches[cid]?.grades);
  const picking = useIsPending((c) => c.t === "pick_candidate");

  const ev = events.find(isModelEvent);
  const msg = ev?.output?.choices?.[0]?.message;
  const pending = ev == null || !!ev.pending;

  // Preview = assistant text; if empty (auditor turns are mostly tool-only)
  // fall back to a one-line-per-call tool summary.
  const text =
    typeof msg?.content === "string"
      ? msg.content
      : (msg?.content ?? [])
          .map((b) => ("text" in b ? b.text : ""))
          .filter(Boolean)
          .join("\n");
  const calls = (msg?.tool_calls ?? []).map(
    (tc) => `${tc.function}(${JSON.stringify(tc.arguments)})`
  );
  const preview = text.trim() || calls.join("\n");

  return (
    <div className={`candidate-card${pending ? " pending" : ""}`}>
      <div className="cand-head">
        <span className="cand-ord">#{ordinal}</span>
        {grades && (
          <span className="cand-grades">
            {Object.entries(grades).map(([k, v]) => (
              <span key={k} className="cand-grade" title={k}>
                {k} {v.toFixed(2)}
              </span>
            ))}
          </span>
        )}
        <button type="button" className="cand-pick" onClick={onPick} disabled={pending || picking}>
          pick this
        </button>
      </div>
      {ev == null ? (
        <ShimmerBubble />
      ) : (
        <div className="cand-preview">
          {preview}
          {ev.pending && <span className="cursor" />}
        </div>
      )}
    </div>
  );
}
