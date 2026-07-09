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
import type {
  BranchId, BranchMeta, CandidateBatch, Down, OrchestratorState, QueuedMap,
  Role, SavedSession, Status, TimelineMap, Up,
} from "../lib/wire";
import { DEFAULT_AUDITOR, DEFAULT_TARGET } from "../lib/presets";
import type { GenerateConfigDict } from "../components/ModelPicker";

/** A2: an in-flight `Up` command awaiting `{t:"ack"}` from the server. */
export type PendingCmd = Up & { req_id: string };

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
const NEXT_CONFIG_KEY = "workbench.nextConfig";
const pinsKey = (sid: string): string => `workbench.pins.${sid}`;

/** P3 annotation-queue label. A labelled pin is a triage decision; a plain
 *  (unlabelled) pin is just a bookmark. */
export type PinLabel = "confirmed" | "false-positive" | "interesting" | "needs-review";

/** Label → icon/title map. Exported for the row button + dropdown
 *  (ProgressCard) and the sidebar group headers — keeps the four-way switch
 *  in one place. Colors live in `styles.css` as `.pin-label-{label}`. */
export const PIN_LABELS: Record<PinLabel, { icon: string; title: string }> = {
  "confirmed":      { icon: "bi-check-circle",    title: "Confirmed" },
  "false-positive": { icon: "bi-x-circle",        title: "False positive" },
  "interesting":    { icon: "bi-lightbulb",       title: "Interesting" },
  "needs-review":   { icon: "bi-question-circle", title: "Needs review" },
};

/** P2 pin/bookmark: a starred sample row. `log` is the finished `.eval` path
 *  (so a pin can always be re-opened via `{t:"import"}`); `at` is the
 *  Date.now() the pin was created. Persisted per-session to localStorage —
 *  server-side persist is P3. */
export type Pin = {
  log: string;
  sample_id: string;
  note?: string;
  /** P3 annotation label — absent = plain star bookmark. */
  label?: PinLabel;
  at: number;
};

function readStoredMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === "orch" ? "orch" : "desk";
  } catch {
    return "desk";
  }
}

const DEFAULT_NEXT_CONFIG: NextConfig = {
  auditor_model: DEFAULT_AUDITOR,
  target_model: DEFAULT_TARGET,
  auditor_config: {},
  target_config: {},
};

/** P2-persist: `nextConfig` survives reload. Merge over defaults so new keys
 *  added later aren't `undefined` on old stored blobs. */
function readStoredNextConfig(): NextConfig {
  try {
    const raw = localStorage.getItem(NEXT_CONFIG_KEY);
    if (!raw) return DEFAULT_NEXT_CONFIG;
    return { ...DEFAULT_NEXT_CONFIG, ...(JSON.parse(raw) as Partial<NextConfig>) };
  } catch {
    return DEFAULT_NEXT_CONFIG;
  }
}

function readStoredPins(sid: string): Pin[] {
  try {
    const raw = localStorage.getItem(pinsKey(sid));
    return raw ? (JSON.parse(raw) as Pin[]) : [];
  } catch {
    return [];
  }
}

