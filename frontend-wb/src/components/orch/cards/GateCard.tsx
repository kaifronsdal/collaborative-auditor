/**
 * `GateCard` — the shared human-in-the-loop request card (UI-AUDIT.md §D).
 * One component; `payload.kind` selects the body variant and how `approve`
 * builds its verdict.
 *
 * Pending state reads as a **request from the agent to you** (Linear/GitHub
 * review-request idiom): thin purple `● waiting on you` header strip,
 * icon+question body, variant-specific preview, right-aligned action footer.
 * Detail overflow (`view all N seeds`, `view all quotes`) opens a `<Modal>`.
 *
 * - `prompt`       — `wb.ask_human`. Options render as the footer buttons
 *                    (first filled-purple, rest ghost); `other…` link below
 *                    reveals a free-text row. `options===null` → input+Send.
 * - `run_proposal` — `wb.run_audits` pre-launch. Body = config line + first
 *                    3 seeds; modal edits the full checklist. Approve ships
 *                    `{surviving: [seed.id, …]}`.
 * - `cite_proposal`— `wb.cite`. Body = claim + first 2 quotes; modal edits
 *                    the checklist. Approve ships `{signed, edits:{quotes}}`.
 *
 * Resolved state collapses to a compact receipt chip
 * (`✓ {q} · you answered {a} · HH:MM`); denied uses `✕` in `--danger`.
 */
import { useEffect, useMemo, useRef, useState, type JSX } from "react";

