import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { beforeEach, describe, expect, it } from "vitest";

import type { ChatMessage } from "@tsmono/inspect-common";

import {
  convertServerTimeline,
  computeFlatSwimlaneRows,
  getAgents,
  splice,
} from "@tsmono/inspect-components/transcript/timeline";

import {
  treeifyEvents,
  type EventNode,
} from "@tsmono/inspect-components/transcript/transform";

import { isModelEvent, resolveRole } from "../lib/events";
import type { Down } from "../lib/wire";
import { useSession } from "./session";

// OVERNIGHT-SWEEP D dropped the `buildEventTree` wrapper (dead in prod) —
// inline inspect's `treeifyEvents` here so the tree-consistency invariants
// stay covered without keeping a wrapper in `lib/events`.
const buildEventTree = (events: Iterable<unknown>): EventNode[] =>
  treeifyEvents([...events] as never, 0);

const fixtureUrl = new URL("../../fixtures/smoke.json", import.meta.url);
const messages = JSON.parse(
  readFileSync(fileURLToPath(fixtureUrl), "utf8")
) as Down[];

/** A3-batch: post-OVERNIGHT-E fixtures ship pool/event/update/timeline inside
 *  `{t:"batch", ops:[…]}`. `apply()` handles the fold; tests that scan the
 *  raw wire capture flatten first (mirrors `_smoke_util.flatten`). */
type DownOp = Exclude<Down, { t: "batch" }>;
const flatten = (msgs: Down[]): DownOp[] =>
  msgs.flatMap((m) => (m.t === "batch" ? m.ops : [m]));

