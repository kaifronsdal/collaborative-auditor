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
import { useEffect, useRef, useState, type JSX } from "react";
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

/** `HH:MM` in the local zone. */
const hhmm = (d: Date): string =>
  `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;

export default function PromptCard({ payload, displayId, send }: Props): JSX.Element {
  const [own, setOwn] = useState("");
  // Local echo of the click so the button highlights before the round-trip.
  const [chosen, setChosen] = useState<string | null>(null);
  // Captured on the pending→resolved edge; a card that mounts already-resolved
  // (reconnect) has no time to show, so the `· HH:MM` chunk is omitted.
  const answeredAt = useRef<Date | null>(null);
  const wasPending = useRef(payload.pending);
  if (wasPending.current && !payload.pending && answeredAt.current == null) {
    answeredAt.current = new Date();
  }
  wasPending.current = payload.pending;

  const opts = payload.options ?? [];
  // Vertical layout when the option set is small or the labels are terse
  // single-char toggles (`y`/`n`) — number-key hints read better stacked.
  const vertical = opts.length > 0 && (opts.length <= 3 || opts.some((o) => o.length <= 2));

  const answer = (verdict: string): void => {
    if (!payload.pending) return;
    setChosen(verdict);
    send({ t: "approve", display_id: displayId, verdict });
  };

  // Number-key shortcuts (`1`..`9`) while this gate is the focused card.
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!payload.pending) return;
    const el = rootRef.current;
    if (!el) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.target instanceof HTMLInputElement) return;
      const n = Number(e.key);
      if (n >= 1 && n <= opts.length) {
        e.preventDefault();
        answer(opts[n - 1]);
      }
    };
    el.addEventListener("keydown", onKey);
    return () => el.removeEventListener("keydown", onKey);
  }, [payload.pending, opts]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!payload.pending) {
    // Collapsed: slim `.out-head` with the resolve stamp, question, answer.
    return (
      <div className="out answered" data-display-id={displayId}>
        <div className="out-head">
          <i className="bi bi-person-check" />
          <span className="out-meta">
            you answered{answeredAt.current ? ` · ${hhmm(answeredAt.current)}` : ""}
          </span>
        </div>
        <div className="ask-q">{payload.question}</div>
        <div className="ask-answered">
          <b>{payload.answer}</b>
        </div>
      </div>
    );
  }

  return (
    <div
      className="out gated gate-waiting"
      data-display-id={displayId}
      ref={rootRef}
      tabIndex={-1}
    >
      <div className="out-head">
        <i className="bi bi-person-raised-hand" style={{ color: "var(--gate-text)" }} />
        <span className="gate-tag">waiting on you</span>
      </div>
      <div className="ask-q">{payload.question}</div>
      <div className={`ask-opts${vertical ? " ask-opts-vert" : ""}`}>
        {opts.map((opt, i) => (
          <button
            key={opt}
            type="button"
            className={`ask-opt${chosen === opt ? " chosen" : ""}`}
            onClick={() => answer(opt)}
          >
            <span className="ask-opt-key">[{i + 1}]</span> {opt}
          </button>
        ))}
        <span className="ask-opt-own-wrap">
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
          <button
            type="button"
            className="ask-opt-send"
            disabled={!own.trim()}
            title="send"
            onClick={() => own.trim() && answer(own.trim())}
          >
            <i className="bi bi-arrow-up" />
          </button>
        </span>
      </div>
    </div>
  );
}
