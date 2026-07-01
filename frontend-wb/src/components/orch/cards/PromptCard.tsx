/**
 * `wb.ask_human` gate card. Payload from `Prompt._repr_mimebundle_`
 * (workbench/m1/kernel.py): `{kind, id, question, options, answer, pending}`.
 *
 * While `pending`, renders `.out.gated` with option buttons + a free-text
 * "type your own" row (scenario-d.html turn 2). Any button click resolves
 * the kernel Future via `{t:"approve", display_id, verdict: <option str>}`;
 * the backend then `dh.update()`s with `pending=false, answer=…` and this
 * component collapses to the answered state.
 */
import { useState, type JSX } from "react";
import type { Up } from "../../../lib/wire";

export type PromptPayload = {
  kind: "prompt";
  id: string;
  question: string;
  options: string[] | null;
  answer: string | null;
  pending: boolean;
};

type Props = {
  payload: PromptPayload;
  displayId: string;
  send: (msg: Up) => void;
};

export default function PromptCard({ payload, displayId, send }: Props): JSX.Element {
  const [own, setOwn] = useState("");
  // Local echo of the click so the button highlights before the round-trip.
  const [chosen, setChosen] = useState<string | null>(null);

  const answer = (verdict: string): void => {
    if (!payload.pending) return;
    setChosen(verdict);
    send({ t: "approve", display_id: displayId, verdict });
  };

  if (!payload.pending) {
    // Collapsed: question + the answer line (`.ask-answered`).
    return (
      <div className="out answered">
        <div className="ask-q">{payload.question}</div>
        <div className="ask-answered">
          answered <b>{payload.answer}</b>
        </div>
      </div>
    );
  }

  return (
    <div className="out gated">
      <div className="out-head">
        <i className="bi bi-person-raised-hand" style={{ color: "var(--gate-text)" }} />
        <span className="out-kind">ask_human</span>
      </div>
      <div className="ask-q">{payload.question}</div>
      <div className="ask-opts">
        {(payload.options ?? []).map((opt) => (
          <button
            key={opt}
            type="button"
            className={`ask-opt${chosen === opt ? " chosen" : ""}`}
            onClick={() => answer(opt)}
          >
            {opt}
          </button>
        ))}
        <input
          className="ask-opt own"
          placeholder="or type your own…"
          value={own}
          onChange={(e) => setOwn(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && own.trim()) {
              e.preventDefault();
              answer(own.trim());
            }
          }}
        />
      </div>
    </div>
  );
}
