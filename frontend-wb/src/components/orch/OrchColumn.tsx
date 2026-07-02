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
import type { Status } from "../../lib/wire";
import { useSession } from "../../store/session";
import { IconPause, IconPlay, IconSend, IconStep, IconStop } from "../icons";
import { ShimmerBubble } from "../ShimmerBubble";
import { OrchTurn } from "./OrchTurn";
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
  const events = useEvents(ORCH, ORCH);

  const turns = useMemo(() => eventsToOrchTurns(events), [events]);
  const status = orch?.status ?? "idle";
  const isRunning = status === "running" || status === "waiting";
  const bgCells = orch?.bg_cells ?? EMPTY_BG;
  const notifications = orch?.notifications ?? EMPTY_NOTIF;
  const last = turns.at(-1);
  const cellRunning = last?.py?.pending === true;
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
        if (!wb || wb.pending !== true || !GATE_KINDS.has(wb.kind as string)) continue;
        out.push({
          id: o.data.id,
          kind: wb.kind as string,
          desc:
            (typeof wb.description === "string" && wb.description) ||
            (typeof wb.question === "string" && wb.question) ||
            (typeof wb.claim === "string" && wb.claim) ||
            o.data.id.slice(0, 8),
        });
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

  // Follow the live tail (same policy as M0 `LinearColumn`).
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

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
          gates={gateInfo}
          onJump={jumpToGate}
          send={send}
        />

        {turns.map((t, i) => (
          <Fragment key={t.model.uuid ?? t.turn}>
            {i > 0 && <div className="turn-sep" />}
            <OrchTurn data={t} bgCells={bgCells} settledBg={settledBg.get(t.turn)} />
          </Fragment>
        ))}

        {isRunning && !last?.model.pending && <ShimmerBubble />}
      </div>

      <div className="composer">
        <textarea
          className="composer-input"
          rows={1}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            const el = e.target;
            el.style.height = "auto";
            el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendText();
            }
          }}
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
                <IconStop />
              </button>
            )}
            <button
              type="button"
              title="run one turn"
              disabled={isRunning}
              onClick={() => send({ t: "step", target: ORCH })}
            >
              <IconStep />
            </button>
            <button
              type="button"
              title={isRunning ? "pause after this turn" : "run"}
              onClick={() => send({ t: isRunning ? "pause" : "play", target: ORCH })}
            >
              {isRunning ? <IconPause /> : <IconPlay />}
            </button>
          </div>
          {cellRunning && hasText ? (
            <span className="composer-hint composer-hint-running">
              reads after turn {turns.length} · <a onClick={sendNow}>send now</a>
            </span>
          ) : (
            <span className="composer-hint">
              enter to send · shift+enter newline
            </span>
          )}
          <button
            className="primary primary-send"
            onClick={sendText}
            disabled={!hasText}
            title="Send to orchestrator"
          >
            <IconSend size={16} />
          </button>
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
  gates,
  onJump,
  send,
}: {
  meta: string;
  statusText: string;
  dot: string;
  gates: GateInfo[];
  onJump: (id: string) => void;
  send: ReturnType<typeof useSession.getState>["send"];
}): JSX.Element {
  const [hover, setHover] = useState(false);
  // `ask_human` gates need a value; only `run_proposal`s can be bulk-approved.
  const approvable = gates.filter((g) => g.kind === "run_proposal");

  return (
    <div className="column-head orch-head">
      <span className="head-left">
        <span className="head-title" title={meta}>orchestrator</span>
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
                  <div key={g.id} className="hgp-row">
                    <span className="hgp-desc" onClick={() => onJump(g.id)}>
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
 * A `ModelEvent` opens a turn. Any following `ToolEvent` with
 * `function === "python"` is that turn's code cell (there is at most one — the
 * agent has a single tool). `InfoEvent(source="orchestrator")` are bucketed by
 * `data.turn` rather than stream position, so a backgrounded cell's late
 * outputs land under the turn that emitted them (M1-NOTEBOOK.md §Background).
 *
 * The user "ask" for turn N is the delta between turn N's `ModelEvent.input`
 * and turn N-1's — i.e. the trailing user messages the composer / `orch_send`
 * appended before this generate.
 */
export function eventsToOrchTurns(events: readonly Event[]): OrchTurnData[] {
  const turns: OrchTurnData[] = [];
  const byTurn = new Map<number, OrchTurnData>();
  let prevInputLen = 0;

  for (const ev of events) {
    if (ev.event === "model") {
      const turn = turns.length + 1;
      // Trailing non-assistant messages since the previous generate.
      const tail = ev.input.slice(prevInputLen).filter((m) => m.role === "user");
      prevInputLen = ev.input.length + (ev.output?.choices?.length ? 1 : 0);
      const data: OrchTurnData = {
        turn,
        model: ev,
        userInput: tail as ChatMessage[],
        py: undefined,
        outputs: [],
      };
      turns.push(data);
      byTurn.set(turn, data);
    } else if (ev.event === "tool" && ev.function === "python") {
      const cur = turns[turns.length - 1];
      if (cur) cur.py = ev;
    } else if (ev.event === "info" && ev.source === ORCH_SOURCE) {
      const de = ev as DisplayInfoEvent;
      const owner = byTurn.get(de.data.turn) ?? turns[turns.length - 1];
      if (owner) owner.outputs.push(de);
    }
  }
  return turns;
}

const EMPTY_BG: readonly number[] = [];
const GATE_KINDS = new Set(["prompt", "run_proposal", "cite_proposal"]);
const EMPTY_NOTIF: readonly string[] = [];
