/**
 * Span → role routing and per-role event bucketing (STREAMING.md §D).
 *
 * The backend opens `span(name="auditor")` / `span(name="target")` per branch
 * and registers their ids in `span_role`. Every event carries a `span_id`; to
 * find which column it belongs to we walk `span_id → parent → …` until we hit a
 * registered role span (model/tool calls nest under e.g. `send_message`).
 *
 * The display tree (`EventNode[]`) is built via inspect's `treeifyEvents`, which
 * applies presentational unwrapping (tool/subtask span collapse, etc.) — that
 * tree feeds BranchPoint/digest UI. Role routing uses the *raw* span graph
 * (`spanParent`), not the display tree, so a model event under a `tool` span
 * under the `target` span still resolves to `target`.
 */
import type { Event, ModelEvent } from "@tsmono/inspect-common";

import type { BranchId, Role } from "./wire";

export function resolveRole(
  spanId: string | null | undefined,
  spanParent: Map<string, string | null>,
  spanRole: Map<string, [BranchId, Role]>
): [BranchId, Role] | null {
  let cur: string | null | undefined = spanId;
  const seen = new Set<string>();
  while (cur != null && !seen.has(cur)) {
    const hit = spanRole.get(cur);
    if (hit) return hit;
    seen.add(cur);
    cur = spanParent.get(cur) ?? null;
  }
  return null;
}

export type EventsByRole = Record<BranchId, Record<Role, Event[]>>;

/** Fresh per-role bucket. Centralized so extending `Role` (e.g. adding
 *  `"orch"` for M1) touches one place instead of every initializer site. */
const emptyRoles = (): Record<Role, Event[]> => ({
  auditor: [],
  target: [],
  orch: [],
});

/**
 * Bucket events into `[branch][role]` lists, preserving iteration order.
 *
 * Used to (re)build the whole index on `state`/`pool` messages. Per-event
 * inserts/updates use {@link assignByRole} so untouched columns keep their
 * array reference (Zustand selectors short-circuit on `Object.is`).
 */
export function buildByRole(
  events: Iterable<Event>,
  spanParent: Map<string, string | null>,
  spanRole: Map<string, [BranchId, Role]>
): EventsByRole {
  const out: EventsByRole = {};
  for (const ev of events) {
    const role = resolveRole(ev.span_id, spanParent, spanRole);
    if (!role) continue;
    const [branch, r] = role;
    (out[branch] ??= emptyRoles())[r].push(ev);
  }
  return out;
}

/**
 * Return a copy of `byRole` with `ev` placed in `[branch][role]` — appended if
 * `prev` is absent, replaced in-place if `prev` is the prior version of the
 * same event. Only the path to that one array is cloned; every other column's
 * array reference is preserved so its `useEvents` selector doesn't re-render.
 */
export function assignByRole(
  byRole: EventsByRole,
  branch: BranchId,
  role: Role,
  ev: Event,
  prev: Event | undefined
): EventsByRole {
  const branchBuckets = byRole[branch] ?? emptyRoles();
  const arr = branchBuckets[role];
  let nextArr: Event[];
  if (prev !== undefined) {
    const i = arr.indexOf(prev);
    nextArr = arr.slice();
    if (i >= 0) nextArr[i] = ev;
    else nextArr.push(ev);
  } else {
    nextArr = [...arr, ev];
  }
  return {
    ...byRole,
    [branch]: { ...branchBuckets, [role]: nextArr },
  };
}

export function isModelEvent(ev: Event): ev is ModelEvent {
  return ev.event === "model";
}