describe("session reducer against real smoke fixture", () => {
  it("applies every wire message and reconstructs the event store", () => {
    // distinct event uuids the backend shipped (state.events + event/update).
    const distinct = new Set<string | null | undefined>();
    for (const m of flatten(messages)) {
      if (m.t === "state") for (const e of m.events) distinct.add(e.uuid);
      if (m.t === "event" || m.t === "update") distinct.add(m.event.uuid);
    }
    // OVERNIGHT-E: fixture regenerated post-A3-batch — every event/update
    // reaches the wire inside a `{t:"batch"}`, so `apply()`'s ops-fold path
    // is now exercised by the fixture replay itself.
    expect(messages.some((m) => m.t === "batch")).toBe(true);

    const { apply } = useSession.getState();
    for (const m of messages) apply(m);
    const state = useSession.getState();

    // every distinct uuid is present.
    expect(state.events.size).toBe(distinct.size);

    // both columns are populated via the incrementally-maintained index.
    const auditor = state.byRole["b0"]?.auditor ?? [];
    const target = state.byRole["b0"]?.target ?? [];
    expect(auditor.length).toBeGreaterThan(0);
    expect(target.length).toBeGreaterThan(0);

    // the last target ModelEvent resolves to system + user (≥2 messages).
    const targetModels = target.filter(isModelEvent);
    expect(targetModels.length).toBeGreaterThan(0);
    const last = targetModels[targetModels.length - 1];
    expect(last.input.length).toBeGreaterThanOrEqual(2);
    const roles = new Set(last.input.map((m: ChatMessage) => m.role));
    expect(roles.has("system")).toBe(true);
    expect(roles.has("user")).toBe(true);
  });

  it("byRole arrays are reference-stable across updates to the other column", () => {
    // capture references after the fixture replay above
    const before = useSession.getState();
    const eventsRef = before.events;
    const auditorRef = before.byRole["b0"].auditor;
    const targetRef = before.byRole["b0"].target;

    // replay the last `update` for a target ModelEvent — only target's array
    // should change; auditor's reference should be preserved.
    const targetUpdate = flatten(messages)
      .reverse()
      .find(
        (m): m is Extract<DownOp, { t: "update" }> =>
          m.t === "update" && m.event.event === "model" &&
          targetRef.some((e) => e.uuid === m.event.uuid)
      );
    expect(targetUpdate).toBeDefined();
    useSession.getState().apply(targetUpdate!);

    const after = useSession.getState();
    expect(after.byRole["b0"].auditor).toBe(auditorRef);
    expect(after.byRole["b0"].target).not.toBe(targetRef);
    // OVERNIGHT-SWEEP P13: the Map is mutated in place — the pre-update ref
    // sees the new content, and the top-level ref is connection-stable.
    expect(eventsRef.get(targetUpdate!.event.uuid!)).toBeDefined();
    expect(after.events).toBe(eventsRef);
  });

  it("routes a nested span to its role via resolveRole", () => {
    const state = useSession.getState();
    // every event with a resolvable role lands in exactly one bucket.
    let resolved = 0;
    for (const ev of state.events.values()) {
      if (resolveRole(ev.span_id, state.spanParent, state.spanRole)) resolved++;
    }
    expect(resolved).toBeGreaterThan(0);
  });

  it("builds an inspect EventNode tree whose span structure matches spanParent", () => {
    const state = useSession.getState();
    // eventTree is derived, not stored — build it directly to verify against
    // the routing graph.
    const eventTree = buildEventTree(state.events.values());
    expect(eventTree.length).toBeGreaterThan(0);

    type N = (typeof eventTree)[number];
    const seen = new Map<string, number>();
    const treeSpanParent = new Map<string, string | null>();
    const walk = (nodes: N[], enclosingSpan: string | null) => {
      for (const n of nodes) {
        seen.set(n.id, (seen.get(n.id) ?? 0) + 1);
        const spanId =
          n.event.event === "span_begin" ? n.event.id : null;
        if (spanId) treeSpanParent.set(spanId, enclosingSpan);
        walk(n.children, spanId ?? enclosingSpan);
      }
    };
    walk(eventTree, null);

    // No event appears twice in the tree (dedup invariant).
    for (const [, count] of seen) expect(count).toBe(1);
    // Tree never invents events — every node id is a real event uuid.
    for (const id of seen.keys()) expect(state.events.has(id)).toBe(true);
    // Tree is a coarsening of the store (transformTree unwraps tool/subtask
    // span wrappers), so |tree| ≤ |store \ span_end| but contains every
    // ModelEvent — those are never dropped.
    const nonSpanEnd = [...state.events.values()].filter(
      (e) => e.event !== "span_end"
    );
    expect(seen.size).toBeGreaterThan(0);
    expect(seen.size).toBeLessThanOrEqual(nonSpanEnd.length);
    for (const ev of state.events.values()) {
      if (ev.event === "model") expect(seen.has(ev.uuid!)).toBe(true);
    }

    // The display tree's span hierarchy agrees with the raw routing graph
    // for every span the routing graph knows about. (transformTree may
    // unwrap presentational spans like type=tool, so we only assert on
    // spans present in both — i.e. the tree is a coarsening, never a
    // contradiction.)
    for (const [spanId, parent] of treeSpanParent) {
      if (state.spanParent.has(spanId)) {
        // walk raw spanParent up from `parent` should eventually hit
        // (or equal) spanParent.get(spanId)'s ancestor chain.
        const rawParent = state.spanParent.get(spanId) ?? null;
        // Either identical, or the raw parent is an ancestor of the tree
        // parent (tree never invents parentage).
        let cur: string | null | undefined = parent;
        const chain = new Set<string | null>([null]);
        while (cur != null) { chain.add(cur); cur = state.spanParent.get(cur) ?? null; }
        // rawParent must be reachable from the tree parent via raw graph,
        // OR they're equal (common case: no unwrapping happened).
        expect(parent === rawParent || chain.has(rawParent)).toBe(true);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// New isolated tests — each resets store state in beforeEach to avoid leaking
// ---------------------------------------------------------------------------

// OVERNIGHT-SWEEP P13: `state.events` is mutated in place, so a shared const
// `emptyState.events` would leak across tests. Factory returns fresh Maps.
const emptyState = () => ({
  pool: [],
  events: new Map(),
  eventsRev: 0,
  byRole: {},
  turnScores: {},
  timelines: {},
  spanParent: new Map(),
  spanRole: new Map(),
  queued: {},
  version: 0,
  current: null,
  status: null,
  ws: null,
  sessionId: null,
  branches: {},
  orchestrator: null,
  rewound: new Set<string>(),
  pins: [],
  pending: [],
  error: null,
});

describe("byRole isolation across two branches", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("(a) events for b0 and b1 land in separate byRole buckets", () => {
    const { apply } = useSession.getState();

    // A state message that maps two span ids to different branches.
    const stateMsg: Down = {
      t: "state",
      v: 1,
      pool: [],
      events: [],
      span_role: {
        "span-b0-auditor": ["b0", "auditor"],
        "span-b1-auditor": ["b1", "auditor"],
      },
      queued: { b0: { auditor: [], target: [] }, b1: { auditor: [], target: [] } },
      current: "b0",
      status: "idle",
      branches: {
        b0: { parent: null, branched_at: null, branched_at_turn: null, status: "idle", seed: "s" },
        b1: { parent: "b0", branched_at: "anc", branched_at_turn: 1, status: "idle", seed: "s" },
      },
    };
    apply(stateMsg);

    // An event for branch b0.
    const eventB0: Down = {
      t: "event",
      v: 2,
      event: {
        event: "span_begin",
        uuid: "uuid-b0-ev1",
        span_id: "span-b0-auditor",
        id: "span-b0-auditor",
        parent_id: null,
        name: "auditor",
        timestamp: "2024-01-01T00:00:00",
        working_start: 0,
      },
    };
    apply(eventB0);

    // An event for branch b1.
    const eventB1: Down = {
      t: "event",
      v: 3,
      event: {
        event: "span_begin",
        uuid: "uuid-b1-ev1",
        span_id: "span-b1-auditor",
        id: "span-b1-auditor",
        parent_id: null,
        name: "auditor",
        timestamp: "2024-01-01T00:00:00",
        working_start: 0,
      },
    };
    apply(eventB1);

    const state = useSession.getState();
    // b0 and b1 each have their own bucket.
    expect(state.byRole["b0"]).toBeDefined();
    expect(state.byRole["b1"]).toBeDefined();
    // The b0 event is only in b0.
    const b0uuids = state.byRole["b0"].auditor.map((e) => e.uuid);
    const b1uuids = state.byRole["b1"].auditor.map((e) => e.uuid);
    expect(b0uuids).toContain("uuid-b0-ev1");
    expect(b1uuids).toContain("uuid-b1-ev1");
    // No cross-contamination.
    expect(b0uuids).not.toContain("uuid-b1-ev1");
    expect(b1uuids).not.toContain("uuid-b0-ev1");
  });
});

describe("status message", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("(b) apply({t:'status'}) updates status", () => {
    const { apply } = useSession.getState();
    apply({ t: "status", v: 1, status: "paused" });
    expect(useSession.getState().status).toBe("paused");
  });
});

describe("OVERNIGHT-SWEEP C1 invariants", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  const stateMsg: Down = {
    t: "state", v: 10, pool: [], events: [],
    span_role: { "sp-t": ["b0", "target"] },
    queued: {}, current: "b0", status: "idle",
    branches: { b0: { parent: null, branched_at: null, branched_at_turn: null, status: "idle", seed: "s" } },
  };

  it("P13: streaming update mutates events in place (stable ref); eventsRev bumps", () => {
    const { apply } = useSession.getState();
    apply(stateMsg);
    const rev0 = useSession.getState().eventsRev;

    apply({ t: "event", v: 11, event: {
      event: "model", uuid: "u1", span_id: "sp-t", model: "m", pending: true,
      input: [], output: { model: "m", choices: [] }, config: {}, tools: [],
      tool_choice: "none", timestamp: "2024-01-01T00:00:00", working_start: 0,
    } } as unknown as Down);
    expect(useSession.getState().eventsRev).toBe(rev0 + 1);
    const ref = useSession.getState().events;

    // Streaming update (pending: true) — the P13 hot path. Map ref STABLE;
    // eventsRev NOT bumped (P16 — `useSwimlanes` stays quiet).
    apply({ t: "update", v: 12, event: {
      event: "model", uuid: "u1", span_id: "sp-t", model: "m", pending: true,
      input: [], output: { model: "m", choices: [] }, config: {}, tools: [],
      tool_choice: "none", timestamp: "2024-01-01T00:00:00", working_start: 0,
    } } as unknown as Down);
    expect(useSession.getState().events).toBe(ref);
    expect(useSession.getState().eventsRev).toBe(rev0 + 1);
    const byRoleRef = useSession.getState().byRole;

    // pool: mutates in place (P12), stable ref.
    apply({ t: "pool", v: 13, from: 0, entries: [{ role: "user", content: "x" }] });
    expect(useSession.getState().events).toBe(ref);
    expect(useSession.getState().eventsRev).toBe(rev0 + 2);
    // P12: byRole untouched when no stored event has unresolved input_refs.
    expect(useSession.getState().byRole).toBe(byRoleRef);

    // Terminal update (pending: false) — structural (chaos s7). Map ref
    // STAYS STABLE (C2 shim removed); eventsRev bumps so `useSwimlanes`
    // recomputes and the `.cursor` / retracted-pending shimmer clears.
    apply({ t: "update", v: 14, event: {
      event: "model", uuid: "u1", span_id: "sp-t", model: "m", pending: false,
      input: [], output: { model: "m", choices: [] }, config: {}, tools: [],
      tool_choice: "none", timestamp: "2024-01-01T00:00:00", working_start: 0,
    } } as unknown as Down);
    expect(useSession.getState().events).toBe(ref);
    expect(useSession.getState().eventsRev).toBe(rev0 + 3);
    expect((ref.get("u1") as { pending?: boolean }).pending).toBe(false);
  });

  it("P17: turnScores index — event appends, update replaces in place", () => {
    const { apply } = useSession.getState();
    apply(stateMsg);
    const mkScore = (score: number | null) => ({
      t: "event" as const, v: 11, event: {
        event: "info", uuid: "ts-ev", span_id: "sp-t", source: "turn_score",
        data: { kind: "turn_score", turn_uuid: "turn-1", scanner: "leak", score, explanation: "", error: null },
        timestamp: "2024-01-01T00:00:00", working_start: 0,
      },
    }) as unknown as Down;

    apply(mkScore(null));
    let ts = useSession.getState().turnScores;
    expect(ts["turn-1"]).toHaveLength(1);
    expect(ts["turn-1"][0].score).toBeNull();

    // resolved score arrives as an update on the same uuid → replaces, not appends
    apply({ ...(mkScore(0.7) as { t: string }), t: "update" } as Down);
    ts = useSession.getState().turnScores;
    expect(ts["turn-1"]).toHaveLength(1);
    expect(ts["turn-1"][0].score).toBe(0.7);

    // a second scanner on the same turn appends
    apply({ t: "event", v: 12, event: {
      event: "info", uuid: "ts-ev2", span_id: "sp-t", source: "turn_score",
      data: { kind: "turn_score", turn_uuid: "turn-1", scanner: "refusal", score: 0.1, explanation: "", error: null },
      timestamp: "2024-01-01T00:00:00", working_start: 0,
    } } as unknown as Down);
    expect(useSession.getState().turnScores["turn-1"]).toHaveLength(2);
  });

  it("W-A: sideband arms do not write version", () => {
    const { apply } = useSession.getState();
    apply(stateMsg); // version = 10
    expect(useSession.getState().version).toBe(10);

    // Each of these carries v:5 (< 10). They're not GUARDED so the guard
    // doesn't drop them; W-A says they must not rewind `version`.
    apply({ t: "queued", v: 5, branch: "b0", role: "auditor",
            message: { role: "user", content: "x", id: "m1" } });
    apply({ t: "unqueued", v: 5, branch: "b0", role: "auditor", message_id: "m1" });
    apply({ t: "rewrite_draft", v: 5, branch: "b0", call_id: "c1", args: {} });
    apply({ t: "error", v: 5, message: "boom" });
    apply({ t: "notify", v: 5, text: "hi" });
    apply({ t: "rewound", v: 5, span: "orch", from_uuid: "x" });
    apply({ t: "update", v: 5, event: {
      event: "info", uuid: "u-wa", span_id: "sp-t", source: "x", data: {},
      timestamp: "2024-01-01T00:00:00", working_start: 0,
    } } as unknown as Down);

    expect(useSession.getState().version).toBe(10);
    // and a subsequent GUARDED frame at v=11 is not rejected
    apply({ t: "current", v: 11, branch: "b0" });
    expect(useSession.getState().version).toBe(11);
  });

  it("A6#4: notifications capped at 200; E19: unknown t warns and returns {}", () => {
    const { apply } = useSession.getState();
    useSession.setState({
      orchestrator: {
        span_id: "o", status: "idle", pending_gates: [], bg_cells: [],
        notifications: Array.from({ length: 199 }, (_, i) => `n${i}`),
      },
    });
    apply({ t: "notify", v: 1, text: "n199" });
    apply({ t: "notify", v: 2, text: "n200" });
    const notes = useSession.getState().orchestrator!.notifications;
    expect(notes).toHaveLength(200);
    expect(notes[0]).toBe("n1"); // head dropped
    expect(notes[199]).toBe("n200");

    // E19 — unknown Down.t: no throw, no state change.
    const before = useSession.getState().version;
    apply({ t: "future_thing", v: 99 } as unknown as Down);
    expect(useSession.getState().version).toBe(before);
  });
});

describe("OVERNIGHT-E: A3-typed-delta reducer arms", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("branch_created: populates branches[id], merges spanRole delta, adopts current", () => {
    const { apply } = useSession.getState();
    apply({
      t: "branch_created", v: 1, id: "b7",
      meta: { parent: "b0", branched_at: "anc", branched_at_turn: 2, status: "idle", seed: "s" },
      span_role_delta: { "sp-b7-aud": ["b7", "auditor"], "sp-b7-tgt": ["b7", "target"] },
      current: "b7",
    });
    const s = useSession.getState();
    expect(s.branches.b7.parent).toBe("b0");
    expect(s.spanRole.get("sp-b7-aud")).toEqual(["b7", "auditor"]);
    expect(s.spanRole.get("sp-b7-tgt")).toEqual(["b7", "target"]);
    expect(s.current).toBe("b7");
    expect(s.version).toBe(1);
    expect(s.pendingNewAudit).toBe(false);
  });

  it("current: sets current + version", () => {
    const { apply } = useSession.getState();
    apply({ t: "current", v: 3, branch: "b2" });
    expect(useSession.getState().current).toBe("b2");
    expect(useSession.getState().version).toBe(3);
    apply({ t: "current", v: 4, branch: null });
    expect(useSession.getState().current).toBeNull();
  });

  it("ack: removes matching req_id from pending", () => {
    useSession.setState({
      pending: [
        { t: "play", req_id: "r1" },
        { t: "pause", req_id: "r2" },
      ],
    });
    const { apply } = useSession.getState();
    apply({ t: "ack", req_id: "r1" });
    const s = useSession.getState();
    expect(s.pending).toHaveLength(1);
    expect(s.pending[0].req_id).toBe("r2");
    // unknown req_id → no-op
    apply({ t: "ack", req_id: "nope" });
    expect(useSession.getState().pending).toHaveLength(1);
  });

  it("orch: merges (not replaces) orchestrator fields; merges spanRole", () => {
    const { apply } = useSession.getState();
    // Initial full push (carries notifications:[]).
    apply({
      t: "orch", v: 1,
      state: {
        span_id: "orch-sp", status: "running", pending_gates: [], bg_cells: [],
        notifications: [],
      },
      span_role_delta: { "orch-sp": ["orch", "orch"] },
    } as Down);
    // Client-side chip appended between pushes.
    apply({ t: "notify", v: 1, text: "chip" });
    expect(useSession.getState().orchestrator!.notifications).toEqual(["chip"]);
    // A4-partial `dirty()` push: bg_jobs only, notifications STRIPPED.
    // Merge must not clobber the appended chip.
    apply({
      t: "orch", v: 2,
      state: { bg_jobs: [{ kind: "bash", id: "bg-1", cmd_or_task: "sleep", status: "running" }] },
      span_role_delta: {},
    } as unknown as Down);
    const s = useSession.getState();
    expect(s.orchestrator!.notifications).toEqual(["chip"]);
    expect(s.orchestrator!.bg_jobs).toHaveLength(1);
    expect(s.orchestrator!.status).toBe("running"); // preserved
    expect(s.spanRole.get("orch-sp")).toEqual(["orch", "orch"]);
    expect(s.version).toBe(2);
  });

  it("rewound: marks orch events at/after from_uuid; bumps eventsRev; W-A no version write", () => {
    // Seed byRole["orch"]["orch"] directly (rewound reads it, not `events`).
    const orchEvs = [
      { event: "info", uuid: "u1" }, { event: "info", uuid: "u2" },
      { event: "info", uuid: "u3" },
    ];
    useSession.setState({
      byRole: { orch: { auditor: [], target: [], orch: orchEvs } } as never,
      version: 10, eventsRev: 5,
    });
    const { apply } = useSession.getState();
    apply({ t: "rewound", v: 3, span: "orch", from_uuid: "u2" });
    const s = useSession.getState();
    expect(s.rewound.has("u1")).toBe(false);
    expect(s.rewound.has("u2")).toBe(true);
    expect(s.rewound.has("u3")).toBe(true);
    expect(s.eventsRev).toBe(6);
    expect(s.version).toBe(10); // W-A: sideband

    // from_uuid not in the bucket → no-op.
    apply({ t: "rewound", v: 3, span: "orch", from_uuid: "missing" });
    expect(useSession.getState().rewound.size).toBe(2);
  });

  it("queued_consumed: drops matching auditor-queued ids", () => {
    useSession.setState({
      queued: {
        b0: {
          auditor: [
            { role: "user", content: "a", id: "m1" },
            { role: "user", content: "b", id: "m2" },
          ],
          target: [],
        },
      },
    });
    const { apply } = useSession.getState();
    apply({ t: "queued_consumed", v: 5, branch: "b0", ids: ["m1", "gone"] });
    const s = useSession.getState();
    expect(s.queued.b0.auditor).toHaveLength(1);
    expect(s.queued.b0.auditor[0].id).toBe("m2");
    expect(s.version).toBe(5);
    // unknown branch → version bump only.
    apply({ t: "queued_consumed", v: 6, branch: "nope", ids: ["m2"] });
    expect(useSession.getState().queued.b0.auditor).toHaveLength(1);
    expect(useSession.getState().version).toBe(6);
  });
});

describe("queued reconciliation", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("(c) queued message drops when its id appears in a ModelEvent input", () => {
    const { apply } = useSession.getState();

    // First register the span so the event routes to a branch/role.
    const stateMsg: Down = {
      t: "state",
      v: 1,
      pool: [],
      events: [],
      span_role: { "span-bq-auditor": ["bq", "auditor"] },
      queued: {},
      current: "bq",
      status: "idle",
      branches: { bq: { parent: null, branched_at: null, branched_at_turn: null, status: "idle", seed: "s" } },
    };
    apply(stateMsg);

    // Apply a queued message for branch "bq", role "auditor".
    const queuedMsg: Down = {
      t: "queued",
      v: 2,
      branch: "bq",
      role: "auditor",
      message: { role: "user", content: "hello", id: "test-msg-id" },
    };
    apply(queuedMsg);

    // Assert the queued map has the message.
    const afterQueued = useSession.getState().queued;
    expect(afterQueued["bq"]?.auditor).toHaveLength(1);
    expect(afterQueued["bq"]?.auditor[0].id).toBe("test-msg-id");

    // Now apply a ModelEvent whose input contains a message with that id.
    // The pool must first contain the input message.
    const poolMsg: Down = {
      t: "pool",
      v: 3,
      from: 0,
      entries: [{ role: "user", content: "hello", id: "test-msg-id" }],
    };
    apply(poolMsg);

    const modelEventMsg = {
      t: "event",
      v: 4,
      event: {
        event: "model",
        uuid: "uuid-model-ev1",
        span_id: "span-bq-auditor",
        model: "test-model",
        input: [{ role: "user", content: "hello", id: "test-msg-id" }],
        output: {
          model: "test-model",
          choices: [],
          completion: "",
          usage: { input_tokens: 1, output_tokens: 0, total_tokens: 1 },
        },
        timestamp: "2024-01-01T00:00:00",
        // Required by the full ModelEvent type — minimal stubs sufficient for the test.
        config: {},
        tool_choice: "none" as const,
        tools: [],
        working_start: 0,
      },
    } as unknown as Down;
    apply(modelEventMsg);

    // The queued message should now be gone.
    const afterEvent = useSession.getState().queued;
    expect(afterEvent["bq"]?.auditor ?? []).toHaveLength(0);
  });
});

describe("state message populates branch tree", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("(d) state with branches populates the tree", () => {
    const { apply } = useSession.getState();

    const stateMsg: Down = {
      t: "state",
      v: 1,
      pool: [],
      events: [],
      span_role: {},
      queued: {},
      current: "b0",
      status: "idle",
      branches: {
        b0: { parent: null, branched_at: null, branched_at_turn: null, status: "paused", seed: "x" },
        b1: { parent: "b0", branched_at: "anc1", branched_at_turn: 1, status: "idle", seed: "x" },
      },
    };
    apply(stateMsg);

    expect(useSession.getState().branches.b1.parent).toBe("b0");
  });
});