/** Write-through helper for `togglePin`/`setLabel`. Returns the `set()` patch. */
function persistPins(sid: string | null, pins: Pin[]): { pins: Pin[] } {
  if (sid != null) {
    try { localStorage.setItem(pinsKey(sid), JSON.stringify(pins)); } catch { /* ignore */ }
  }
  return { pins };
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
  /** Which column of the current branch is mid-generate (RACE-FIXES.md R3).
   *  `null` = neither; `undefined` = backend didn't send it (fall back to
   *  `status === "running"` for the shimmer gate). */
  generating: "auditor" | "target" | null | undefined;
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
  /** STRESS-V2 P2: `onclose` scheduled an auto-reconnect and it hasn't opened
   *  yet. Drives a "reconnecting…" banner; cleared on `onopen`/`disconnect`. */
  reconnecting: boolean;
  /**
   * A2 (ARCHITECTURE-RACES.md): every `send()` appends `{...msg, req_id}`
   * here; `case "ack"` filters it. `useIsPending(pred)` reads this — one
   * hook replaces the nine per-component `useState` guards R1 introduced.
   * Reset on `connect()` / `case "state"` so a WS drop between send and
   * ack doesn't leave a button disabled forever.
   */
  pending: PendingCmd[];
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
  /** Per-branch config captured at `start`, keyed by branch id. Read by the
   *  sidebar config card. F3: currently unpopulated — the PENDING_ID re-key
   *  path is gone and `branch_created` doesn't yet write it; `DeskView`
   *  falls back to `branches[current].seed` from `msg.meta`. */
  branchConfig: Record<string, BranchConfig>;
  /** Editable config for the next audit (pre-populates StartView pickers). */
  nextConfig: NextConfig;
  /** Which start card the sidebar's MODES section has selected. Only affects
   *  what `+ New audit` / the empty StartView renders — a running session's
   *  mode is fixed by whether it has an `orchestrator`. */
  mode: Mode;
  /** P2 pin/bookmark: starred sample rows (most-recent first). Loaded from
   *  `localStorage["workbench.pins.{sessionId}"]` on `connect()`. */
  pins: Pin[];

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
    auditor_model_args?: Record<string, unknown>;
    target_model_args?: Record<string, unknown>;
    live_scanners?: string[];
  }) => void;
  /** Update the sidebar's editable next-audit config. */
  setNextConfig: (patch: Partial<NextConfig>) => void;
  /** Toggle a sample-row bookmark. No `label`: add if absent, remove if
   *  present (plain star toggle). With `label`: add-or-relabel — an existing
   *  pin's label is updated in place (never removed). Writes through to
   *  localStorage. */
  togglePin: (log: string, sample_id: string, label?: PinLabel) => void;
  /** Set (or clear, with `null`) an existing pin's annotation label. No-op if
   *  the pin isn't present — labelling never implicitly creates a pin. */
  setLabel: (log: string, sample_id: string, label: PinLabel | null) => void;
  /** Select which start card `+ New audit` opens; persisted to localStorage. */
  setMode: (mode: Mode) => void;
  /**
   * Return to the empty StartView without tearing down the backend branch.
   * The branch stays in the session (clicking its Recents row re-views it via
   * the live socket — `current` flips back on the next `state`).
   */
  newAudit: () => void;

  /** Switch the desk to `id`: optimistically flip `current` (so highlight
   *  responds instantly), clear `pendingNewAudit`, send `{t:"switch"}`. */
  switchBranch: (id: BranchId) => void;

  /** Fetch `GET /sessions` and populate `savedSessions`. */
  fetchSessions: () => Promise<void>;

  /** Export `branchId` (and its subtree) to a `.eval` at `path`. */
  exportBranch: (branchId: BranchId, path: string) => void;

  /** Import a `.eval` sample as a new root branch in the current session. */
  importEval: (path: string, sampleId?: string) => void;

  /** Send `{t:"branch", at}`. F3: overlay-only — `useTruncatedEvents` derives
   *  the visual cut from `pending[]`; `current`/`byRole` are untouched. */
  branchAt: (anchorId: string) => void;

  /** Send `{t:"resample", at}`. Same overlay-only truncation as `branchAt`. */
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

  /** Remove a queued message before the next turn consumes it. Optimistically
   *  drops it from `queued[branch][role]` (ghost bubble disappears
   *  immediately), then sends `{t:"unqueue", …}`. */
  unqueue: (branch: BranchId, role: "auditor" | "target", messageId: string) => void;

  /** One-shot handoff into the auditor composer: when non-null, `DeskView`
   *  picks it up into the textarea and clears it. Set by the queued-bubble
   *  edit action (unqueue → prefill composer with the old text). */
  composerDraft: string | null;
  setComposerDraft: (text: string | null) => void;
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
      // R4 gap #7: backend `Branch.queued` no longer carries a `"target"`
      // key, so a `{t:"state"}` snapshot may ship `{auditor: […]}` only.
      const before = roles[role] ?? [];
      const kept = before.filter((m) => m.id == null || !inputIds.has(m.id));
      if (kept.length !== before.length) changed = true;
      next[branch][role] = kept;
    }
  }
  return changed ? next : queued;
}

