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

import {
  assignByRole,
  buildByRole,
  isModelEvent,
  resolveRole,
  type EventsByRole,
} from "../lib/events";
import type { BranchId, Down, QueuedMap, Role, Status, Up } from "../lib/wire";

export type SessionState = {
  pool: ChatMessage[];
  events: Map<string, Event>;
  /**
   * Events bucketed by `[branch][role]`, maintained incrementally by the
   * reducer. Selectors read this directly (O(1)); per-column arrays keep
   * their reference across updates that don't touch that column, so
   * Zustand's `Object.is` short-circuits and only the streaming column
   * re-renders per flush.
   */
  byRole: EventsByRole;
  spanParent: Map<string, string | null>;
  spanRole: Map<string, [BranchId, Role]>;
  queued: QueuedMap;
  version: number;
  current: string | null;
  /** Lifecycle status of the current branch (idle/running/paused/ended). */
  status: Status | null;
  ws: WebSocket | null;
  /** Id of the session the current socket is for; guards idempotent connect. */
  sessionId: string | null;

  apply: (msg: Down) => void;
  connect: (sessionId: string) => void;
  disconnect: () => void;
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
 * Every event has `uuid` populated at runtime, but the generated type is
 * `uuid?: string | null` (the OpenAPI schema marks it optional+nullable
 * because the Pydantic field defaults to `None` and is filled in
 * `model_post_init`). Pin the non-null runtime contract at the ingest
 * boundary so downstream code uses `string`.
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
  byRole: {},
  spanParent: new Map(),
  spanRole: new Map(),
  queued: {},
  version: 0,
  current: null,
  status: null,
  ws: null,
  sessionId: null,

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
            byRole: buildByRole(events.values(), spanParent, spanRole),
            spanRole,
            spanParent,
            queued: msg.queued,
            current: msg.current,
            status: msg.status,
            version: msg.v,
          };
        }

        case "status": {
          return { status: msg.status, version: msg.v };
        }

        case "pool": {
          const pool = state.pool.slice();
          // `from` is the high-water-mark; entries append at that index.
          pool.length = msg.from;
          pool.push(...msg.entries);
          // Growing the pool can change earlier ModelEvents' resolution.
          const events = resolveAll(state.events, pool);
          return {
            pool,
            events,
            byRole: buildByRole(events.values(), state.spanParent, state.spanRole),
            version: msg.v,
          };
        }

        case "event": {
          const ev = resolveOne(msg.event, state.pool);
          const uuid = uuidOf(ev);
          const events = new Map(state.events);
          events.set(uuid, ev);
          const spanParent =
            ev.event === "span_begin"
              ? new Map(state.spanParent).set(ev.id, ev.parent_id ?? null)
              : state.spanParent;
          const role = resolveRole(ev.span_id, spanParent, state.spanRole);
          return {
            events,
            spanParent,
            byRole: role
              ? assignByRole(state.byRole, role[0], role[1], ev, undefined)
              : state.byRole,
            queued: reconcileQueued(state.queued, ev),
            version: msg.v,
          };
        }

        case "update": {
          const ev = resolveOne(msg.event, state.pool);
          const uuid = uuidOf(ev);
          const prev = state.events.get(uuid);
          const events = new Map(state.events);
          events.set(uuid, ev);
          const role = resolveRole(ev.span_id, state.spanParent, state.spanRole);
          return {
            events,
            byRole: role
              ? assignByRole(state.byRole, role[0], role[1], ev, prev)
              : state.byRole,
            version: msg.v,
          };
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
    // Idempotent: React 18 StrictMode mounts effects twice in dev, so guard
    // against opening a second socket for the same session (the duplicate would
    // race the first on `start` and leave a dangling connection server-side).
    const { ws, sessionId: cur } = get();
    if (
      ws &&
      cur === sessionId &&
      (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)
    ) {
      return;
    }
    ws?.close();
    const next = new WebSocket(`ws://localhost:8765/ws/${sessionId}`);
    next.onmessage = (e) => get().apply(JSON.parse(e.data) as Down);
    next.onclose = () => {
      // Clear only if this is still the live socket (a newer connect may have
      // replaced it). Lets a fresh connect re-open cleanly.
      if (get().ws === next) set({ ws: null, sessionId: null });
    };
    set({ ws: next, sessionId });
  },

  disconnect: () => {
    const ws = get().ws;
    if (ws) {
      ws.onclose = null; // avoid the handler racing our explicit clear
      ws.close();
    }
    set({ ws: null, sessionId: null });
  },

  send: (msg: Up) => {
    const ws = get().ws;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  },
}));
