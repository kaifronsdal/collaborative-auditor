/**
 * `GateCard` — the shared `.out.gated` shell for every human-in-the-loop
 * pause (UI-AUDIT.md §D). One component; `payload.kind` selects the body
 * variant and how `approve` builds its verdict:
 *
 * - `prompt`       — `wb.ask_human`. Body = option pills; each pill *is* the
 *                    approve, so no `.gate-bar`. Free-text row only appears
 *                    when `options===null` (or via the `other…` link).
 * - `run_proposal` — `wb.run_audits` pre-launch. Body = config line + seed
 *                    checklist; approve ships `{surviving: [seed.id, …]}`.
 * - `cite_proposal`— `wb.cite`. Body = quote checklist; approve ships
 *                    `{signed: true, edits: {quotes: [checked, …]}}`.
 *
 * Resolved cards collapse to a single `.out-head` line (icon + summary +
 * `HH:MM`); no body. Deny is a return-not-throw on the backend, so denied
 * gates stay visible with the reason.
 */
import { useEffect, useRef, useState, type JSX } from "react";
import type { Up } from "../../../lib/wire";

// -- payload shapes -----------------------------------------------------------

export type PromptPayload = {
  kind: "prompt";
  id: string;
  question: string;
  options: string[] | null;
  answer: string | null;
  answered_at: string | null;
  pending: boolean;
};

type Seed = { id: string; text: string };

export type RunProposalPayload = {
  kind: "run_proposal";
  id: string;
  description: string;
  n: number;
  n_per_seed: number;
  seeds: Seed[];
  /** Decision-relevant fields — `n_per_seed` always; `model`/`max_turns`
   *  when the caller supplied them (UI-AUDIT §A). */
  config: { model?: string; max_turns?: number; n_per_seed: number };
  pending: boolean;
  verdict: { denied?: boolean; surviving?: string[]; reason?: string } | null;
};

export type Quote = { sample_id: string; at: number; role: string; text: string };

export type CiteProposalPayload = {
  kind: "cite_proposal";
  id: string;
  claim: string;
  description: string;
  quotes: Quote[];
  grades_ref: string | null;
  pending: boolean;
  verdict: { signed?: boolean; by?: string; reason?: string } | null;
};

export type GatePayload = PromptPayload | RunProposalPayload | CiteProposalPayload;

type Props = {
  payload: GatePayload;
  displayId: string;
  send: (msg: Up) => void;
};