describe("rollback fixture (smoke_rollback) — server timeline → swimlanes", () => {
  // Generated by `_smoke_branch_deep --scenarios b`: one branch where the
  // auditor calls rollback_conversation, producing 2 target trajectories.
  // Asserts the same pipe inspect-view uses: petri's server-built Timeline →
  // convertServerTimeline → computeFlatSwimlaneRows({showBranches:true}).
  const rbUrl = new URL("../../fixtures/smoke_rollback.json", import.meta.url);
  const rbMessages = JSON.parse(
    readFileSync(fileURLToPath(rbUrl), "utf8")
  ) as Down[];

  beforeEach(() => {
    useSession.setState(emptyState());
    const { apply } = useSession.getState();
    for (const m of rbMessages) apply(m);
  });

  it("stores the server-built target timeline and swimlane rows expose ≥2 trajectories", () => {
    const { timelines, events } = useSession.getState();
    const serverTl = timelines["rb"]?.target;
    expect(serverTl, "no target timeline for branch rb").toBeDefined();

    const tl = convertServerTimeline(serverTl!, [...events.values()]);
    const rows = computeFlatSwimlaneRows(tl.root, {
      includeUtility: true,
      showBranches: true,
    });
    // Root trajectory + ≥1 branch row.
    expect(rows.length).toBeGreaterThanOrEqual(2);
    expect(rows[0].branch).not.toBe(true);
    const branchRows = rows.filter((r) => r.branch === true);
    expect(branchRows.length).toBeGreaterThanOrEqual(1);
    // `branchedFrom` may be null when the rollback anchors before any parent
    // content (model-behavior dependent); when present it names an anchor id.
    if (branchRows[0].branchedFrom != null) {
      expect(typeof branchRows[0].branchedFrom).toBe("string");
    }

    // splice() on a branch row's span yields a non-empty lineage that
    // includes the parent prefix (≥ the branch's own content length).
    const branchSpan = getAgents(branchRows[0].spans[0])[0];
    const lineage = splice(tl.root, branchSpan);
    const ownContent = branchSpan.content.filter((c) => c.type === "event").length;
    expect(lineage.length).toBeGreaterThanOrEqual(ownContent);
  });

  it("emits ≥2 target {t:'timeline'} ops (on BranchEvent + post-rollback target turns)", () => {
    // Both roles now ship a timeline (auditor swimlane added in the splice
    // refactor); the rollback-creates-a-branch assertion is target-specific.
    const flat = flatten(rbMessages);
    const tlOps = flat.filter(
      (m): m is Extract<typeof m, { t: "timeline" }> =>
        m.t === "timeline" && m.role === "target"
    );
    expect(tlOps.length).toBeGreaterThanOrEqual(2);
    const last = tlOps[tlOps.length - 1];
    expect(last.timeline.root.branches?.length ?? 0).toBeGreaterThanOrEqual(1);
    // OVERNIGHT-E: rollback fixture now carries `{t:"l1_spans"}` (F2) inside
    // the same batch as the BranchEvent's timeline op.
    expect(flat.some((m) => m.t === "l1_spans")).toBe(true);
  });
});

