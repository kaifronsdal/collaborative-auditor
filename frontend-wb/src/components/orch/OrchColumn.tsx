/**
 * `<OrchColumn>` — the M1 orchestrator column shell.
 *
 * Location follows interaction (UI-AUDIT §B): the header is passive status
 * only (title + dot-or-gate-pill); transport (`⏹ ⏭ ▶/⏸`) and send live in the
 * composer footer where the cursor is, matching M0's `DeskView` composer.
 *
 * Turns are derived from `byRole["orch"]["orch"]`: each `ModelEvent` is one
 * turn, its following `ToolEvent(function="python")` is the code cell, and
 * every `InfoEvent(source="orchestrator")` is bucketed by `data.turn`.
 */
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type JSX,
} from "react";

import type { ChatMessage, Event } from "@tsmono/inspect-common";

import { useEvents } from "../../lib/selectors";
import type { BgJob, Status } from "../../lib/wire";
import { useSession } from "../../store/session";
import { ComposerTextarea } from "../ComposerTextarea";
import { IconPause, IconPlay, IconSend, IconStep, IconStop } from "../icons";
import { ShimmerBubble } from "../ShimmerBubble";
import { JobsChip } from "./JobsPanel";
import { OrchTurn, type NsSummary } from "./OrchTurn";
import {
  ORCH_SOURCE,
  WB_MIME,
  type DisplayInfoEvent,
  type OrchTurnData,
} from "./types";

import "./orch.css";

/** The synthetic branch id / role the backend registers the orch span under. */
const ORCH = "orch";

/** Header status text (tooltip only — UI-AUDIT §B). */
const STATUS_TEXT: Record<Status, string> = {
  idle: "idle",
  paused: "paused",
  running: "running",
  waiting: "waiting on you",
  ended: "ended",
};

/** Tri-state header dot class. `running` splits into generating (blue) vs
 *  kernel-executing (amber) by whether the current cell's `ToolEvent` is
 *  pending; `waiting` (gate) is purple. */
function dotClass(status: Status, cellRunning: boolean): string {
  if (status === "waiting") return "gate";
  if (status === "running") return cellRunning ? "exec" : "gen";
  return status;
}

/** `anthropic/claude-opus-4-8` → `opus-4-8`; `openai/gpt-5.4` → `gpt-5.4`. */
function shortModel(name: string | undefined): string {
  if (!name) return "";
  const tail = name.split("/").pop() ?? name;
  return tail.replace(/^claude-/, "");
}

