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
  emptyRoles,
  isModelEvent,
  resolveRole,
  type EventsByRole,
} from "../lib/events";
import type {
  BranchId, BranchMeta, CandidateBatch, Down, OrchestratorState, QueuedMap,
  Role, SavedSession, Status, TimelineMap, Up,
} from "../lib/wire";
import { DEFAULT_AUDITOR, DEFAULT_TARGET } from "../lib/presets";
import type { GenerateConfigDict } from "../components/ModelPicker";

/** Local id for a just-initiated branch/resample/edit, before `state` arrives. */
export const PENDING_BRANCH = "__pending_branch__";

/**
 * A "Recents" entry. M0 stub: populated UI-side when an audit starts (the
 * backend has no session-listing endpoint yet — see the deliverable note).
 * `id` is the branch id once known; until the `state` broadcast lands with a
 * `current`, the row is keyed by a temporary local id.
 */
export type SessionSummary = {
  id: string;
  title: string;
  updatedAt: number;
};

/** The config a branch was started with. Captured UI-side at `start` (the
 *  backend doesn't broadcast it); shown read-only in the sidebar config card. */
export type BranchConfig = {
  seed: string;
  auditor_model: string;
  target_model: string;
  max_turns?: number;
  auditor_config?: Partial<GenerateConfigDict>;
  target_config?: Partial<GenerateConfigDict>;
};

/** LLM-assisted rewrite draft. Keyed by `call_id` (auditor-side tool_call
 *  rewrite) or `message_id` (target-side message rewrite). */
export type RewriteDraft = {
  status: "pending" | "ready" | "error";
  args?: Record<string, unknown>;
  /** Target-side: the rewritten message text (staging-call content arg). */
  content?: string;
  raw?: string;
  error?: string;
};

/** Which start card `+ New audit` opens. Persisted UI-side; the backend
 *  doesn't care until a `start` / `start_orchestrator` is actually sent. */
export type Mode = "desk" | "orch";
const MODE_KEY = "workbench.mode";

function readStoredMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === "orch" ? "orch" : "desk";
  } catch {
    return "desk";
  }
}

/** Local id for the just-started Recents stub, before `current` arrives. */
const PENDING_ID = "__pending__";

/** Status precedence for optimistic-vs-backend reconciliation. `"waiting"` is
 *  orchestrator-only and never flows through this branch-status path, but the
 *  Record type demands the key. */
const STATUS_RANK: Record<Status | "null", number> = {
  null: 0, idle: 1, paused: 2, running: 3, waiting: 3, ended: 4,
};

/**
 * Reconcile an optimistic local status with an incoming backend status.
 *
 * While a pending start/branch is in flight the backend's first `state` may
 * still read null/"idle" (the branch task hasn't reached "paused" yet). Keep
 * the optimistic status unless the backend's is further along — never regress.
 */
function reconcileStatus(
  optimistic: Status | null,
  incoming: Status | null,
  hadPendingOp: boolean
): Status | null {
  if (!hadPendingOp || optimistic == null) return incoming;
  return STATUS_RANK[incoming ?? "null"] > STATUS_RANK[optimistic]
    ? incoming
    : optimistic;
}

/** Truncate seed text into a Recents-row title. */
function titleFromSeed(seed: string): string {
  const trimmed = seed.trim().replace(/\s+/g, " ");
  return trimmed.length > 42 ? `${trimmed.slice(0, 42)}…` : trimmed || "untitled audit";
}

