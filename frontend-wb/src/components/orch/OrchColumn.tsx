/**
 * `<OrchColumn>` — the M1 orchestrator column shell.
 *
 * Header (status pill + step/play/pause), a scrolling turn list, and a
 * composer that talks to the orchestrator via `{t:"orch_send"}`. Turns are
 * derived from `byRole["orch"]["orch"]`: each `ModelEvent` is one turn, its
 * following `ToolEvent(function="python")` is the code cell, and every
 * `InfoEvent(source="orchestrator")` is bucketed by `data.turn`.
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

/** Header status text (UI-ITERATION §1/§3). */
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
  const pendingGates = orch?.pending_gates ?? EMPTY_GATES;
  const last = turns.at(-1);
  const cellRunning = last?.py?.pending === true;

  // Resolve each pending gate's card payload so the header popover can show
  // its `.gate-desc` and per-row approve. `display_id === InfoEvent.uuid`
  // for stable cards, so index outputs across all turns once.
  const gateInfo = useMemo(() => {
    if (pendingGates.length === 0) return [];
    const byId = new Map<string, DisplayInfoEvent>();
    for (const t of turns) for (const o of t.outputs) byId.set(o.data.id, o);
    return pendingGates.map((id) => {
      const wb = byId.get(id)?.data.bundle[WB_MIME];
      return {
        id,
        kind: (wb?.kind as string | undefined) ?? "gate",
        desc:
          (typeof wb?.description === "string" && wb.description) ||
          (wb?.kind === "prompt" && typeof wb.question === "string" && wb.question) ||
          id.slice(0, 8),
      };
    });
  }, [pendingGates, turns]);

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

  // Context-aware primary (mirrors DeskView's auditor composer).
  const primary = hasText
    ? { Icon: IconSend, title: "Send to orchestrator", onClick: sendText, mode: "send" as const }
    : isRunning
      ? {
          Icon: IconPause,
          title: "Pause orchestrator",
          onClick: () => send({ t: "pause", target: ORCH }),
          mode: "pause" as const,
        }
      : {
          Icon: IconPlay,
          title: "Run orchestrator",
          onClick: () => send({ t: "play", target: ORCH }),
          mode: "play" as const,
        };

  return (
    <div className="col-wrap orch-col-wrap">
      <div className="column orch-col" ref={scrollRef} onScroll={onScroll}>
        <OrchHeader
          status={status}
          model={shortModel(orch?.model)}
          turn={turns.length}
          dot={dotClass(status, cellRunning)}
          gates={gateInfo}
          onJump={jumpToGate}
          send={send}
        />

        {turns.map((t, i) => (
          <Fragment key={t.model.uuid ?? t.turn}>
            {i > 0 && <div className="turn-sep" />}
            <OrchTurn data={t} bgCells={bgCells} />
          </Fragment>
        ))}

        {orch?.notifications.map((n, i) => (
          <div key={`n${i}`} className="sys-chip">
            <i className="bi bi-bell sc-icon" />
            <span>{n}</span>
          </div>
        ))}

        {isRunning && !last?.model.pending && <ShimmerBubble />}
      </div>

      <div className="composer">
        <span className="composer-to">to: orchestrator</span>
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
              primary.onClick();
            }
          }}
          placeholder="Instruct the orchestrator…"
        />
        <div className="composer-lower">
          {cellRunning && hasText ? (
            <span className="composer-hint composer-hint-running">
              cell running — this will be read after turn {turns.length} ·{" "}
              <a onClick={sendNow}>send now (background current cell)</a>
            </span>
          ) : (
            <span className="composer-hint">
              <i className="bi bi-arrow-return-right" /> orchestrator
            </span>
          )}
          <button
            className={`primary primary-${primary.mode}`}
            onClick={primary.onClick}
            title={primary.title}
          >
            <primary.Icon size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

// ── header ──────────────────────────────────────────────────────────────────

type GateInfo = { id: string; kind: string; desc: string };

function OrchHeader({
  status,
  model,
  turn,
  dot,
  gates,
  onJump,
  send,
}: {
  status: Status;
  model: string;
  turn: number;
  dot: string;
  gates: GateInfo[];
  onJump: (id: string) => void;
  send: ReturnType<typeof useSession.getState>["send"];
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const isRunning = status === "running" || status === "waiting";
  // `ask_human` gates need a value; only `run_proposal`s can be bulk-approved.
  const approvable = gates.filter((g) => g.kind === "run_proposal");

  return (
    <div className="column-head orch-head">
      <span className="head-left">
        <span className="head-title">orchestrator</span>
        {model && (
          <>
            <span className="head-sep">·</span>
            <span className="head-model">{model}</span>
          </>
        )}
        <span className="head-sep">·</span>
        <span className="head-turn">t{turn}</span>
        <span className="head-sep">·</span>
        <span className={`rl-dot rl-dot-${dot}`} title={status} />
        <span className={`head-status head-status-${status}`}>
          {STATUS_TEXT[status]}
        </span>
        {gates.length > 0 && (
          <span className="head-gate-wrap">
            <button
              type="button"
              className="head-gate-jump"
              onClick={() => {
                onJump(gates[0].id);
                setOpen((v) => !v);
              }}
              title="jump to first pending gate"
            >
              {gates.length} waiting
            </button>
            {open && (
              <div className="head-gate-pop" onMouseLeave={() => setOpen(false)}>
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
                      setOpen(false);
                    }}
                  >
                    Approve all ({approvable.length})
                  </button>
                )}
              </div>
            )}
          </span>
        )}
      </span>
      <span className="orch-head-actions">
        {isRunning && (
          <button
            type="button"
            className="head-bg-btn"
            title="Detach: keep the current cell running in background"
            onClick={() => send({ t: "detach_cell" })}
          >
            <i className="bi bi-layer-backward" /> bg
          </button>
        )}
        <button
          type="button"
          title="Interrupt"
          disabled={!isRunning}
          onClick={() => send({ t: "cancel_cell", turn })}
        >
          <IconStop />
        </button>
        <button
          type="button"
          title="Step one turn"
          disabled={isRunning}
          onClick={() => send({ t: "step", target: ORCH })}
        >
          <IconStep />
        </button>
        <button
          type="button"
          title={isRunning ? "Pause" : "Play"}
          onClick={() => send({ t: isRunning ? "pause" : "play", target: ORCH })}
        >
          {isRunning ? <IconPause /> : <IconPlay />}
        </button>
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
const EMPTY_GATES: readonly string[] = [];
