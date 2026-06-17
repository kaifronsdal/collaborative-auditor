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
    (out[branch] ??= { auditor: [], target: [] })[r].push(ev);
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
  const branchBuckets = byRole[branch] ?? { auditor: [], target: [] };
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
