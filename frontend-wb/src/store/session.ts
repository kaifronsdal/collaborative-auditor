/**
 * Session store (STREAMING.md §D) — a Zustand reducer over the wire protocol.
 *
 * The store mirrors the backend's dumb event pipe: a content-deduped message
 * `pool`, an event map keyed by uuid (with each `ModelEvent`'s `input_refs`
 * already resolved against the pool via `expandEvents`), span routing tables,
 * and per-branch queued (injected) messages.
 */
import type {
  ChatMessage,
  Event,
  EventsData,
} from "@tsmono/inspect-common";
import { expandEvents } from "@tsmono/inspect-common/utils";
import { create } from "zustand";

import { isModelEvent } from "../lib/events";
import type { BranchId, Down, QueuedMap, Role, Up } from "../lib/wire";

export type SessionState = {
  pool: ChatMessage[];
  events: Map<string, Event>;
  spanParent: Map<string, string | null>;
  spanRole: Map<string, [BranchId, Role]>;
  queued: QueuedMap;
  version: number;
  current: string | null;
  ws: WebSocket | null;

  apply: (msg: Down) => void;
  connect: (sessionId: string) => void;
  send: (msg: Up) => void;
};

/** Resolve a single ModelEvent's `input_refs` against the pool. */
function resolveOne(ev: Event, pool: ChatMessage[]): Event {
  if (!isModelEvent(ev)) return ev;
  const data: EventsData = { messages: pool, calls: [] };
  return expandEvents([ev], data)[0];
}

/** Re-resolve every ModelEvent in the map against the current pool. */
function resolveAll(
  events: Map<string, Event>,
  pool: ChatMessage[]
): Map<string, Event> {
  const next = new Map<string, Event>();
  for (const [uuid, ev] of events) next.set(uuid, resolveOne(ev, pool));
  return next;
}

/**
 * Every event has `uuid` / `span_id` populated at runtime, but the generated
 * types mark them optional (inspect serializes with exclude_none). These
 * helpers pin the non-null runtime contract at the boundary.
 */
function uuidOf(ev: Event): string {
  if (ev.uuid == null) throw new Error("event missing uuid");
  return ev.uuid;
}

/**
 * Drop any queued message that has now landed in `ev.input` — the injected id
 * survives through `state.messages → ModelEvent.input → pool`, so once it shows
 * up in a generated event's resolved input the ghost bubble reconciles away.
 */
function reconcileQueued(queued: QueuedMap, ev: Event): QueuedMap {
  if (!isModelEvent(ev)) return queued;
  const inputIds = new Set(
    ev.input.map((m) => m.id).filter((id): id is string => id != null)
  );
  if (inputIds.size === 0) return queued;
  let changed = false;
  const next: QueuedMap = {};
  for (const [branch, roles] of Object.entries(queued)) {
    next[branch] = { auditor: [], target: [] };
    for (const role of ["auditor", "target"] as Role[]) {
      const kept = roles[role].filter((m) => m.id == null || !inputIds.has(m.id));
      if (kept.length !== roles[role].length) changed = true;
      next[branch][role] = kept;
    }
  }
  return changed ? next : queued;
}

export const useSession = create<SessionState>((set, get) => ({
  pool: [],
  events: new Map(),
  spanParent: new Map(),
  spanRole: new Map(),
  queued: {},
  version: 0,
  current: null,
  ws: null,

  apply: (msg: Down) =>
    set((state) => {
      switch (msg.t) {
        case "state": {
          const pool = msg.pool;
          const events = new Map<string, Event>();
          for (const ev of expandEvents(msg.events, { messages: pool, calls: [] })) {
            events.set(uuidOf(ev), ev);
          }
          const spanRole = new Map<string, [BranchId, Role]>(
            Object.entries(msg.span_role)
          );
          // Recover span parentage from any SpanBegin events in the snapshot.
          const spanParent = new Map<string, string | null>();
          for (const ev of msg.events) {
            if (ev.event === "span_begin") spanParent.set(ev.id, ev.parent_id ?? null);
          }
          return {
            pool,
            events,
            spanRole,
            spanParent,
            queued: msg.queued,
            current: msg.current,
            version: msg.v,
          };
        }

        case "pool": {
          const pool = state.pool.slice();
          // `from` is the high-water-mark; entries append at that index.
          pool.length = msg.from;
          pool.push(...msg.entries);
          // Growing the pool can change earlier ModelEvents' resolution.
          return { pool, events: resolveAll(state.events, pool), version: msg.v };
        }

        case "event": {
          const ev = resolveOne(msg.event, state.pool);
          const events = new Map(state.events);
          events.set(uuidOf(ev), ev);
          const spanParent =
            ev.event === "span_begin"
              ? new Map(state.spanParent).set(ev.id, ev.parent_id ?? null)
              : state.spanParent;
          return {
            events,
            spanParent,
            queued: reconcileQueued(state.queued, ev),
            version: msg.v,
          };
        }

        case "update": {
          const ev = resolveOne(msg.event, state.pool);
          const events = new Map(state.events);
          events.set(uuidOf(ev), ev);
          return { events, version: msg.v };
        }

        case "queued": {
          const queued: QueuedMap = structuredClone(state.queued);
          (queued[msg.branch] ??= { auditor: [], target: [] })[msg.role].push(
            msg.message
          );
          return { queued, version: msg.v };
        }
      }
    }),

  connect: (sessionId: string) => {
    get().ws?.close();
    const ws = new WebSocket(`ws://localhost:8765/ws/${sessionId}`);
    ws.onmessage = (e) => get().apply(JSON.parse(e.data) as Down);
    set({ ws });
  },

  send: (msg: Up) => {
    const ws = get().ws;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  },
}));
