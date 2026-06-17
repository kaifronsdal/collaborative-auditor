/**
 * Span → role routing and per-role event bucketing (STREAMING.md §D).
 *
 * The backend opens `span(name="auditor")` / `span(name="target")` per branch
 * and registers their ids in `span_role`. Every event carries a `span_id`; to
 * find which column it belongs to we walk `span_id → parent → …` until we hit a
 * registered role span (model/tool calls nest under e.g. `send_message`).
 */
import type { Event, ModelEvent } from "@tsmono/inspect-common";

import type { BranchId, Role } from "./wire";
import type { SessionState } from "../store/session";

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

/**
 * Bucket every stored event into `[branch][role]` lists, preserving insertion
 * order (the `Map` iterates in insertion order, which matches event arrival).
 */
export function eventsByRole(state: SessionState): EventsByRole {
  const out: EventsByRole = {};
  for (const ev of state.events.values()) {
    const role = resolveRole(ev.span_id, state.spanParent, state.spanRole);
    if (!role) continue;
    const [branch, r] = role;
    (out[branch] ??= { auditor: [], target: [] })[r].push(ev);
  }
  return out;
}

export function isModelEvent(ev: Event): ev is ModelEvent {
  return ev.event === "model";
}