/** ISO → `HH:MM` local. */
const hhmm = (iso: string): string => {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

const ellipsis = (s: string, n: number): string =>
  s.length > n ? s.slice(0, n - 1) + "…" : s;

// -- shared shell -------------------------------------------------------------

export default function GateCard({ payload, displayId, send }: Props): JSX.Element {
  // Checklist state for the two gate-bar variants. Keyed by seed-id / quote
  // index respectively; hoisted here so `buildVerdict` can read it without
  // threading refs through the body components.
  const [struck, setStruck] = useState<Set<string>>(new Set());
  const [denyReason, setDenyReason] = useState<string | null>(null);

  const resolve = (verdict: unknown): void =>
    send({ t: "approve", display_id: displayId, verdict });

  const toggle = (key: string): void =>
    setStruck((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  if (!payload.pending) {
    return (
      <div className="out answered" data-display-id={displayId}>
        <div className="out-head">{resolvedHead(payload)}</div>
      </div>
    );
  }

  // `prompt` has no gate-bar — the option buttons *are* the approve. The
  // purple `.gated` tint identifies it, so no icon head.
  if (payload.kind === "prompt") {
    return (
      <div className="out gated gate-waiting" data-display-id={displayId}>
        <PromptBody payload={payload} resolve={resolve} />
      </div>
    );
  }

  const v = variant(payload, struck);
  const deny = (reason: string): void => resolve(v.denyVerdict(reason));

  return (
    <div className="out gated gate-waiting" data-display-id={displayId}>
      <div className="gate-desc">
        <i className={`bi ${v.icon}`} />
        {v.desc}
      </div>
      {payload.kind === "run_proposal" ? (
        <SeedBody payload={payload} struck={struck} toggle={toggle} />
      ) : (
        <CiteBody payload={payload} struck={struck} toggle={toggle} />
      )}
      {denyReason != null && (
        <input
          autoFocus
          className="gate-deny-reason"
          placeholder="reason (optional) — enter to deny, esc to cancel"
          value={denyReason}
          onChange={(e) => setDenyReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") deny(denyReason);
            if (e.key === "Escape") setDenyReason(null);
          }}
        />
      )}
      <div className="gate-bar">
        <button
          type="button"
          className="gate-btn deny"
          onClick={() => (denyReason == null ? setDenyReason("") : deny(denyReason))}
        >
          deny
        </button>
        <span className="gate-spacer" />
        <button
          type="button"
          className="gate-btn primary"
          onClick={() => resolve(v.buildVerdict())}
        >
          <i className="bi bi-check2" /> {v.approveLabel}
        </button>
      </div>
    </div>
  );
}

// -- variant config -----------------------------------------------------------

type BodyProps<P> = { payload: P; struck: Set<string>; toggle: (k: string) => void };

type Variant = {
  icon: string;
  desc: string;
  approveLabel: string;
  buildVerdict: () => unknown;
  denyVerdict: (reason: string) => unknown;
};

function variant(p: RunProposalPayload | CiteProposalPayload, struck: Set<string>): Variant {
  if (p.kind === "run_proposal") {
    const surviving = p.seeds.filter((s) => !struck.has(s.id));
    return {
      icon: "bi-rocket-takeoff",
      desc: p.description,
      approveLabel: `approve ${surviving.length * p.n_per_seed}`,
      buildVerdict: () => ({ surviving: surviving.map((s) => s.id) }),
      denyVerdict: (reason) => ({ denied: true, reason }),
    };
  }
  const kept = p.quotes.filter((_, i) => !struck.has(String(i)));
  return {
    icon: "bi-bookmark-star",
    desc: p.description,
    approveLabel: `sign ${kept.length}`,
    buildVerdict: () => ({ signed: true, edits: { quotes: kept } }),
    denyVerdict: (reason) => ({ signed: false, reason }),
  };
}

// -- resolved single-line head ------------------------------------------------

function resolvedHead(p: GatePayload): JSX.Element {
  if (p.kind === "prompt") {
    return (
      <>
        <i className="bi bi-person-check" />
        <span className="gate-resolved">
          {p.question} → <b>{p.answer}</b>
          {p.answered_at && ` · ${hhmm(p.answered_at)}`}
        </span>
      </>
    );
  }
  if (p.kind === "run_proposal") {
    const denied = p.verdict?.denied;
    return (
      <>
        <i className={`bi bi-${denied ? "x-circle" : "check-circle"}`} />
        <span className="gate-resolved">
          {denied ? (
            <>denied{p.verdict?.reason && ` — ${p.verdict.reason}`}</>
          ) : (
            <>
              approved · <b>{p.n}</b> audits
            </>
          )}
        </span>
      </>
    );
  }
  const signed = p.verdict?.signed;
  return (
    <>
      <i className={`bi bi-${signed ? "bookmark-check-fill" : "x-circle"}`} />
      <span className="gate-resolved">
        {signed ? (
          <>
            signed{p.verdict?.by && ` by ${p.verdict.by}`} · <b>{p.quotes.length}</b> quotes
          </>
        ) : (
          <>refused{p.verdict?.reason && ` — ${p.verdict.reason}`}</>
        )}
      </span>
    </>
  );
}

// -- prompt body --------------------------------------------------------------

function PromptBody({
  payload,
  resolve,
}: {
  payload: PromptPayload;
  resolve: (v: unknown) => void;
}): JSX.Element {
  const [own, setOwn] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);
  // Free-text row: always shown when there are no options; otherwise hidden
  // behind an `other…` link (UI-AUDIT §C).
  const [ownOpen, setOwnOpen] = useState(payload.options == null);

  const opts = payload.options ?? [];
  const vertical = opts.length > 0 && (opts.length <= 3 || opts.some((o) => o.length <= 2));
  // Key hints only when the set is large enough that scanning is slow;
  // rendered as a faint superscript, no brackets (UI-AUDIT §C).
  const showKeys = opts.length > 3;

  const answer = (v: string): void => {
    setChosen(v);
    resolve(v);
  };

  // `1`..`9` shortcuts while the card has focus.
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
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
  }, [opts]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div ref={rootRef} tabIndex={-1}>
      <div className="ask-q">{payload.question}</div>
      <div className={`ask-opts${vertical ? " ask-opts-vert" : ""}`}>
        {opts.map((opt, i) => (
          <button
            key={opt}
            type="button"
            className={`ask-opt${chosen === opt ? " chosen" : ""}`}
            onClick={() => answer(opt)}
          >
            {showKeys && <sup className="ask-opt-key">{i + 1}</sup>}
            {opt}
          </button>
        ))}
        {opts.length > 0 && !ownOpen && (
          <a className="ask-other" onClick={() => setOwnOpen(true)}>
            other…
          </a>
        )}
        {ownOpen && (
          <span className="ask-opt-own-wrap">
            <input
              autoFocus={payload.options != null}
              className="ask-opt own"
              placeholder="type your own…"
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
        )}
      </div>
    </div>
  );
}

