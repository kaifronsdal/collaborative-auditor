/**
 * React selectors over the session store (STREAMING.md §D).
 *
 * `byRole` is maintained incrementally in the reducer, so these are O(1)
 * reads. A column's array reference only changes when an event in that
 * column is added/updated, so Zustand short-circuits and only the
 * streaming column re-renders per flush.
 */
import type { ChatMessage, Event, ModelEvent } from "@tsmono/inspect-common";

import { isModelEvent } from "./events";
import type { BranchId, Role } from "./wire";
import { useSession } from "../store/session";

export function useEvents(branch: BranchId, role: Role): Event[] {
  return useSession((s) => s.byRole[branch]?.[role] ?? EMPTY);
}

/** The single in-flight (streaming) ModelEvent for a column, if any. */
export function usePending(branch: BranchId, role: Role): ModelEvent | null {
  return useSession((s) => {
    const events = s.byRole[branch]?.[role] ?? EMPTY;
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      if (isModelEvent(ev) && ev.pending === true) return ev;
    }
    return null;
  });
}

export function useQueued(branch: BranchId, role: Role): ChatMessage[] {
  return useSession((s) => s.queued[branch]?.[role] ?? EMPTY_MSGS);
}

const EMPTY: Event[] = [];
const EMPTY_MSGS: ChatMessage[] = [];