describe("multi-branch fixture (smoke_branch_deep)", () => {
  // Generated by `uv run python -m workbench._smoke_branch_deep` — a real
  // 3-branch chain (b1 → b2 → b3) wire capture. Asserts the frontend reducer
  // upholds the same invariants the backend test checks (INSPECT-REUSE.md §4).
  const deepUrl = new URL("../../fixtures/smoke_branch_deep.json", import.meta.url);
  const deepMessages = JSON.parse(
    readFileSync(fileURLToPath(deepUrl), "utf8")
  ) as Down[];

  beforeEach(() => {
    useSession.setState(emptyState());
    const { apply } = useSession.getState();
    for (const m of deepMessages) apply(m);
  });

  it("byRole has b1/b2/b3 with non-empty auditor and target columns", () => {
    const { byRole } = useSession.getState();
    for (const bid of ["b1", "b2", "b3"]) {
      expect(byRole[bid]?.auditor.length, `${bid}.auditor`).toBeGreaterThan(0);
      expect(byRole[bid]?.target.length, `${bid}.target`).toBeGreaterThan(0);
    }
  });

  it("no event uuid appears in more than one branch's bucket", () => {
    const { byRole } = useSession.getState();
    const branchOf = new Map<string, string>();
    for (const [bid, roles] of Object.entries(byRole)) {
      for (const ev of [...roles.auditor, ...roles.target]) {
        const prev = branchOf.get(ev.uuid!);
        expect(
          prev === undefined || prev === bid,
          `uuid ${ev.uuid} in both ${prev} and ${bid}`
        ).toBe(true);
        branchOf.set(ev.uuid!, bid);
      }
    }
  });

  it("eventTree contains every ModelEvent across all branches", () => {
    const { events } = useSession.getState();
    const eventTree = buildEventTree(events.values());
    type N = (typeof eventTree)[number];
    const inTree = new Set<string>();
    const walk = (ns: N[]) => {
      for (const n of ns) { inTree.add(n.id); walk(n.children); }
    };
    walk(eventTree);
    for (const ev of events.values()) {
      if (ev.event === "model") {
        expect(inTree.has(ev.uuid!), `model ${ev.uuid} missing from tree`).toBe(true);
      }
    }
  });

  it("branches metadata reflects the chain (b2.parent=b1, b3.parent=b2)", () => {
    const { branches } = useSession.getState();
    expect(branches.b1?.parent).toBeNull();
    expect(branches.b2?.parent).toBe("b1");
    expect(branches.b3?.parent).toBe("b2");
    expect(branches.b2?.branched_at).toBeTruthy();
    expect(branches.b3?.branched_at).toBeTruthy();
  });

  it("synthesised prefix events (input=[]) precede live events in each child branch", () => {
    const { byRole } = useSession.getState();
    for (const bid of ["b2", "b3"]) {
      for (const role of ["auditor", "target"] as const) {
        const models = byRole[bid][role].filter((e) => e.event === "model");
        const firstLive = models.findIndex(
          (e) => e.event === "model" && (e.input?.length ?? 0) > 0
        );
        const lastSynth = models.reduce(
          (idx, e, i) => (e.event === "model" && e.input?.length === 0 ? i : idx),
          -1
        );
        // every synth comes before the first live (or there are no live yet)
        if (firstLive !== -1 && lastSynth !== -1) {
          expect(lastSynth, `${bid}.${role}: synth after live`).toBeLessThan(firstLive);
        }
      }
    }
  });
});

