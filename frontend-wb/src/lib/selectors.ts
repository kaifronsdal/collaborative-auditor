/**
 * React selectors over the session store (STREAMING.md §D).
 */
import type { ChatMessage, Event, ModelEvent } from "@tsmono/inspect-common";

import { eventsByRole, isModelEvent } from "./events";
import type { BranchId, Role } from "./wire";
import { useSession } from "../store/session";

export function useEvents(branch: BranchId, role: Role): Event[] {
  return useSession((s) => eventsByRole(s)[branch]?.[role] ?? EMPTY);
}

/** The single in-flight (streaming) ModelEvent for a column, if any. */
export function usePending(branch: BranchId, role: Role): ModelEvent | null {
  const events = useEvents(branch, role);
  for (const ev of events) {
    if (isModelEvent(ev) && ev.pending === true) return ev;
  }
  return null;
}

export function useQueued(branch: BranchId, role: Role): ChatMessage[] {
  return useSession((s) => s.queued[branch]?.[role] ?? EMPTY_MSGS);
}

const EMPTY: Event[] = [];
const EMPTY_MSGS: ChatMessage[] = [];