// -- run_proposal body --------------------------------------------------------

function SeedBody({ payload, struck, toggle }: BodyProps<RunProposalPayload>): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const [popover, setPopover] = useState<string | null>(null);
  const cfg = payload.config;
  const cfgLine = [
    cfg.model,
    cfg.max_turns != null && `${cfg.max_turns} turns`,
    `×${cfg.n_per_seed}`,
  ]
    .filter(Boolean)
    .join(" · ");
  const survivingN = payload.seeds.length - struck.size;

  return (
    <>
      <div className="gate-config">{cfgLine}</div>
      <div className={`seed-preview${expanded ? "" : " collapsed"}`}>
        {payload.seeds.map((s) => {
          const isStruck = struck.has(s.id);
          return (
            <label
              key={s.id}
              className={`sp-row${isStruck ? " sp-row-struck" : ""}`}
              title={s.text}
            >
              <input
                type="checkbox"
                className="sp-check"
                checked={!isStruck}
                onChange={() => toggle(s.id)}
              />
              <span
                className="sp-seed"
                onClick={(e) => {
                  // Click on the text opens the full-seed popover instead of
                  // toggling the checkbox (the row is a `<label>`).
                  e.preventDefault();
                  setPopover((cur) => (cur === s.id ? null : s.id));
                }}
              >
                {ellipsis(s.text, 80)}
              </span>
              {popover === s.id && <div className="sp-pop">{s.text}</div>}
            </label>
          );
        })}
        <div className="sp-toggle" onClick={() => setExpanded((v) => !v)}>
          <i className={`bi bi-chevron-${expanded ? "down" : "right"}`} />{" "}
          <b>
            {survivingN} seed{survivingN === 1 ? "" : "s"}
            {payload.n_per_seed > 1 && ` × ${payload.n_per_seed}`}
          </b>
          {struck.size > 0 && (
            <span style={{ color: "var(--ink-faint)" }}> · {struck.size} struck</span>
          )}
        </div>
      </div>
    </>
  );
}

// -- cite_proposal body -------------------------------------------------------

function CiteBody({ payload, struck, toggle }: BodyProps<CiteProposalPayload>): JSX.Element {
  return (
    <>
      <div className="gate-claim">{payload.claim}</div>
      <div className="cite-quotes">
        {payload.quotes.map((q, i) => (
          <label
            key={i}
            className={`cite-q${struck.has(String(i)) ? " sp-row-struck" : ""}`}
          >
            <input
              type="checkbox"
              className="sp-check"
              checked={!struck.has(String(i))}
              onChange={() => toggle(String(i))}
            />
            <div className="cite-q-body">
              <div className="cite-q-head">
                <a className="qref" href={`wb://audit/${q.sample_id}`}>
                  {q.sample_id}
                </a>{" "}
                · t{q.at} · {q.role}
              </div>
              <div className="cite-q-text">{q.text}</div>
            </div>
          </label>
        ))}
      </div>
    </>
  );
}
