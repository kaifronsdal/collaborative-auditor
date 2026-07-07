import type { ChatMessage, Content } from "@tsmono/inspect-common";

import { useMemo, type JSX } from "react";

import { useSession } from "../store/session";
import { CollapsibleContent } from "./CollapsibleContent";
import { Markdown } from "./Markdown";
import type { TurnScorePayload } from "./orch/types";

const COLLAPSIBLE_ROLES: ReadonlySet<ChatMessage["role"]> = new Set(["system", "user", "tool"]);

type Props = {
  msg?: ChatMessage;
  /** Override role styling (e.g. force "assistant" for a streaming output). */
  role?: ChatMessage["role"];
  ghost?: boolean;
  byline?: string;
  children?: React.ReactNode;
};

/** Render one Content block. */
function renderBlock(block: Content, i: number): JSX.Element | null {
  switch (block.type) {
    case "text":
      return <Markdown key={i}>{block.text}</Markdown>;
    case "reasoning":
      // Anthropic extended-thinking with ``redacted: true`` puts an
      // encrypted base64 blob in ``.reasoning`` — never render that.
      // Show the ``.summary`` (if the provider gave one) or a placeholder.
      // Non-redacted reasoning (open-weights / opt-in) renders as before.
      if ((block as { redacted?: boolean }).redacted) {
        const summary = (block as { summary?: string }).summary;
        return (
          <span key={i} className="reasoning reasoning-redacted">
            {summary || "[extended reasoning — redacted by provider]"}
          </span>
        );
      }
      return (
        <span key={i} className="reasoning">
          {block.reasoning}
        </span>
      );
    case "tool_use":
      return (
        <span key={i} className="reasoning">
          [tool_use {block.name}]
        </span>
      );
    default:
      return (
        <span key={i} className="reasoning">
          [{block.type}]
        </span>
      );
  }
}

export function renderContent(content: ChatMessage["content"]): React.ReactNode {
  if (typeof content === "string") return <Markdown>{content}</Markdown>;
  return content.map(renderBlock);
}

export function Bubble({ msg, role, ghost, byline, children }: Props): JSX.Element {
  const effectiveRole = role ?? msg?.role ?? "user";
  const cls = `bubble ${effectiveRole}${ghost ? " ghost" : ""}`;
  const body = children ?? (msg ? renderContent(msg.content) : null);
  // Assistant output streams and is the thing being read — never clip it.
  const inner = COLLAPSIBLE_ROLES.has(effectiveRole) ? (
    <CollapsibleContent>{body}</CollapsibleContent>
  ) : (
    body
  );
  return (
    <div className="bubble-wrap">
      {byline && <div className="bubble-by">{byline}</div>}
      <div className={cls}>{inner}</div>
    </div>
  );
}

/**
 * P1.8(c) — every `turn_score` payload for the target message with id `uuid`.
 *
 * The backend routes `InfoEvent(source="turn_score", span_id=target_span)`
 * into `byRole[branch].target`, but this hook reads the flat `events` map
 * instead so it works from any render site (linear column, swimlane splice,
 * candidate card) without needing the branch id. O(events) per render — fine
 * at M0 desk scale; move to a `byTurnScore` index in the store if it shows up
 * in a profile.
 */
export function useTurnScores(uuid: string | null | undefined): TurnScorePayload[] {
  const events = useSession((s) => s.events);
  return useMemo(() => {
    if (uuid == null) return [];
    const out: TurnScorePayload[] = [];
    for (const ev of events.values()) {
      if (ev.event !== "info") continue;
      const d = (ev as { data?: { kind?: string; turn_uuid?: string } }).data;
      if (d?.kind === "turn_score" && d.turn_uuid === uuid) {
        out.push(d as TurnScorePayload);
      }
    }
    return out;
  }, [events, uuid]);
}

/** Badge row under a target assistant bubble: one chip per live scanner.
 *  Pending (`score === null`, no error) → spinner; error → danger tint;
 *  otherwise `name: 0.70` with the explanation on hover. */
export function TurnScoreChips({ uuid }: { uuid: string | null | undefined }): JSX.Element | null {
  const scores = useTurnScores(uuid);
  if (scores.length === 0) return null;
  return (
    <div className="turn-score-row">
      {scores.map((s) => {
        const pending = s.score === null && s.error == null;
        const cls =
          "turn-score-chip" + (pending ? " pending" : s.error ? " error" : "");
        return (
          <span key={s.scanner} className={cls} title={s.error ?? s.explanation}>
            {s.scanner}
            {s.error != null
              ? ": err"
              : s.score != null
                ? `: ${s.score.toFixed(2)}`
                : ""}
          </span>
        );
      })}
    </div>
  );
}