/** Config editable in the sidebar for the next audit (or to override current). */
export type NextConfig = {
  auditor_model: string;
  target_model: string;
  auditor_config: Partial<GenerateConfigDict>;
  target_config: Partial<GenerateConfigDict>;
};

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
  /** Server-built petri timelines (`build_target_timeline`), keyed by branch/role.
   *  Event refs are UUIDs; resolve via `convertServerTimeline(tl, [...events])`. */
  timelines: TimelineMap;
  spanParent: Map<string, string | null>;
  spanRole: Map<string, [BranchId, Role]>;
  queued: QueuedMap;
  version: number;
  current: string | null;
  /**
   * True when the user has explicitly requested the StartView (clicked "+ New
   * audit"). Prevents the next backend `state` broadcast from flipping
   * `current` back and hiding the StartView. Cleared when a new `start` is
   * sent or the user switches to an existing branch.
   */
  pendingNewAudit: boolean;
  /** Lifecycle status of the current branch (idle/running/paused/ended). */
  status: Status | null;
  /** M1 orchestrator column state (M1-NOTEBOOK.md). Null until
   *  `start_orchestrator` — DeskView keeps its M0 two-column layout. */
  orchestrator: OrchestratorState | null;
  /** Orch event uuids invalidated by a `{t:"rewind"}` (M1-FEATURES §2). Events
   *  aren't removed from `byRole` (wire monotonicity); `eventsToOrchTurns`
   *  filters them. Cleared on the next full `state` (which carries per-event
   *  `data.rewound` flags instead). */
  rewound: ReadonlySet<string>;
  ws: WebSocket | null;
  /** Id of the session the current socket is for; guards idempotent connect. */
  sessionId: string | null;
  /** Branch tree metadata — keyed by branch id, populated from `state` broadcasts. */
  branches: Record<BranchId, BranchMeta>;
  /** Resample-N batches keyed by `batch_id` (RESAMPLE-N.md). */
  candidateBatches: Record<string, CandidateBatch>;
  /**
   * Recents list (STUB, M0). Most-recent first. Populated on `start`; the
   * pending entry's `id` is reconciled to the real branch id when the next
   * `state` broadcast lands with a `current`.
   */
  sessionsList: SessionSummary[];
  /** Persisted sessions from `GET /sessions` (sidebar Recents). */
  savedSessions: SavedSession[];
  /** Per-branch config captured at `start`, keyed by branch id (PENDING_ID
   *  until reconciled). Read by the sidebar config card. */
  branchConfig: Record<string, BranchConfig>;
  /** Editable config for the next audit (pre-populates StartView pickers). */
  nextConfig: NextConfig;
  /** Which start card the sidebar's MODES section has selected. Only affects
   *  what `+ New audit` / the empty StartView renders — a running session's
   *  mode is fixed by whether it has an `orchestrator`. */
  mode: Mode;

  /**
   * Stash of the real branch id before we set `current = PENDING_BRANCH`.
   * Used by the error rollback to restore `current` when a branch/resample/edit
   * fails on the backend.
   */
  prevCurrent: string | null;

  error: string | null;
  apply: (msg: Down) => void;
  connect: (sessionId: string) => void;
  disconnect: () => void;
  send: (msg: Up) => void;
  dismissError: () => void;
  /** Compose + send a `start`, and record a Recents entry for it. */
  start: (params: {
    seed: string;
    auditor_model: string;
    target_model: string;
    auditor_config?: Partial<GenerateConfigDict>;
    target_config?: Partial<GenerateConfigDict>;
  }) => void;
  /** Update the sidebar's editable next-audit config. */
  setNextConfig: (patch: Partial<NextConfig>) => void;
  /** Select which start card `+ New audit` opens; persisted to localStorage. */
  setMode: (mode: Mode) => void;
  /**
   * Return to the empty StartView without tearing down the backend branch.
   * The branch stays in the session (clicking its Recents row re-views it via
   * the live socket — `current` flips back on the next `state`).
   */
  newAudit: () => void;

  /** Fetch `GET /sessions` and populate `savedSessions`. */
  fetchSessions: () => Promise<void>;

  /** Export `branchId` (and its subtree) to a `.eval` at `path`. */
  exportBranch: (branchId: BranchId, path: string) => void;

  /** Import a `.eval` sample as a new root branch in the current session. */
  importEval: (path: string, sampleId?: string) => void;

  /**
   * Optimistically branch at `anchorId`: truncate columns to events up to the
   * clicked row, set `current = PENDING_BRANCH`, send `{t:"branch", at}`.
   */
  branchAt: (anchorId: string) => void;

  /**
   * Optimistically resample at `anchorId`: same truncation as `branchAt`, but
   * sends `{t:"resample", at}`.
   */
  resampleAt: (anchorId: string) => void;

  /**
   * Edit a target-side user/system/tool message. Backend maps it to the
   * auditor staging tool_call that produced it and replays that turn with
   * the edited content (WISHLIST 3c).
   */
  editTargetMessage: (
    messageId: string,
    role: "user" | "system" | "tool",
    content: string,
    toolCallId?: string
  ) => void;

  /** Regenerate the auditor's `turnIdx`-th response (WISHLIST 3a tape model). */
  resampleAuditor: (turnIdx: number, anchorId: string) => void;

  /** Fork the auditor conversation at `turnIdx` (same backend op as resample). */
  branchAuditor: (turnIdx: number, anchorId: string) => void;

  /** Edit an auditor tool_call's args and replay that turn (WISHLIST 3a). */
  editAuditorCall: (
    turnIdx: number,
    anchorId: string,
    callId: string,
    args: Record<string, unknown>
  ) => void;

  /** Per-call_id LLM rewrite drafts. Cleared on apply/discard. */
  rewriteDrafts: Record<string, RewriteDraft>;

  /** Ask the auditor model to rewrite a tool_call's args. Stateless draft —
   *  the result lands in `rewriteDrafts[callId]`; apply via `editAuditorCall`. */
  rewriteToolCall: (
    turnIdx: number,
    callId: string,
    instruction: string,
    selectedText?: string
  ) => void;

  /** Ask the auditor model to rewrite a *target-side* message (user/system/
   *  tool). Backend resolves the staging tool_call via `locate_staging_call`
   *  then runs the same `generate_rewrite`. Draft lands in
   *  `rewriteDrafts[messageId]`; apply via `editTargetMessage`. */
  rewriteTargetMessage: (
    messageId: string,
    role: "user" | "system" | "tool",
    instruction: string,
    selectedText?: string,
    toolCallId?: string
  ) => void;

  /** Drop a rewrite draft (discard or post-apply cleanup). */
  clearRewriteDraft: (key: string) => void;

  /** Resample-N: spawn `n` background target resamples at `anchor`.
   *  `current` is unchanged; cards fill as events stream. */
  requestCandidates: (branchId: BranchId, anchor: string, n: number) => void;

  /** Resample-N auditor variant: `n` background forks at auditor turn
   *  `turnIndex`, each stepped once for the divergent auditor turn. */
  requestCandidatesAuditor: (branchId: BranchId, turnIndex: number, n: number) => void;

  /** Adopt one candidate: cancel its siblings, switch `current` to it. */
  pickCandidate: (batchId: string, branchId: BranchId) => void;

  /** Cancel all candidates in `batchId`; the parent (original) wins. */
  dismissCandidates: (batchId: string) => void;

  /**
   * Flip `status` locally before the round-trip: play→running, pause→paused,
   * step→running, end→ended. Then send the wire command.
   */
  transport: (cmd: "play" | "pause" | "step" | "end") => void;

  /**
   * Push a message into `queued[branch][role]` immediately (ghost bubble
   * appears before the server echo), then send `{t:"inject", …}`.
   */
  inject: (branch: BranchId, role: "auditor" | "target", message: ChatMessage) => void;
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
    for (const role of ["auditor", "target"] as const) {
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
  timelines: {},
  spanParent: new Map(),
  spanRole: new Map(),
  queued: {},
  version: 0,
  current: null,
  pendingNewAudit: false,
  status: null,
  orchestrator: null,
  rewound: new Set(),
  ws: null,
  sessionId: null,
  sessionsList: [],
  savedSessions: [],
  branchConfig: {},
  branches: {},
  candidateBatches: {},
  nextConfig: {
    auditor_model: DEFAULT_AUDITOR,
    target_model: DEFAULT_TARGET,
    auditor_config: {},
    target_config: {},
  },
  mode: readStoredMode(),
  prevCurrent: null,
  error: null,
  rewriteDrafts: {},

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
          // Reconcile the pending Recents stub to the real branch id once the
          // backend assigns `current`. If the branch isn't listed yet (e.g. a
          // reconnect to a session that already had a running branch), add it.
          let sessionsList = state.sessionsList;
          let branchConfig = state.branchConfig;
          if (msg.current != null) {
            const pendingIdx = sessionsList.findIndex((s) => s.id === PENDING_ID);
            if (pendingIdx !== -1) {
              sessionsList = sessionsList.slice();
              sessionsList[pendingIdx] = {
                ...sessionsList[pendingIdx],
                id: msg.current,
              };
              if (branchConfig[PENDING_ID]) {
                const { [PENDING_ID]: pending, ...rest } = branchConfig;
                branchConfig = { ...rest, [msg.current]: pending };
              }
            } else if (!sessionsList.some((s) => s.id === msg.current)) {
              // Only add to Recents if this is a root branch (no parent).
              // Child branches (branch/resample/edit) live in the Branches tree,
              // not in the Recents list — adding them here creates phantom entries.
              const branchMeta = (msg.branches ?? {})[msg.current];
              const isRootBranch = branchMeta == null || branchMeta.parent == null;
              if (isRootBranch) {
                sessionsList = [
                  { id: msg.current, title: "audit", updatedAt: Date.now() },
                  ...sessionsList,
                ];
              }
            }
          }
          // If the user clicked "+ New audit" we keep current=null (show
          // StartView) even if the backend still reports the old branch as
          // current. The flag is cleared when they send a new `start` or
          // switch to an existing branch.
          const resolvedCurrent = state.pendingNewAudit ? null : msg.current;

          const hadPendingOp =
            state.current === PENDING_ID || state.current === PENDING_BRANCH;
          const resolvedStatus = state.pendingNewAudit
            ? null
            : reconcileStatus(state.status, msg.status, hadPendingOp);

          // The real `state` broadcast naturally supersedes any PENDING_BRANCH
          // optimistic state — `byRole` and `branches` are rebuilt from the
          // broadcast, and `prevCurrent` is cleared.
          return {
            pool,
            events,
            byRole: buildByRole(events.values(), spanParent, spanRole),
            timelines: msg.timelines ?? {},
            spanRole,
            spanParent,
            queued: msg.queued,
            current: resolvedCurrent,
            status: resolvedStatus,
            version: msg.v,
            sessionsList,
            branchConfig,
            branches: msg.branches ?? {},
            candidateBatches: msg.candidate_batches ?? {},
            orchestrator: msg.orchestrator ?? null,
            // Full snapshot supersedes the live rewound-uuid set — the backend
            // now carries per-event `data.rewound` flags for the same effect.
            rewound: new Set(),
            prevCurrent: null,
          };
        }

        case "status": {
          // Don't clobber an optimistic "paused" or "running" status set by
          // start()/transport() with a stale null from the backend if a
          // pending operation is in flight.
          const isPendingOp =
            state.current === PENDING_ID || state.current === PENDING_BRANCH;
          const resolvedStatus =
            isPendingOp && msg.status == null ? state.status : msg.status;
          return {
            status: resolvedStatus,
            version: msg.v,
            // Orchestrator status piggybacks on the same broadcast; keep the
            // existing view() snapshot but overlay the fresh status.
            ...(msg.orch_status !== undefined && state.orchestrator
              ? { orchestrator: { ...state.orchestrator, status: msg.orch_status ?? "idle" } }
              : {}),
          };
        }

        case "rewound": {
          // §2: mark every orch event at/after `from_uuid` as rewound. The
          // events stay in `byRole["orch"]["orch"]` (removing would break the
          // uuid-keyed update path); `eventsToOrchTurns` filters by this set.
          const orchEvents = state.byRole.orch?.orch ?? [];
          const idx = orchEvents.findIndex((e) => e.uuid === msg.from_uuid);
          if (idx < 0) return { version: msg.v };
          const next = new Set(state.rewound);
          for (let i = idx; i < orchEvents.length; i++) {
            const u = orchEvents[i].uuid;
            if (u != null) next.add(u);
          }
          return { rewound: next, version: msg.v };
        }

        case "notify": {
          // sys-chip pushed by a backgrounded cell's done-callback. Full
          // list is refreshed on the next `state`; append optimistically so
          // the chip renders above the next turn immediately.
          if (state.orchestrator == null) return { version: msg.v };
          return {
            version: msg.v,
            orchestrator: {
              ...state.orchestrator,
              notifications: [...state.orchestrator.notifications, msg.text],
            },
          };
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
            byRole: buildByRole(events.values(), state.spanParent, state.spanRole),            version: msg.v,
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
              : state.byRole,            queued: reconcileQueued(state.queued, ev),
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
              : state.byRole,            version: msg.v,
          };
        }

        case "queued": {
          const queued: QueuedMap = structuredClone(state.queued);
          const roles = (queued[msg.branch] ??= { auditor: [], target: [] });
          // Dedup by message id: if the inject action already pushed it
          // optimistically, don't double-push when the server echo arrives.
          if (msg.message.id == null || !roles[msg.role].some((m) => m.id === msg.message.id)) {
            roles[msg.role].push(msg.message);
          }
          return { queued, version: msg.v };
        }

        case "rewrite_draft": {
          const key = msg.call_id ?? msg.message_id;
          if (key == null) return { version: msg.v };
          const draft: RewriteDraft = msg.error
            ? { status: "error", error: msg.error }
            : { status: "ready", args: msg.args ?? {}, content: msg.content, raw: msg.raw };
          return {
            rewriteDrafts: { ...state.rewriteDrafts, [key]: draft },
            version: msg.v,
          };
        }

        case "timeline": {
          return {
            timelines: {
              ...state.timelines,
              [msg.branch]: { ...state.timelines[msg.branch], [msg.role]: msg.timeline },
            },
            version: msg.v,
          };
        }

        case "error": {
          // Roll back any optimistic branch/resample/edit: drop PENDING_BRANCH
          // from byRole and restore `current` to the previous real id.
          const hadPending =
            state.current === PENDING_BRANCH || state.byRole[PENDING_BRANCH] != null;
          if (hadPending) {
            const { [PENDING_BRANCH]: _dropped, ...byRoleWithout } = state.byRole;
            const { [PENDING_BRANCH]: _droppedB, ...branchesWithout } = state.branches;
            return {
              error: msg.message,
              version: msg.v,
              current: state.prevCurrent,
              prevCurrent: null,
              byRole: byRoleWithout,
              branches: branchesWithout,
            };
          }
          return { error: msg.message, version: msg.v };
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
    // Same-origin /ws (Vite proxies to the backend) so only one port needs
    // forwarding. Override with VITE_WS_URL for a direct connection
    // (e.g. `VITE_WS_URL=ws://localhost:8766 pnpm dev --port 5174`).
    const base =
      import.meta.env.VITE_WS_URL ??
      `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
    const next = new WebSocket(`${base}/ws/${sessionId}`);
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

  start: (params) => {
    // Record a pending Recents entry and capture the branch config.
    // The `state` broadcast that follows carries the real branch id and
    // reconciles `PENDING_ID` to it (both in sessionsList and branchConfig).
    const pendingConfig: BranchConfig = {
      seed: params.seed,
      auditor_model: params.auditor_model,
      target_model: params.target_model,
      auditor_config: params.auditor_config,
      target_config: params.target_config,
    };
    set((state) => ({
      pendingNewAudit: false,
      current: PENDING_ID,
      status: "paused",
      // Empty columns for the pending branch — the DeskView skeleton is visible
      // immediately with no spinner. The real `state` broadcast re-keys to the
      // actual branch id and populates events.
      byRole: { ...state.byRole, [PENDING_ID]: emptyRoles() },
      branches: {
        ...state.branches,
        [PENDING_ID]: {
          parent: null,
          branched_at: null,
          branched_at_turn: null,
          status: "paused" as const,
          seed: params.seed,
        },
      },
      sessionsList: [
        { id: PENDING_ID, title: titleFromSeed(params.seed), updatedAt: Date.now() },
        // a single pending stub at a time — drop any stale one.
        ...state.sessionsList.filter((s) => s.id !== PENDING_ID),
      ],
      branchConfig: {
        // drop any stale pending entry, then add the new one
        ...Object.fromEntries(
          Object.entries(state.branchConfig).filter(([k]) => k !== PENDING_ID)
        ),
        [PENDING_ID]: pendingConfig,
      },
    }));
    get().send({
      t: "start",
      seed: params.seed,
      auditor_model: params.auditor_model,
      target_model: params.target_model,
      ...(params.auditor_config && Object.keys(params.auditor_config).length > 0
        ? { auditor_config: params.auditor_config as Record<string, unknown> }
        : {}),
      ...(params.target_config && Object.keys(params.target_config).length > 0
        ? { target_config: params.target_config as Record<string, unknown> }
        : {}),
    });
  },

  setNextConfig: (patch) => {
    set((state) => ({ nextConfig: { ...state.nextConfig, ...patch } }));
  },

  setMode: (mode) => {
    try { localStorage.setItem(MODE_KEY, mode); } catch { /* ignore */ }
    set({ mode });
  },

  fetchSessions: async () => {
    try {
      const res = await fetch("/sessions");
      if (!res.ok || !res.headers.get("content-type")?.includes("json")) return;
      set({ savedSessions: (await res.json()) as SavedSession[] });
    } catch {
      // backend down / not proxied in this dev setup — recents stays empty
    }
  },

  exportBranch: (branchId, path) => {
    get().send({ t: "export", branch: branchId, path });
  },

  importEval: (path, sampleId) => {
    // Optimistic pending-branch — the `state` broadcast that follows the
    // import's `_register_and_spawn` re-keys it to the real id.
    set((state) => ({
      pendingNewAudit: false,
      prevCurrent: state.current,
      current: PENDING_BRANCH,
      status: "paused",
      byRole: { ...state.byRole, [PENDING_BRANCH]: emptyRoles() },
    }));
    get().send({
      t: "import",
      path,
      ...(sampleId != null ? { sample_id: sampleId } : {}),
    });
  },

  newAudit: () => {
    // Back to the empty StartView. The backend branch is untouched; clicking
    // its Recents row re-views it (same socket, `current` flips back).
    // `pendingNewAudit` prevents the next backend `state` broadcast from
    // overwriting `current` back to the old branch.
    set({ current: null, pendingNewAudit: true });
  },

  dismissError: () => set({ error: null }),

  branchAt: (anchorId) => _pendingChild(set, get, anchorId, { t: "branch", at: anchorId }),
  resampleAt: (anchorId) => _pendingChild(set, get, anchorId, { t: "resample", at: anchorId }),

  editTargetMessage: (messageId, role, content, toolCallId) => {
    const current = get().current;
    if (current == null) return;
    _pendingChild(set, get, messageId, {
      t: "edit_target_message",
      branch: current,
      message_id: messageId,
      role,
      content,
      ...(toolCallId != null ? { tool_call_id: toolCallId } : {}),
    });
  },

  resampleAuditor: (turnIdx, anchorId) => {
    const current = get().current;
    if (current == null) return;
    _pendingChild(set, get, anchorId, {
      t: "resample_auditor",
      branch: current,
      turn_index: turnIdx,
    });
  },

  branchAuditor: (turnIdx, anchorId) => {
    const current = get().current;
    if (current == null) return;
    _pendingChild(set, get, anchorId, {
      t: "branch_auditor",
      branch: current,
      turn_index: turnIdx,
    });
  },

  editAuditorCall: (turnIdx, anchorId, callId, args) => {
    const current = get().current;
    if (current == null) return;
    _pendingChild(set, get, anchorId, {
      t: "edit_auditor_call",
      branch: current,
      turn_index: turnIdx,
      call_id: callId,
      args,
    });
  },

  rewriteToolCall: (turnIdx, callId, instruction, selectedText) => {
    const current = get().current;
    if (current == null) return;
    set((state) => ({
      rewriteDrafts: { ...state.rewriteDrafts, [callId]: { status: "pending" } },
    }));
    get().send({
      t: "rewrite_tool_call",
      branch: current,
      turn_index: turnIdx,
      call_id: callId,
      instruction,
      ...(selectedText ? { selected_text: selectedText } : {}),
    });
  },

  rewriteTargetMessage: (messageId, role, instruction, selectedText, toolCallId) => {
    const current = get().current;
    if (current == null) return;
    set((state) => ({
      rewriteDrafts: { ...state.rewriteDrafts, [messageId]: { status: "pending" } },
    }));
    get().send({
      t: "rewrite_target_message",
      branch: current,
      message_id: messageId,
      role,
      instruction,
      ...(selectedText ? { selected_text: selectedText } : {}),
      ...(toolCallId != null ? { tool_call_id: toolCallId } : {}),
    });
  },

  clearRewriteDraft: (key) => {
    set((state) => {
      const { [key]: _dropped, ...rest } = state.rewriteDrafts;
      return { rewriteDrafts: rest };
    });
  },

  requestCandidates: (branchId, anchor, n) => {
    get().send({ t: "candidates", branch: branchId, at: anchor, n });
  },

  requestCandidatesAuditor: (branchId, turnIndex, n) => {
    get().send({ t: "candidates_auditor", branch: branchId, turn_index: turnIndex, n });
  },

  pickCandidate: (batchId, branchId) => {
    // Optimistic: flip `current` immediately so the desk follows the pick;
    // the backend `state` confirms (and ships the cancelled siblings'
    // `status: "ended"`). `pendingNewAudit` cleared so the broadcast isn't
    // suppressed by an in-flight new-audit click.
    set({ current: branchId, pendingNewAudit: false });
    get().send({ t: "pick_candidate", batch: batchId, branch: branchId });
  },

  dismissCandidates: (batchId) => {
    get().send({ t: "dismiss_candidates", batch: batchId });
  },

  transport: (cmd) => {
    const statusMap: Record<"play" | "pause" | "step" | "end", Status> = {
      play: "running",
      pause: "paused",
      step: "running",
      end: "ended",
    };
    set({ status: statusMap[cmd] });
    get().send({ t: cmd });
  },

  inject: (branch, role, message) => {
    set((state) => {
      const queued: QueuedMap = structuredClone(state.queued);
      const roles = (queued[branch] ??= { auditor: [], target: [] });
      // Optimistic push — dedup on id to avoid double-entry when server echoes.
      if (message.id == null || !roles[role].some((m) => m.id === message.id)) {
        roles[role].push(message);
      }
      return { queued };
    });
    get().send({ t: "inject", branch, role, message });
  },
}));

/**
 * Shared body for `branchAt` / `resampleAt` / edit ops: optimistically install
 * a `PENDING_BRANCH` snapshot truncated at `anchorId`, then send `cmd`. The
 * backend's `state` broadcast replaces the pending entry with the real branch.
 */
function _pendingChild(
  set: (partial: Partial<SessionState>) => void,
  get: () => SessionState,
  anchorId: string,
  cmd: Up
): void {
  const state = get();
  const current = state.current;
  if (current == null) return;
  const truncated = _truncateByRole(state, current, anchorId);
  set({
    prevCurrent: current,
    current: PENDING_BRANCH,
    status: "paused",
    byRole: { ...state.byRole, [PENDING_BRANCH]: truncated },
    branches: {
      ...state.branches,
      [PENDING_BRANCH]: {
        parent: current,
        branched_at: anchorId,
        branched_at_turn: null,
        status: "paused",
        seed: state.branches[current]?.seed ?? "",
      },
    },
  });
  get().send(cmd);
}

/**
 * Build a truncated `{auditor, target}` snapshot of the current branch's
 * events, cut at the event whose output message id is `anchorId`.
 *
 * Strategy: find the anchor event in either role column. For each role, keep
 * all events whose array index is ≤ the anchor event's index in that role's
 * column. If the anchor doesn't appear in a role, keep all events for that
 * role (it was a cross-role anchor).
 */
function _truncateByRole(
  state: SessionState,
  branchId: BranchId,
  anchorId: string
): Record<Role, Event[]> {
  const roleBuckets = state.byRole[branchId] ?? emptyRoles();
  const roles = ["auditor", "target"] as const;

  // Find the anchor index in each role.
  const anchorIdx = { auditor: -1, target: -1 };
  for (const role of roles) {
    const events = roleBuckets[role];
    for (let i = 0; i < events.length; i++) {
      const ev = events[i];
      if (isModelEvent(ev)) {
        const msgId = ev.output.choices[0]?.message.id;
        if (msgId === anchorId) { anchorIdx[role] = i; break; }
      }
    }
  }

  const result = emptyRoles();
  for (const role of roles) {
    const events = roleBuckets[role];
    const cutoff = anchorIdx[role];
    // If the anchor was found in this role, include up to and including it.
    // If not found, include all (the anchor lives in the other role).
    result[role] = cutoff >= 0 ? events.slice(0, cutoff + 1) : events.slice();
  }
  return result;
}

/** Selector hook: the rewrite draft for one tool_call, or undefined. */
export const useRewriteDraft = (callId: string): RewriteDraft | undefined =>
  useSession((s) => s.rewriteDrafts[callId]);