/** Any `Down` variant except the `{t:"batch"}` envelope itself. */
type DownOp = Exclude<Down, { t: "batch" }>;

/** A3 version guard (ARCHITECTURE-RACES.md, adversarial-review caveat #5):
 *  frame types that carry structural state and must arrive in `v`-order.
 *  A stale one is dropped. Legacy singleton ops (`event`/`update`/`pool`/
 *  `timeline`) only reach the wire inside `{t:"batch"}` post-A3 so are
 *  covered by the batch's `v`; sideband broadcasts (`error`/`notify`/
 *  `queued`/…) are idempotent or overlay-only and may legitimately race
 *  the drain queue. STRESS-V2 H5b: `status` writes `version: msg.v` and
 *  goes through the drain queue too, so a stale one (post-H5a resync, or
 *  under `WORKBENCH_BROADCAST_DELAY_MS` reorder) must be dropped rather
 *  than rewind `version` and cause the guard to reject the next batch. */
/** STRESS-V2 P2 auto-reconnect: exponential backoff (1s → 2s → … → 30s cap),
 *  reset on the next successful `onopen`. Module-level (not store state) —
 *  one WS at a time, and the setTimeout closure needs to read the *current*
 *  value across `connect()` calls. */
let reconnectBackoff = 1000;
const RECONNECT_BACKOFF_MAX = 30000;

const GUARDED: ReadonlySet<Down["t"]> = new Set([
  "state", "batch", "branch_created", "current", "batch_resolved", "orch",
  "queued_consumed", "status",
]);

/**
 * Pure reducer for a single wire message. Returns the `Partial<SessionState>`
 * patch for `msg`; `apply()` folds a `{t:"batch"}`'s `ops[]` through this so
 * pool + event + timeline land in one Zustand `set()` (A3-batch,
 * ARCHITECTURE-RACES.md — closes the ghost-gap one-frame race).
 */