import Modal from "../../Modal";
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
  // Checklist state for the two review variants. Keyed by seed-id / quote
  // index; hoisted here so both the inline peek and the modal edit the same
  // set, and so `buildVerdict` can read it without threading refs.
  const [struck, setStruck] = useState<Set<string>>(new Set());
  const [denyOpen, setDenyOpen] = useState(false);
  const [denyReason, setDenyReason] = useState("");
  const [modalOpen, setModalOpen] = useState(false);

  const resolve = (verdict: unknown): void =>
    send({ t: "approve", display_id: displayId, verdict });

  const toggle = (key: string): void =>
    setStruck((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  if (!payload.pending) {
    return <Receipt payload={payload} displayId={displayId} />;
  }

  if (payload.kind === "prompt") {
    return (
      <div className="out gated gate-waiting" data-display-id={displayId}>
        <PromptBody payload={payload} resolve={resolve} />
      </div>
    );
  }

  const v = variant(payload, struck);
  const deny = (): void => resolve(v.denyVerdict(denyReason));

  return (
    <div className="out gated gate-waiting" data-display-id={displayId}>
      <div className="gate-body">
        <div className="gate-title">
          <i className={`bi ${v.icon}`} />
          <span>{v.title}</span>
        </div>
        <div className="gate-sub">{v.subtitle}</div>
        {payload.kind === "run_proposal" ? (
          <SeedPeek payload={payload} struck={struck} toggle={toggle} />
        ) : (
          <QuotePeek payload={payload} struck={struck} toggle={toggle} />
        )}
        <a className="gate-more" onClick={() => setModalOpen(true)}>
          view all {v.n} {v.noun} <i className="bi bi-arrow-right" />
        </a>
      </div>
      {denyOpen && (
        <input
          autoFocus
          className="gate-deny-reason"
          placeholder="reason (optional) — enter to deny, esc to cancel"
          value={denyReason}
          onChange={(e) => setDenyReason(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") deny();
            if (e.key === "Escape") setDenyOpen(false);
          }}
        />
      )}
      <div className="gate-actions">
        <button
          type="button"
          className="gate-btn danger"
          onClick={() => (denyOpen ? deny() : setDenyOpen(true))}
        >
          deny
        </button>
        <span className="gate-spacer" />
        <button
          type="button"
          className="gate-btn primary"
          onClick={() => resolve(v.buildVerdict())}
        >
          {v.approveLabel}
        </button>
      </div>
      {modalOpen && (
        <ReviewModal
          v={v}
          payload={payload}
          struck={struck}
          setStruck={setStruck}
          toggle={toggle}
          onClose={() => setModalOpen(false)}
          onApprove={() => {
            setModalOpen(false);
            resolve(v.buildVerdict());
          }}
        />
      )}
    </div>
  );
}

// -- variant config -----------------------------------------------------------

type Variant = {
  icon: string;
  title: string;
  subtitle: JSX.Element | string;
  noun: string;
  n: number;
  kept: number;
  approveLabel: string;
  buildVerdict: () => unknown;
  denyVerdict: (reason: string) => unknown;
};

function variant(p: RunProposalPayload | CiteProposalPayload, struck: Set<string>): Variant {
  if (p.kind === "run_proposal") {
    const surviving = p.seeds.filter((s) => !struck.has(s.id));
    const cfg = p.config;
    const sub = [cfg.model, cfg.max_turns != null && `${cfg.max_turns} turns`, `×${cfg.n_per_seed}`]
      .filter(Boolean)
      .join(" · ");
    return {
      icon: "bi-rocket-takeoff",
      title: p.description,
      subtitle: <code>{sub}</code>,
      noun: "seeds",
      n: p.seeds.length,
      kept: surviving.length,
      approveLabel: `approve ${surviving.length * p.n_per_seed}`,
      buildVerdict: () => ({ surviving: surviving.map((s) => s.id) }),
      denyVerdict: (reason) => ({ denied: true, reason }),
    };
  }
  const kept = p.quotes.filter((_, i) => !struck.has(String(i)));
  return {
    icon: "bi-bookmark-star",
    title: p.description,
    subtitle: p.claim,
    noun: "quotes",
    n: p.quotes.length,
    kept: kept.length,
    approveLabel: `sign ${kept.length}`,
    buildVerdict: () => ({ signed: true, edits: { quotes: kept } }),
    denyVerdict: (reason) => ({ signed: false, reason }),
  };
}

// -- resolved receipt chip ----------------------------------------------------

function Receipt({ payload: p, displayId }: { payload: GatePayload; displayId: string }): JSX.Element {
  // Structured — no `·` separators. `label` (ink-dim) | `value` (ink bold)
  // | spacer | `time` (right, faint). The chip reads as a single fact.
  let ok: boolean, label: string, value: string, time: string | null;
  if (p.kind === "prompt") {
    ok = true;
    label = ellipsis(p.question, 60);
    value = String(p.answer);
    time = p.answered_at ? hhmm(p.answered_at) : null;
  } else if (p.kind === "run_proposal") {
    ok = !p.verdict?.denied;
    label = ellipsis(p.description, 40);
    value = ok ? `approved ${p.n}` : (p.verdict?.reason || "denied");
    time = null;
  } else {
    ok = !!p.verdict?.signed;
    label = ellipsis(p.claim, 40);
    value = ok ? `signed ${p.quotes.length}` : (p.verdict?.reason || "denied");
    time = null;
  }
  return (
    <div className="out answered" data-display-id={displayId}>
      <div className={`gate-receipt${ok ? "" : " denied"}`}>
        <i className={`bi ${ok ? "bi-check-lg" : "bi-x-lg"}`} />
        <span className="gate-receipt-label">{label}</span>
        <b className="gate-receipt-value">{value}</b>
        {time && <span className="gate-receipt-time">{time}</span>}
      </div>
    </div>
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
  // Free-text row: always shown when there are no options; otherwise hidden
  // behind an `other…` link below the button row.
  const [ownOpen, setOwnOpen] = useState(payload.options == null);

  const opts = payload.options ?? [];
  const showKeys = opts.length > 3;

  const answer = (v: string): void => resolve(v);

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

  const ownRow = (
    <div className="ask-own-row">
      <input
        autoFocus={payload.options != null}
        className="ask-own"
        placeholder="type your own answer…"
        value={own}
        onChange={(e) => setOwn(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && own.trim()) {
            e.preventDefault();
            answer(own.trim());
          }
          if (e.key === "Escape" && payload.options != null) setOwnOpen(false);
        }}
      />
      <button
        type="button"
        className="gate-btn primary"
        disabled={!own.trim()}
        onClick={() => own.trim() && answer(own.trim())}
      >
        send
      </button>
    </div>
  );

  return (
    <div ref={rootRef} tabIndex={-1}>
      <div className="gate-body">
        <div className="gate-title">
          <i className="bi bi-question-circle" />
          <span>{payload.question}</span>
        </div>
      </div>
      {opts.length > 0 ? (
        <>
          <div className="gate-actions">
            {opts.map((opt, i) => (
              <button
                key={opt}
                type="button"
                className={`gate-btn ${i === 0 ? "primary" : "ghost"}`}
                onClick={() => answer(opt)}
              >
                {showKeys && <sup className="ask-opt-key">{i + 1}</sup>}
                {opt}
              </button>
            ))}
          </div>
          {ownOpen ? (
            <div className="gate-actions gate-actions-own">{ownRow}</div>
          ) : (
            <a className="ask-other" onClick={() => setOwnOpen(true)}>
              other…
            </a>
          )}
        </>
      ) : (
        <div className="gate-actions gate-actions-own">{ownRow}</div>
      )}
    </div>
  );
}

// -- run_proposal peek --------------------------------------------------------

type PeekProps<P> = { payload: P; struck: Set<string>; toggle: (k: string) => void };

function SeedPeek({ payload, struck, toggle }: PeekProps<RunProposalPayload>): JSX.Element {
  return (
    <div className="gate-peek">
      {payload.seeds.slice(0, 3).map((s) => {
        const isStruck = struck.has(s.id);
        return (
          <label
            key={s.id}
            className={`gp-row${isStruck ? " gp-struck" : ""}`}
            title={s.text}
          >
            <input
              type="checkbox"
              className="sp-check"
              checked={!isStruck}
              onChange={() => toggle(s.id)}
            />
            <span className="gp-text">{ellipsis(s.text, 80)}</span>
          </label>
        );
      })}
    </div>
  );
}

// -- cite_proposal peek -------------------------------------------------------

function QuotePeek({ payload, struck, toggle }: PeekProps<CiteProposalPayload>): JSX.Element {
  return (
    <div className="gate-peek cite-quotes">
      {payload.quotes.slice(0, 2).map((q, i) => {
        const key = String(i);
        return (
          <label key={i} className={`cite-q${struck.has(key) ? " gp-struck" : ""}`}>
            <input
              type="checkbox"
              className="sp-check"
              checked={!struck.has(key)}
              onChange={() => toggle(key)}
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
        );
      })}
    </div>
  );
}

// -- review modal (seed/quote checklist) -------------------------------------

function ReviewModal({
  v,
  payload,
  struck,
  setStruck,
  toggle,
  onClose,
  onApprove,
}: {
  v: Variant;
  payload: RunProposalPayload | CiteProposalPayload;
  struck: Set<string>;
  setStruck: (s: Set<string>) => void;
  toggle: (k: string) => void;
  onClose: () => void;
  onApprove: () => void;
}): JSX.Element {
  const [q, setQ] = useState("");

  type Row = { key: string; text: string; head?: JSX.Element };
  const rows: Row[] =
    payload.kind === "run_proposal"
      ? payload.seeds.map((s) => ({ key: s.id, text: s.text }))
      : payload.quotes.map((qt, i) => ({
          key: String(i),
          text: qt.text,
          head: (
            <div className="cite-q-head">
              <a className="qref">{qt.sample_id}</a> · t{qt.at} · {qt.role}
            </div>
          ),
        }));

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? rows.filter((r) => r.text.toLowerCase().includes(needle)) : rows;
  }, [q, rows]);

  const allKeys = rows.map((r) => r.key);

  return (
    <Modal
      open
      onClose={onClose}
      title={`Review ${v.noun}`}
      footer={
        <>
          <button type="button" className="gate-btn ghost" onClick={onClose}>
            cancel
          </button>
          <span className="gate-spacer" />
          <button type="button" className="gate-btn primary" onClick={onApprove}>
            {v.approveLabel}
          </button>
        </>
      }
    >
      <div className="grm-tools">
        <input
          className="grm-search"
          placeholder={`search ${v.noun}…`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <a onClick={() => setStruck(new Set(allKeys))}>uncheck all</a>
        <span className="grm-sep">·</span>
        <a onClick={() => setStruck(new Set())}>check all</a>
        <span className="grm-count">
          {v.kept} / {v.n}
        </span>
      </div>
      <div className="grm-list">
        {filtered.map((r) => (
          <label key={r.key} className={`grm-row${struck.has(r.key) ? " gp-struck" : ""}`}>
            <input
              type="checkbox"
              className="sp-check"
              checked={!struck.has(r.key)}
              onChange={() => toggle(r.key)}
            />
            <div className="grm-body">
              {r.head}
              <div className="grm-text">{r.text}</div>
            </div>
          </label>
        ))}
      </div>
    </Modal>
  );
}
