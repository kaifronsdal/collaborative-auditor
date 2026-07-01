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
import { useSession } from "../../store/session";
import { IconPause, IconPlay, IconSend, IconStep } from "../icons";
import { ShimmerBubble } from "../ShimmerBubble";
import { OrchTurn } from "./OrchTurn";
import {
  ORCH_SOURCE,
  type DisplayInfoEvent,
  type OrchTurnData,
} from "./types";

import "./orch.css";

/** The synthetic branch id / role the backend registers the orch span under. */
const ORCH = "orch";

export function OrchColumn(): JSX.Element {
  const send = useSession((s) => s.send);
  const orch = useSession((s) => s.orchestrator);
  const events = useEvents(ORCH, ORCH);

  const turns = useMemo(() => eventsToOrchTurns(events), [events]);
  const status = orch?.status ?? "idle";
  const isRunning = status === "running";
  const bgCells = orch?.bg_cells ?? EMPTY_BG;

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

  const [text, setText] = useState("");
  const hasText = text.trim().length > 0;

  const sendText = (): void => {
    if (!hasText) return;
    send({ t: "orch_send", text });
    setText("");
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
        <div className="column-head orch-head">
          <span>orchestrator</span>
          <span className={`rl-dot rl-dot-${status}`} title={status} />
          <span className="orch-head-actions">
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
              onClick={() =>
                send({ t: isRunning ? "pause" : "play", target: ORCH })
              }
            >
              {isRunning ? <IconPause /> : <IconPlay />}
            </button>
          </span>
        </div>

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

        {isRunning && !turns.at(-1)?.model.pending && <ShimmerBubble />}
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
              primary.onClick();
            }
          }}
          placeholder="Ask the orchestrator…"
        />
        <div className="composer-lower">
          <span className="composer-hint">
            <i className="bi bi-arrow-return-right" /> orchestrator
          </span>
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