export function OrchColumn(): JSX.Element {
  const send = useSession((s) => s.send);
  const orch = useSession((s) => s.orchestrator);
  const rewound = useSession((s) => s.rewound);
  const events = useEvents(ORCH, ORCH);

  const turns = useMemo(() => eventsToOrchTurns(events, rewound), [events, rewound]);
  const status = orch?.status ?? "idle";
  const isRunning = status === "running" || status === "waiting";
  const bgCells = orch?.bg_cells ?? EMPTY_BG;
  const bgJobs = orch?.bg_jobs ?? EMPTY_JOBS;
  const notifications = orch?.notifications ?? EMPTY_NOTIF;
  const last = turns.at(-1);
  const cellRunning = last?.tools.some((t) => t.pending) ?? false;
  const statusText = STATUS_TEXT[status];

  // Header pill: derive pending gates from the live event stream, not
  // ``orch.pending_gates`` — that field only ships in the full ``{t:"state"}``
  // push, so it's stale between reconnects. Gate cards land as
  // ``InfoEvent`` s with ``bundle[WB_MIME].pending === true`` and flip to
  // ``false`` on ``dh.update()`` when resolved, so scanning ``turns`` is
  // always current.
  const gateInfo = useMemo(() => {
    const out: { id: string; kind: string; desc: string }[] = [];
    for (const t of turns) {
      for (const o of t.outputs) {
        const wb = o.data.bundle[WB_MIME];
        if (!wb || !("pending" in wb) || !wb.pending) continue;
        const desc =
          wb.kind === "prompt"
            ? wb.question
            : wb.kind === "run_proposal"
              ? wb.description
              : wb.kind === "cite_proposal"
                ? wb.claim
                : null;
        if (desc == null) continue;
        out.push({ id: o.data.id, kind: wb.kind, desc: desc || o.data.id.slice(0, 8) });
      }
    }
    return out;
  }, [turns]);

  // §C sys-chip → origin-cell badge: parse `notifications` for
  // `cell-{N} … bound: {name}` and thread the settled binding into the turn
  // that emitted it, so `.cc-bg-chip` can show `done · {name}` in place.
  const settledBg = useMemo(() => {
    const m = new Map<number, string>();
    for (const n of notifications) {
      const match = /cell-(\d+).*bound: (\w+)/.exec(n);
      if (match) m.set(Number(match[1]), match[2]);
    }
    return m;
  }, [notifications]);

  // §7: accumulate the `ns` snapshot from every settled cell's `cell_done`
  // output — later cells win on rebind. Passed to every `<OrchTurn>` so the
  // collapsed `.cc-gist` can decorate identifiers with a `type · repr` tooltip.
  const ns = useMemo<NsSummary>(() => {
    const acc: Record<string, string> = {};
    for (const t of turns) {
      for (const o of t.outputs) {
        const wb = o.data.bundle[WB_MIME];
        if (wb?.kind === "cell_done") Object.assign(acc, wb.ns);
      }
    }
    return acc;
  }, [turns]);

  // Follow the live tail (same policy as M0 `LinearColumn`).
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  // §4: while the user has scrolled away from the tail, count new turns that
  // land and offer a floating `↓ N new` pill to jump back.
  const [unseen, setUnseen] = useState(0);
  const prevTurnCount = useRef(turns.length);
  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    const atTail = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stick.current = atTail;
    if (atTail) setUnseen(0);
  };
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });
  useEffect(() => {
    const grew = turns.length - prevTurnCount.current;
    prevTurnCount.current = turns.length;
    // Only count arrivals while scrolled away — the layout effect above already
    // followed the tail otherwise. `grew < 0` after a rewind → clear.
    if (grew > 0 && !stick.current) setUnseen((n) => n + grew);
    else if (grew < 0) setUnseen(0);
  }, [turns.length]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);
  const scrollToTail = (): void => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    stick.current = true;
    setUnseen(0);
  };

  const jumpToGate = (id: string): void => {
    const el = scrollRef.current?.querySelector<HTMLElement>(
      `[data-display-id="${id}"]`
    );
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  const [text, setText] = useState("");
  const hasText = text.trim().length > 0;

  const sendText = (): void => {
    if (!hasText) return;
    send({ t: "orch_send", text });
    setText("");
  };
  const sendNow = (): void => {
    // "send now" while a cell is running: background it, then deliver.
    send({ t: "detach_cell" });
    sendText();
  };
  // §11: kill the running cell (partial output preserved as the tool result)
  // and inject `text` into the *same* generate that reads that result — the
  // Cursor/Claude-Code Escape pattern. Distinct from `sendNow` (backgrounds
  // the cell; message reads *after* it settles).
  const interruptAndSend = (): void => {
    if (!hasText || !last) return;
    send({ t: "interrupt_and_send", turn: last.turn, text });
    setText("");
  };

  // PRODUCT-GAPS P2: coarse context-window gauge. `context_chars` only ships
  // in the full `{t:"state"}` push (once per turn), which is fine for a header
  // percentage — it doesn't need to track mid-stream deltas.
  const ctxChars = orch?.context_chars ?? 0;
  const ctxLimit = orch?.context_limit ?? 200_000;
  const ctxPct = Math.min(100, Math.round((ctxChars / ctxLimit) * 100));
  const ctxTitle = `${Math.round(ctxChars / 1000)}k / ${Math.round(ctxLimit / 1000)}k chars`;

  const meta = [
    shortModel(orch?.model),
    `turn ${turns.length}`,
    statusText,
  ].filter(Boolean).join(" · ");

  return (
    <div className="col-wrap orch-col-wrap">
      <div className="column orch-col" ref={scrollRef} onScroll={onScroll}>
        <OrchHeader
          meta={meta}
          statusText={statusText}
          dot={dotClass(status, cellRunning)}
          ctxPct={ctxPct}
          ctxTitle={ctxTitle}
          gates={gateInfo}
          jobs={bgJobs}
          turns={turns}
          onJump={jumpToGate}
          send={send}
        />

        {turns.map((t, i) => (
          <Fragment key={t.model.uuid ?? t.turn}>
            {i > 0 && <div className="turn-sep" />}
            <OrchTurn data={t} bgCells={bgCells} settledBg={settledBg.get(t.turn)} ns={ns} />
          </Fragment>
        ))}

        {isRunning && !last?.model.pending && <ShimmerBubble />}
      </div>

      <div className="composer">
        {unseen > 0 && (
          <button type="button" className="scroll-new" onClick={scrollToTail}>
            <i className="bi bi-arrow-down" /> {unseen} new
          </button>
        )}
        <ComposerTextarea
          value={text}
          setValue={setText}
          onEnter={cellRunning ? interruptAndSend : sendText}
          placeholder="Instruct the orchestrator…"
        />
        <div className="composer-lower">
          <div className="composer-controls">
            {isRunning && (
              <button
                type="button"
                title="interrupt cell"
                onClick={() => send({ t: "cancel_cell", turn: turns.length })}
              >
                <IconStop size={14} />
              </button>
            )}
            <button
              type="button"
              title="run one turn"
              disabled={isRunning}
              onClick={() => send({ t: "step", target: ORCH })}
            >
              <IconStep size={14} />
            </button>
            <button
              type="button"
              title={isRunning ? "pause after this turn" : "run"}
              onClick={() => send({ t: isRunning ? "pause" : "play", target: ORCH })}
            >
              {isRunning ? <IconPause size={14} /> : <IconPlay size={15} />}
            </button>
            <button
              type="button"
              title="restart kernel (clear Python namespace, keep conversation)"
              disabled={isRunning}
              onClick={() => send({ t: "restart_kernel" })}
            >
              <i className="bi bi-arrow-clockwise" style={{ fontSize: 14 }} />
            </button>
          </div>
          {cellRunning && hasText ? (
            <span className="composer-hint composer-hint-running truncate">
              or queue · <a onClick={sendNow}>send now (background)</a>
            </span>
          ) : (
            <span className="composer-hint truncate">
              enter to send · shift+enter newline
            </span>
          )}
          {cellRunning && hasText ? (
            <button
              className="primary primary-send primary-interrupt"
              onClick={interruptAndSend}
              title="Interrupt the running cell and send now"
            >
              <i className="bi bi-send" style={{ fontSize: 13 }} />
            </button>
          ) : (
            <button
              className="primary primary-send"
              onClick={sendText}
              disabled={!hasText}
              title="Send to orchestrator"
            >
              <IconSend size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── header ──────────────────────────────────────────────────────────────────

type GateInfo = { id: string; kind: string; desc: string };

/** Passive-status header: title (with meta tooltip) + either the bare status
 *  dot or, when gates are pending, a purple pill that absorbs the dot. Hover
 *  the pill for the per-gate popover; click it to jump to the first gate. */
function OrchHeader({
  meta,
  statusText,
  dot,
  ctxPct,
  ctxTitle,
  gates,
  jobs,
  turns,
  onJump,
  send,
}: {
  meta: string;
  statusText: string;
  dot: string;
  ctxPct: number;
  ctxTitle: string;
  gates: GateInfo[];
  jobs: readonly BgJob[];
  turns: readonly OrchTurnData[];
  onJump: (id: string) => void;
  send: ReturnType<typeof useSession.getState>["send"];
}): JSX.Element {
  const [hover, setHover] = useState(false);
  // `ask_human` gates need a value; only `run_proposal`s can be bulk-approved.
  const approvable = gates.filter((g) => g.kind === "run_proposal");
  const ctxLevel = ctxPct > 90 ? "hi" : ctxPct > 70 ? "mid" : "lo";

  return (
    <div className="column-head orch-head hstack g10">
      <span className="head-left">
        <span className="head-title" title={meta}>orchestrator</span>
        <span className={`ctx-gauge ctx-${ctxLevel}`} title={ctxTitle}>
          {ctxPct}%
        </span>
        <JobsChip
          jobs={jobs}
          turns={turns}
          onJump={onJump}
          onCancel={(id) => send({ t: "cancel_bg", id })}
        />
        {gates.length > 0 ? (
          <span
            className="head-gate-wrap"
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
          >
            <button
              type="button"
              className="head-gate-jump"
              onClick={() => onJump(gates[0].id)}
              title="jump to first pending gate"
            >
              <span className="rl-dot rl-dot-gate" />
              {gates.length} waiting on you
            </button>
            {hover && (
              <div className="head-gate-pop">
                {gates.map((g) => (
                  <div key={g.id} className="hgp-row hstack g6">
                    <span className="hgp-desc truncate" onClick={() => onJump(g.id)}>
                      {g.desc}
                    </span>
                    {g.kind === "run_proposal" && (
                      <>
                        <button
                          type="button"
                          className="hgp-btn"
                          onClick={() =>
                            send({ t: "approve", display_id: g.id, verdict: {} })
                          }
                        >
                          approve
                        </button>
                        <button
                          type="button"
                          className="hgp-btn deny"
                          onClick={() =>
                            send({
                              t: "approve",
                              display_id: g.id,
                              verdict: { denied: true },
                            })
                          }
                        >
                          deny
                        </button>
                      </>
                    )}
                  </div>
                ))}
                {approvable.length > 1 && (
                  <button
                    type="button"
                    className="hgp-all"
                    onClick={() => {
                      for (const g of approvable) {
                        send({ t: "approve", display_id: g.id, verdict: {} });
                      }
                    }}
                  >
                    Approve all ({approvable.length})
                  </button>
                )}
              </div>
            )}
          </span>
        ) : (
          <span className={`rl-dot rl-dot-${dot}`} title={statusText} />
        )}
      </span>
    </div>
  );
}

// ── turn derivation ─────────────────────────────────────────────────────────

/**
 * Group the orch span's flat event stream into `OrchTurnData[]`.
 *
 * A `ModelEvent` opens a turn. Every following `ToolEvent` (any `function`)
 * belongs to that turn (M1-HYBRID.md — the agent has 8 tools and may call
 * several per generate). `InfoEvent(source="orchestrator")` are bucketed by
 * `data.turn` rather than stream position, so a backgrounded cell's late
 * outputs land under the turn that emitted them (M1-NOTEBOOK.md §Background).
 *
 * The user "ask" for turn N is the delta between turn N's `ModelEvent.input`
 * and turn N-1's — i.e. the trailing user messages the composer / `orch_send`
 * appended before this generate.
 *
 * §2 rewind: events whose uuid is in `rewound` (live `{t:"rewound"}` push) or
 * that carry a top-level `rewound: true` flag (reconnect snapshot —
 * `session.mark_rewound` writes it directly onto the stored event dict) are
 * dropped — they belong to a turn the user restarted from.
 */
export function eventsToOrchTurns(
  events: readonly Event[],
  rewound: ReadonlySet<string> = EMPTY_REWOUND
): OrchTurnData[] {
  const turns: OrchTurnData[] = [];
  const byTurn = new Map<number, OrchTurnData>();
  let prevInputLen = 0;

  for (const ev of events) {
    if (ev.uuid != null && rewound.has(ev.uuid)) continue;
    if ((ev as { rewound?: unknown }).rewound === true) continue;
    if (ev.event === "model") {
      const turn = turns.length + 1;
      // Trailing non-assistant messages since the previous generate.
      const tail = ev.input.slice(prevInputLen).filter((m) => m.role === "user");
      prevInputLen = ev.input.length + (ev.output?.choices?.length ? 1 : 0);
      const data: OrchTurnData = {
        turn,
        model: ev,
        userInput: tail as ChatMessage[],
        tools: [],
        outputs: [],
      };
      turns.push(data);
      byTurn.set(turn, data);
    } else if (ev.event === "tool") {
      const cur = turns[turns.length - 1];
      if (cur) cur.tools.push(ev);
    } else if (ev.event === "info" && ev.source === ORCH_SOURCE) {
      const de = ev as DisplayInfoEvent;
      // Not every orch InfoEvent is a display — `rewind_marker` (§2) carries
      // `{kind, to_turn}` with no `bundle` and would crash `<OrchTurn>`'s
      // `bundle[WB_MIME]` reads. It has no card; drop it.
      if (de.data?.bundle == null) continue;
      const owner = byTurn.get(de.data.turn) ?? turns[turns.length - 1];
      if (owner) owner.outputs.push(de);
    }
  }
  return turns;
}

const EMPTY_BG: readonly number[] = [];
const EMPTY_JOBS: readonly BgJob[] = [];
const EMPTY_NOTIF: readonly string[] = [];
const EMPTY_REWOUND: ReadonlySet<string> = new Set();
