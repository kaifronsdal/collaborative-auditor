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

import { buildEventTree, isModelEvent, resolveRole } from "../lib/events";
import type { Down } from "../lib/wire";
import { useSession } from "./session";

const fixtureUrl = new URL("../../fixtures/smoke.json", import.meta.url);
const messages = JSON.parse(
  readFileSync(fileURLToPath(fixtureUrl), "utf8")
) as Down[];

describe("session reducer against real smoke fixture", () => {
  it("applies every wire message and reconstructs the event store", () => {
    // distinct event uuids the backend shipped (state.events + event/update).
    const distinct = new Set<string | null | undefined>();
    for (const m of messages) {
      if (m.t === "state") for (const e of m.events) distinct.add(e.uuid);
      if (m.t === "event" || m.t === "update") distinct.add(m.event.uuid);
    }

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
    const auditorRef = before.byRole["b0"].auditor;
    const targetRef = before.byRole["b0"].target;

    // replay the last `update` for a target ModelEvent — only target's array
    // should change; auditor's reference should be preserved.
    const targetUpdate = [...messages]
      .reverse()
      .find(
        (m): m is Extract<Down, { t: "update" }> =>
          m.t === "update" && m.event.event === "model" &&
          targetRef.some((e) => e.uuid === m.event.uuid)
      );
    expect(targetUpdate).toBeDefined();
    useSession.getState().apply(targetUpdate!);

    const after = useSession.getState();
    expect(after.byRole["b0"].auditor).toBe(auditorRef);
    expect(after.byRole["b0"].target).not.toBe(targetRef);
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
    // eventTree is derived (useEventTree selector), not stored — build it the
    // same way the selector does to verify it against the routing graph.
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

const emptyState = {
  pool: [],
  events: new Map(),
  byRole: {},
  timelines: {},
  spanParent: new Map(),
  spanRole: new Map(),
  queued: {},
  version: 0,
  current: null,
  status: null,
  ws: null,
  sessionId: null,
  sessionsList: [],
  branchConfig: {},
  branches: {},
  error: null,
};

describe("byRole isolation across two branches", () => {
  beforeEach(() => {
    useSession.setState(emptyState);
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
    useSession.setState(emptyState);
  });

  it("(b) apply({t:'status'}) updates status", () => {
    const { apply } = useSession.getState();
    apply({ t: "status", v: 1, status: "paused" });
    expect(useSession.getState().status).toBe("paused");
  });
});

describe("queued reconciliation", () => {
  beforeEach(() => {
    useSession.setState(emptyState);
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
    useSession.setState(emptyState);
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
    useSession.setState(emptyState);
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
    expect(branchRows[0].branchedFrom).toBeTruthy();

    // splice() on a branch row's span yields a non-empty lineage that
    // includes the parent prefix (≥ the branch's own content length).
    const branchSpan = getAgents(branchRows[0].spans[0])[0];
    const lineage = splice(tl.root, branchSpan);
    const ownContent = branchSpan.content.filter((c) => c.type === "event").length;
    expect(lineage.length).toBeGreaterThanOrEqual(ownContent);
  });

  it("emits ≥2 {t:'timeline'} ops (on BranchEvent + post-rollback target turns)", () => {
    const tlOps = rbMessages.filter((m) => m.t === "timeline");
    expect(tlOps.length).toBeGreaterThanOrEqual(2);
    // The last one should reflect the final tree (≥1 branch with content).
    const last = tlOps[tlOps.length - 1];
    expect(last.timeline.root.branches?.length ?? 0).toBeGreaterThanOrEqual(1);
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
    useSession.setState(emptyState);
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

describe("start does not add phantom Recents for child branch", () => {
  beforeEach(() => {
    useSession.setState(emptyState);
  });

  it("(e) state for a child branch does not add a Recents entry", () => {
    const { apply, start } = useSession.getState();

    // start() adds a pending Recents entry.
    start({ seed: "test", auditor_model: "m", target_model: "m" });

    // First state broadcast: backend assigns current = "branch-id-root" (root branch).
    const stateRoot: Down = {
      t: "state",
      v: 1,
      pool: [],
      events: [],
      span_role: {},
      queued: {},
      current: "branch-id-root",
      status: "idle",
      branches: {
        "branch-id-root": { parent: null, branched_at: null, branched_at_turn: null, status: "idle", seed: "test" },
      },
    };
    apply(stateRoot);

    // Second state broadcast: user branched; current switches to child branch.
    const stateChild: Down = {
      t: "state",
      v: 2,
      pool: [],
      events: [],
      span_role: {},
      queued: {},
      current: "branch-id-child",
      status: "idle",
      branches: {
        "branch-id-root": { parent: null, branched_at: null, branched_at_turn: null, status: "idle", seed: "test" },
        "branch-id-child": { parent: "branch-id-root", branched_at: "anc", branched_at_turn: 1, status: "idle", seed: "test" },
      },
    };
    apply(stateChild);

    // sessionsList should have at most 1 entry (the root), NOT 2.
    const { sessionsList } = useSession.getState();
    expect(sessionsList.length).toBeLessThanOrEqual(1);
    // The one entry should be for the root branch, not the child.
    if (sessionsList.length === 1) {
      expect(sessionsList[0].id).toBe("branch-id-root");
    }
  });
});