/**
 * Convert a column's event stream into per-turn rows for rendering, reusing
 * inspect-view's chat resolution.
 *
 * Both columns are event-driven: walk `ModelEvent`s in order, each becomes one
 * `Turn`. The conversation body is reconstructed once from the *last* event's
 * `input` (which already contains every prior turn's output and tool results)
 * plus the last event's own output, then fed through inspect's
 * {@link resolveMessages} so `assistant.tool_calls[i]` get paired with their
 * following `ChatMessageTool` by `tool_call_id` — the same code path
 * inspect-view's `ChatView` uses. The resolved stream is chunked back into
 * turns at each assistant message (the n-th assistant = the n-th ModelEvent's
 * output, by construction).
 *
 * `hasToolEvents` distinguishes the auditor column (real `ToolEvent`s exist —
 * results render as `ToolPair` cards from those, so `ChatMessageTool` rows are
 * dropped from the conversation to avoid doubling) from the target column
 * (calls are simulated; results live only in the conversation, so they stay
 * and `resolveMessages` pairs them).
 */
import {
  resolveMessages,
  type ResolvedMessage,
} from "@tsmono/inspect-components/chat/messages";
import type {
  ChatMessage,
  ChatMessageTool,
  ToolCall,
  ToolEvent,
} from "@tsmono/inspect-common";

export type { ResolvedMessage };

export type Turn = {
  ev: ModelEvent;
  /** Messages belonging to this turn — everything after the previous
   *  assistant, through this one — with tool results already paired onto the
   *  assistant via inspect's `resolveMessages`. */
  resolved: ResolvedMessage[];
  /** Real tool executions this turn produced (auditor column only). */
  tools: ToolEvent[];
};

export function eventsToTurns(
  events: readonly Event[],
  hasToolEvents: boolean
): Turn[] {
  const models: ModelEvent[] = [];
  const toolsAfter: ToolEvent[][] = [];
  for (const ev of events) {
    if (ev.event === "model") {
      models.push(ev);
      toolsAfter.push([]);
    } else if (ev.event === "tool" && toolsAfter.length > 0) {
      toolsAfter[toolsAfter.length - 1].push(ev);
    }
  }
  if (models.length === 0) return [];

  // The last event's `input` is the full conversation so far. Replayed
  // ModelEvents (forked-branch prefix) carry the loop's real `state.messages`
  // via `EmittingTape`, so there is no empty-input case to special-case.
  const last = models[models.length - 1];
  const lastOut = last.output?.choices?.[0]?.message;
  let convo: ChatMessage[] = lastOut ? [...last.input, lastOut] : [...last.input];
  if (hasToolEvents) convo = convo.filter((m) => m.role !== "tool");
  const resolved = resolveMessages(convo);

  const turns: Turn[] = [];
  let bucket: ResolvedMessage[] = [];
  let mi = 0;
  for (const rm of resolved) {
    bucket.push(rm);
    if (rm.message.role === "assistant" && mi < models.length) {
      turns.push({ ev: models[mi], resolved: bucket, tools: toolsAfter[mi] });
      bucket = [];
      mi++;
    }
  }
  // A pending tail event whose output hasn't landed yet still gets a turn so
  // its input tail (the just-sent user message) and the streaming cursor show.
  if (mi < models.length) {
    turns.push({ ev: models[mi], resolved: bucket, tools: toolsAfter[mi] });
  }
  return turns;
}

/** Pair an assistant message's `tool_calls` with their results from the same
 *  turn's `toolMessages` (inspect-view's `ChatMessageRow` matching rule:
 *  by `tool_call_id`, falling back to positional). */
export function pairToolCalls(
  rm: ResolvedMessage
): { call: ToolCall; result?: ChatMessageTool }[] {
  const msg = rm.message;
  if (msg.role !== "assistant" || !msg.tool_calls?.length) return [];
  const pool = rm.toolMessages;
  return msg.tool_calls.map((call, i) => ({
    call,
    result: call.id ? pool.find((t) => t.tool_call_id === call.id) : pool[i],
  }));
}

/** Greatest index `i` with `turns[i].ev.timestamp <= ts`, or `-1`. */
export function bisectTurns(turns: readonly Turn[], ts: string): number {
  let lo = 0, hi = turns.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (turns[mid].ev.timestamp <= ts) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}