function reduceOne(state: SessionState, msg: DownOp): Partial<SessionState> {
  switch (msg.t) {
    case "state": {
      // A3-typed-deltas: `{t:"state"}` is now CONNECT-ONLY (`push_full_
      // state`). The six mid-session broadcast sites (`_register_and_
      // spawn`, `_candidates`, `pick_candidate`, `dismiss_candidates`,
      // `switch`, `start_orchestrator`) ship narrow typed deltas instead.
      // On connect there is no optimistic client state to reconcile, so
      // the mid-session-clobber guards (`pendingNewAudit`/`reconcileStatus`
      // /`STATUS_RANK`/sentinel re-key) that used to live here are gone.
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
        timelines: msg.timelines ?? {},
        spanRole,
        spanParent,
        queued: msg.queued,
        current: msg.current,
        status: msg.status,
        generating: msg.generating,
        version: msg.v,
        branches: msg.branches ?? {},
        candidateBatches: msg.candidate_batches ?? {},
        orchestrator: msg.orchestrator ?? null,
        // Full snapshot supersedes the live rewound-uuid set — the backend
        // now carries per-event `data.rewound` flags for the same effect.
        rewound: new Set(),
        // Connect-time reset — same principle as A2's "`connect()` must
        // reset `pending: []`" (a pre-reconnect in-flight cmd is stale).
        pending: [],
      };
    }

    case "ack": {
      // A2: the server has finished handling `req_id`; drop it from the
      // in-flight overlay. `useIsPending` subscribers re-render (boolean
      // flips) and re-enable their button.
      return { pending: state.pending.filter((c) => c.req_id !== msg.req_id) };
    }

    case "branch_created": {
      // A3-typed-deltas: a new `Branch` was registered. Merge its
      // `span_role` entries and `BranchMeta`; adopt the (possibly-
      // repointed) `current`. Sent BEFORE the branch's `run()` spawns, so
      // its events find `spanRole` populated — but re-scan `state.events`
      // for any that raced in and re-bucket them (defence-in-depth for
      // adversarial-review caveat #1).
      const spanRole = new Map(state.spanRole);
      for (const [sid, br] of Object.entries(msg.span_role_delta)) {
        spanRole.set(sid, br);
      }
      let byRole = state.byRole;
      for (const ev of state.events.values()) {
        const hit = ev.span_id != null ? msg.span_role_delta[ev.span_id] : undefined;
        if (hit) byRole = assignByRole(byRole, hit[0], hit[1], ev, undefined);
      }
      return {
        spanRole,
        byRole,
        branches: { ...state.branches, [msg.id]: msg.meta },
        // A1-b-wide: point the new branch's slot at the session-wide tree
        // so its `SwimlaneColumn` renders the shared target prefix before
        // the first `{t:"timeline"}` for it lands (chaos s4).
        timelines: {
          ...state.timelines,
          [msg.id]: Object.values(state.timelines)[0] ?? {},
        },
        current: msg.current,
        pendingNewAudit: false,
        version: msg.v,
      };
    }

    case "current": {
      return { current: msg.branch, version: msg.v };
    }

    case "batch_resolved": {
      const prev = state.candidateBatches[msg.batch];
      const candidateBatches = {
        ...state.candidateBatches,
        [msg.batch]: { ...(prev ?? {} as CandidateBatch), picked: msg.picked },
      };
      // adversarial-review caveat #3: flip cancelled siblings' status so
      // the sidebar doesn't show them as still running.
      let branches = state.branches;
      for (const id of msg.ended) {
        if (branches[id]) {
          branches = { ...branches, [id]: { ...branches[id], status: "ended" } };
        }
      }
      return { candidateBatches, branches, version: msg.v };
    }

    case "orch": {
      // adversarial-review caveat #4: orch registers `("orch","orch")` in
      // `span_role` too — merge it so `resolveRole` routes orch events.
      // A4-partial: `Orchestrator.dirty()` reuses this variant for
      // process-only deltas (bg_jobs / bg_cells / run_log_dirs) with an
      // empty `span_role_delta` and `notifications` STRIPPED (it's on the
      // append-only `{t:"notify"}` path — A4 mis-partition #2). Merge (not
      // wholesale-replace) so a `dirty()` doesn't wipe those chips; the
      // initial `start_orchestrator` push carries `notifications: []` and
      // wins via spread order.
      const spanRole = new Map(state.spanRole);
      for (const [sid, br] of Object.entries(msg.span_role_delta)) {
        spanRole.set(sid, br);
      }
      return {
        orchestrator:
          state.orchestrator == null
            ? msg.state
            : { ...state.orchestrator, ...msg.state },
        spanRole,
        version: msg.v,
      };
    }

    case "l1_spans": {
      // F2 (A1-b-wide follow-up): a live L1 rollback grew this branch's
      // target-`History` tree. Refresh `branches[bid].l1_spans` so
      // `SwimlaneColumn.defaultKey` picks the post-rollback lane without
      // waiting for the next full `state`.
      const meta = state.branches[msg.branch];
      if (meta == null) return { version: msg.v };
      return {
        branches: {
          ...state.branches,
          [msg.branch]: { ...meta, l1_spans: msg.l1_spans },
        },
        version: msg.v,
      };
    }

    case "queued_consumed": {
      // Drop the consumed injected-message ids from `queued[branch].
      // auditor` — the backend-authoritative counterpart to
      // `reconcileQueued`'s `ModelEvent.input`-scan (which stays as
      // belt-and-suspenders until A2 deletes it).
      const roles = state.queued[msg.branch];
      if (roles == null) return { version: msg.v };
      const drop = new Set(msg.ids);
      const kept = roles.auditor.filter((m) => m.id == null || !drop.has(m.id));
      if (kept.length === roles.auditor.length) return { version: msg.v };
      return {
        queued: {
          ...state.queued,
          [msg.branch]: { ...roles, auditor: kept },
        },
        version: msg.v,
      };
    }

    case "status": {
      // F3: the stale-null guard is gone — `start()`/`_pendingChild` no
      // longer set an optimistic status for it to clobber.
      return {
        status: msg.status,
        generating: msg.generating,
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
        // R4 gap #2: the pending ModelEvent's terminal update carries
        // the resolved `input` (with the injected id) — reconcile here
        // too, not just on the initial `{t:"event"}`.
        queued: reconcileQueued(state.queued, ev),
        version: msg.v,
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

    case "unqueued": {
      const roles = state.queued[msg.branch];
      if (roles == null) return { version: msg.v };
      const kept = roles[msg.role].filter((m) => m.id !== msg.message_id);
      if (kept.length === roles[msg.role].length) return { version: msg.v };
      return {
        queued: {
          ...state.queued,
          [msg.branch]: { ...roles, [msg.role]: kept },
        },
        version: msg.v,
      };
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
      // A1-b-wide: both auditor and target timelines are session-wide
      // trees. Every branch key points to the same slot object so
      // `useSwimlanes(anyBranch, role)` resolves the identical tree
      // (session-scoped storage without touching `selectors.ts`).
      const slot = {
        ...(Object.values(state.timelines)[0] ?? {}),
        [msg.role]: msg.timeline,
      };
      const timelines: TimelineMap = { [msg.branch]: slot };
      for (const bid of Object.keys(state.branches)) timelines[bid] = slot;
      return { timelines, version: msg.v };
    }

    case "forked": {
      // P2 session fork: server has closed the parent (this session's
      // pool/events/byRole are now stale) and registered a fresh Session
      // under `msg.session_id`. Navigate — same hard-reload path as the
      // sidebar's `openSession`, so `App.connect()` opens a clean socket
      // and the first `push_full_state` seeds store from scratch.
      // `typeof` guard: the vitest node env has no `location`.
      if (typeof location !== "undefined") {
        const url = new URL(location.href);
        url.searchParams.set("session", msg.session_id);
        location.assign(url.toString());
      }
      return {};
    }

    case "error": {
      // F3: no optimistic `current`/`byRole` mutation to roll back — the
      // fork overlay is derived from `pending[]` and clears on `{t:"ack"}`.
      return { error: msg.message, version: msg.v };
    }
  }
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
  generating: undefined,
  orchestrator: null,
  rewound: new Set(),
  ws: null,
  sessionId: null,
  reconnecting: false,
  pending: [],
  sessionsList: [],
  savedSessions: [],
  branchConfig: {},
  branches: {},
  candidateBatches: {},
  nextConfig: readStoredNextConfig(),
  mode: readStoredMode(),
  pins: [],
  error: null,
  rewriteDrafts: {},
  composerDraft: null,

  apply: (msg: Down) =>
    set((state) => {
      // A3 version guard: drop a stale structural frame. `<` (not `<=`)
      // so the connect-time `push_full_state` at `v === session.version`
      // isn't rejected when the store's initial `version` happens to
      // match. All GUARDED frames go through the backend's single drain
      // queue and each bumps `version`, so on live traffic `v` is
      // strictly monotone and the guard only fires on a genuine
      // reorder/replay.
      if ("v" in msg && GUARDED.has(msg.t) && msg.v < state.version) return {};
      return msg.t === "batch"
        ? msg.ops.reduce<SessionState>(
            (s, op) => ({ ...s, ...reduceOne(s, op) }),
            state
          )
        : { ...state, ...reduceOne(state, msg) };
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
    next.onopen = () => {
      reconnectBackoff = 1000;
      if (get().ws === next) set({ reconnecting: false });
    };
    next.onclose = () => {
      // Only act if this is still the live socket — a newer `connect()` (or
      // the e2e reconnect test's manual `setState({ws:null})+connect()`) may
      // have superseded it, in which case *its* onclose owns retry.
      if (get().ws !== next) return;
      // STRESS-V2 P2: auto-reconnect with exponential backoff. `sessionId`
      // is KEPT (was cleared pre-P2) so `CommandPalette`/`Sidebar` exports
      // and the setTimeout guard below can distinguish "socket dropped,
      // retry" from "user left" (`disconnect()` clears it).
      set({ ws: null, reconnecting: true });
      const delay = reconnectBackoff;
      reconnectBackoff = Math.min(reconnectBackoff * 2, RECONNECT_BACKOFF_MAX);
      setTimeout(() => {
        // Skip if `disconnect()`/`newAudit()` navigated away, or a manual
        // `connect()` already opened a fresh socket in the interim.
        if (get().sessionId === sessionId && get().ws == null) {
          get().connect(sessionId);
        }
      }, delay);
    };
    // A2 failure-mode caveat: a WS drop between `send()` and the server's
    // `{t:"ack"}` would strand an entry in `pending` (button disabled
    // forever). Reset on every (re)connect — commands never survive a
    // socket, so no in-flight ack is expected on the new one.
    set({ ws: next, sessionId, pending: [], pins: readStoredPins(sessionId) });
  },

  disconnect: () => {
    const ws = get().ws;
    if (ws) {
      ws.onclose = null; // avoid the handler racing our explicit clear
      ws.close();
    }
    set({ ws: null, sessionId: null, reconnecting: false });
  },

  send: (msg: Up) => {
    const ws = get().ws;
    const req_id = crypto.randomUUID();
    set((s) => ({ pending: [...s.pending, { ...msg, req_id }] }));
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ ...msg, req_id }));
    }
    // PRODUCT-GAPS P2: request desktop-notification permission on the first
    // user-initiated send (browsers require a user gesture on the call stack).
    // One-shot — `permission !== "default"` short-circuits after grant/deny.
    // `typeof` guard (not `"Notification" in window`) so the node vitest env,
    // which has no `window`, doesn't ReferenceError.
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      void Notification.requestPermission();
    }
  },

  start: (params) => {
    // F3: overlay-only. `send()` appends to `pending[]` → StartView's
    // `useIsPending(c => c.t === "start")` disables the launch button;
    // A3's `{t:"branch_created"}` echo carries the real id (`current` /
    // `branches[id]` / seed via `msg.meta`) fast enough post-R5 that no
    // PENDING_ID skeleton is needed. The old `sessionsList`/`branchConfig`
    // PENDING_ID entries were never re-keyed post-A3 (dead) and are gone.
    set({ pendingNewAudit: false });
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
      ...(params.auditor_model_args
        ? { auditor_model_args: params.auditor_model_args }
        : {}),
      ...(params.target_model_args
        ? { target_model_args: params.target_model_args }
        : {}),
      ...(params.live_scanners && params.live_scanners.length > 0
        ? { live_scanners: params.live_scanners }
        : {}),
    });
  },

  setNextConfig: (patch) => {
    set((state) => {
      const nextConfig = { ...state.nextConfig, ...patch };
      try {
        localStorage.setItem(NEXT_CONFIG_KEY, JSON.stringify(nextConfig));
      } catch { /* ignore */ }
      return { nextConfig };
    });
  },

  setMode: (mode) => {
    try { localStorage.setItem(MODE_KEY, mode); } catch { /* ignore */ }
    set({ mode });
  },

  togglePin: (log, sample_id, label) => {
    set((state) => {
      const idx = state.pins.findIndex(
        (p) => p.log === log && p.sample_id === sample_id
      );
      let pins: Pin[];
      if (idx >= 0 && label != null) {
        // Relabel in place — a label click on an existing pin is an edit,
        // not a toggle-off.
        pins = state.pins.slice();
        pins[idx] = { ...pins[idx], label };
      } else if (idx >= 0) {
        pins = [...state.pins.slice(0, idx), ...state.pins.slice(idx + 1)];
      } else {
        pins = [{ log, sample_id, label, at: Date.now() }, ...state.pins];
      }
      return persistPins(state.sessionId, pins);
    });
  },

  setLabel: (log, sample_id, label) => {
    set((state) => {
      const idx = state.pins.findIndex(
        (p) => p.log === log && p.sample_id === sample_id
      );
      if (idx < 0) return {};
      const pins = state.pins.slice();
      const { label: _prev, ...rest } = pins[idx];
      pins[idx] = label == null ? rest : { ...rest, label };
      return persistPins(state.sessionId, pins);
    });
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
    // F3: overlay-only. `{t:"branch_created"}` carries the real id;
    // `current`/`byRole` are untouched until it lands.
    set({ pendingNewAudit: false });
    get().send({
      t: "import",
      path,
      ...(sampleId != null ? { sample_id: sampleId } : {}),
    });
  },

  newAudit: () => {
    // A "new audit" is a *fresh session*, not another root branch in the
    // current one — otherwise the sidebar tree accretes every failed
    // attempt (mythos-5 → fable-5 → mythos-preview cascade). Sibling
    // audits in one session are what fork/branch is for. Guard for vitest.
    if (typeof window !== "undefined" && window.location) {
      const id = Math.random().toString(36).slice(2, 10);
      window.location.assign(`?session=${id}`);
      return;
    }
    set({ current: null, pendingNewAudit: true });
  },

  switchBranch: (id) => {
    set({ current: id, pendingNewAudit: false });
    get().send({ t: "switch", branch: id });
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
    // Optimistic status flip for play/step/end so the primary button responds
    // instantly. NOT for pause (RACE-FIXES R1 / chaos s1): flipping to
    // "paused" locally morphs the button into Play, so a double-click sends
    // pause→play and the branch never stops. `Branch.pause()` now cancels its
    // scope synchronously, so the backend `{t:"status"}` lands fast enough to
    // drive the button without an optimistic flip.
    if (cmd !== "pause") {
      set({ status: cmd === "end" ? "ended" : "running" });
    }
    get().send({ t: cmd });
  },

  inject: (branch, role, message) => {
    // F3: `current` is never a sentinel now, so `branch` (its caller-side
    // capture) is always real. The composer disables via `useIsPending`
    // during a fork, so the R1 sentinel guard is unreachable and gone.
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

  unqueue: (branch, role, messageId) => {
    set((state) => {
      const roles = state.queued[branch];
      if (roles == null) return {};
      const kept = roles[role].filter((m) => m.id !== messageId);
      return { queued: { ...state.queued, [branch]: { ...roles, [role]: kept } } };
    });
    get().send({ t: "unqueue", branch, role, message_id: messageId });
  },

  setComposerDraft: (text) => set({ composerDraft: text }),
}));

/**
 * Shared body for `branchAt` / `resampleAt` / edit ops.
 *
 * F3: overlay-only — `send()` appends `{...cmd, at: anchorId}` to `pending[]`
 * and `useTruncatedEvents` / `usePendingForkAnchor` derive the visual cut
 * from that. `current`/`byRole`/`branches` are NOT mutated, so `case "error"`
 * has nothing to roll back and `prevCurrent` is gone. Re-entry is prevented
 * at the button (`disabled={useIsPending(c => FORK_KINDS.has(c.t))}`).
 */
function _pendingChild(
  _set: (partial: Partial<SessionState>) => void,
  get: () => SessionState,
  anchorId: string,
  cmd: Up
): void {
  const current = get().current;
  if (current == null) return;
  // Pin `branch:` to the id captured at click time — the server otherwise
  // falls back to its own `session.current`, which can drift if a background
  // op repoints it before this lands (WS-race #3,4). `at:` rides along
  // (uniformly, even on variants whose `Up` type lacks it) so the derived
  // truncation selector can read the anchor from `pending[]`. The backend
  // ignores extras; `wire.ts` doesn't declare either on every variant —
  // hence the cast.
  get().send({ ...cmd, branch: current, at: anchorId } as Up);
}