describe("togglePin (P2 pin/bookmark)", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("adds a pin, then removes it on second toggle of same (log, sample_id)", () => {
    const { togglePin } = useSession.getState();
    togglePin("/logs/run.eval", "sample-1");
    let { pins } = useSession.getState();
    expect(pins).toHaveLength(1);
    expect(pins[0]).toMatchObject({ log: "/logs/run.eval", sample_id: "sample-1" });
    expect(pins[0].at).toBeGreaterThan(0);

    togglePin("/logs/run.eval", "sample-1");
    pins = useSession.getState().pins;
    expect(pins).toHaveLength(0);
  });

  it("keys on (log, sample_id) — same sample_id in different logs are distinct", () => {
    const { togglePin } = useSession.getState();
    togglePin("/logs/a.eval", "s1");
    togglePin("/logs/b.eval", "s1");
    expect(useSession.getState().pins).toHaveLength(2);
    // most-recent first
    expect(useSession.getState().pins[0].log).toBe("/logs/b.eval");

    togglePin("/logs/a.eval", "s1");
    const { pins } = useSession.getState();
    expect(pins).toHaveLength(1);
    expect(pins[0].log).toBe("/logs/b.eval");
  });

  it("with label on an existing pin relabels (never removes)", () => {
    const { togglePin } = useSession.getState();
    togglePin("/logs/run.eval", "s1");
    expect(useSession.getState().pins[0].label).toBeUndefined();

    togglePin("/logs/run.eval", "s1", "confirmed");
    let { pins } = useSession.getState();
    expect(pins).toHaveLength(1);
    expect(pins[0].label).toBe("confirmed");

    togglePin("/logs/run.eval", "s1", "needs-review");
    pins = useSession.getState().pins;
    expect(pins).toHaveLength(1);
    expect(pins[0].label).toBe("needs-review");

    // no-label toggle still removes, even when labelled
    togglePin("/logs/run.eval", "s1");
    expect(useSession.getState().pins).toHaveLength(0);
  });

  it("with label on a missing pin creates it labelled", () => {
    const { togglePin } = useSession.getState();
    togglePin("/logs/run.eval", "s1", "interesting");
    const { pins } = useSession.getState();
    expect(pins).toHaveLength(1);
    expect(pins[0]).toMatchObject({
      log: "/logs/run.eval",
      sample_id: "s1",
      label: "interesting",
    });
  });
});

describe("setLabel (P3 annotation queue)", () => {
  beforeEach(() => {
    useSession.setState(emptyState());
  });

  it("sets and clears label on an existing pin; no-op on missing pin", () => {
    const { togglePin, setLabel } = useSession.getState();

    // no-op when pin absent
    setLabel("/logs/run.eval", "s1", "confirmed");
    expect(useSession.getState().pins).toHaveLength(0);

    togglePin("/logs/run.eval", "s1");
    setLabel("/logs/run.eval", "s1", "false-positive");
    expect(useSession.getState().pins[0].label).toBe("false-positive");

    setLabel("/logs/run.eval", "s1", null);
    const { pins } = useSession.getState();
    expect(pins).toHaveLength(1);
    expect(pins[0].label).toBeUndefined();
  });
});

// OVERNIGHT-SWEEP D: the "start does not add phantom Recents" test asserted
// against `sessionsList`, which is dead post-F3 (`start()` no longer writes a
// PENDING_ID entry; `savedSessions` from `GET /sessions` supersedes it).
// Dropped so D can delete the `sessionsList`/`SessionSummary` stubs.
