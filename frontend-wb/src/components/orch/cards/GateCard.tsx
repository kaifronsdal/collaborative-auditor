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
import { useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { Modal } from "@tsmono/react/components/Modal";

import type { Up } from "../../../lib/wire";
import type {
  CiteProposalPayload,
  PromptPayload,
  RunProposalPayload,
} from "../types";

type GatePayload = PromptPayload | RunProposalPayload | CiteProposalPayload;

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

  // A2 adversarial-review caveat: `approve` is `UNLOCKED` — the ack lands
  // in ~1ms, so a `useIsPending` guard wouldn't cover an 80ms double-click.
  // But `gate.resolve()` is server-idempotent (returns False on the 2nd),
  // so the R1 `sent` guard is deleted outright — a double-approve is a
  // no-op, not a race.
  const resolve = (verdict: unknown): void => {
    send({ t: "approve", display_id: displayId, verdict });
  };

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
        <CheckListPeek v={v} struck={struck} toggle={toggle} />
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
      <div className="gate-actions hstack g8">
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

/** One checklist row — shared shape for the inline peek and `ReviewModal`. */
type Row = { key: string; text: string; head?: JSX.Element };

type Variant = {
  icon: string;
  title: string;
  subtitle: JSX.Element | string;
  noun: string;
  n: number;
  kept: number;
  /** Full seed/quote list normalised to `Row` — both `<CheckListPeek>`
   *  (first `peekCap`) and `<ReviewModal>` (all, filterable) read this. */
  rows: Row[];
  peekCap: number;
  peekClass: string;
  peekRow: (r: Row) => ReactNode;
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
      rows: p.seeds.map((s) => ({ key: s.id, text: s.text })),
      peekCap: 3,
      peekClass: "gp-row hstack g10",
      peekRow: (r) => (
        <span className="gp-text truncate" title={r.text}>{ellipsis(r.text, 80)}</span>
      ),
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
    rows: p.quotes.map((qt, i) => ({
      key: String(i),
      text: qt.text,
      head: (
        <div className="cite-q-head">
          <a className="qref" href={`wb://audit/${qt.sample_id}`}>{qt.sample_id}</a>
          {" "}· t{qt.at} · {qt.role}
        </div>
      ),
    })),
    peekCap: 2,
    peekClass: "cite-q",
    peekRow: (r) => (
      <div className="cite-q-body">{r.head}<div className="cite-q-text">{r.text}</div></div>
    ),
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
      <div className={`gate-receipt hstack g10${ok ? "" : " denied"}`}>
        <i className={`bi ${ok ? "bi-check-lg" : "bi-x-lg"}`} />
        <span className="gate-receipt-label truncate">{label}</span>
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
  // Single-row layout when the whole request fits on one line — a y/n
  // shouldn't cost 110px of column. Falls back to the stacked title/actions
  // layout for long questions or >3 options.
  const inline = payload.question.length <= 50 && opts.length > 0 && opts.length <= 3;

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

  const buttons = opts.map((opt, i) => (
    <button
      key={opt}
      type="button"
      className={`gate-btn ${i === 0 ? "primary" : "ghost"}`}
      onClick={() => answer(opt)}
    >
      {showKeys && <sup className="ask-opt-key">{i + 1}</sup>}
      {opt}
    </button>
  ));

  if (inline) {
    return (
      <div ref={rootRef} tabIndex={-1}>
        <div className="gate-body gate-inline hstack g12">
          <i className="bi bi-question-circle" />
          <span className="gate-q truncate">{payload.question}</span>
          {buttons}
          {!ownOpen && (
            <a className="ask-other" onClick={() => setOwnOpen(true)}>
              other…
            </a>
          )}
        </div>
        {ownOpen && <div className="gate-actions gate-actions-own hstack g8">{ownRow}</div>}
      </div>
    );
  }

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
          <div className="gate-actions hstack g8">{buttons}</div>
          {ownOpen ? (
            <div className="gate-actions gate-actions-own hstack g8">{ownRow}</div>
          ) : (
            <a className="ask-other" onClick={() => setOwnOpen(true)}>
              other…
            </a>
          )}
        </>
      ) : (
        <div className="gate-actions gate-actions-own hstack g8">{ownRow}</div>
      )}
    </div>
  );
}

// -- checklist peek (seed/quote) ----------------------------------------------

/** `.gate-peek`: first-`v.peekCap` rows as strike-toggle checkboxes. Row body
 *  and label class are variant-supplied; the checkbox/struck wiring is shared. */
function CheckListPeek({
  v,
  struck,
  toggle,
}: {
  v: Variant;
  struck: Set<string>;
  toggle: (k: string) => void;
}): JSX.Element {
  return (
    <div className={`gate-peek${v.peekClass === "cite-q" ? " cite-quotes" : ""}`}>
      {v.rows.slice(0, v.peekCap).map((r) => (
        <label key={r.key} className={`${v.peekClass}${struck.has(r.key) ? " gp-struck" : ""}`}>
          <input
            type="checkbox"
            className="sp-check"
            checked={!struck.has(r.key)}
            onChange={() => toggle(r.key)}
          />
          {v.peekRow(r)}
        </label>
      ))}
    </div>
  );
}

// -- review modal (seed/quote checklist) -------------------------------------

function ReviewModal({
  v,
  struck,
  setStruck,
  toggle,
  onClose,
  onApprove,
}: {
  v: Variant;
  struck: Set<string>;
  setStruck: (s: Set<string>) => void;
  toggle: (k: string) => void;
  onClose: () => void;
  onApprove: () => void;
}): JSX.Element {
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? v.rows.filter((r) => r.text.toLowerCase().includes(needle)) : v.rows;
  }, [q, v.rows]);

  const allKeys = v.rows.map((r) => r.key);

  return (
    <Modal
      show
      onHide={onClose}
      title={`Review ${v.noun}`}
      className="wb-modal"
      padded={false}
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
      <div className="grm-tools hstack g10">
        <input
          data-autofocus
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
