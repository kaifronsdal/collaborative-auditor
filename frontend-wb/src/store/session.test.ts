import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { beforeEach, describe, expect, it } from "vitest";

import type { ChatMessage } from "@tsmono/inspect-common";

import { isModelEvent, resolveRole } from "../lib/events";
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
});

// ---------------------------------------------------------------------------
// New isolated tests — each resets store state in beforeEach to avoid leaking
// ---------------------------------------------------------------------------

const emptyState = {
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
        b0: { parent: null, branched_at: null, status: "idle", seed: "s" },
        b1: { parent: "b0", branched_at: "anc", status: "idle", seed: "s" },
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
      branches: { bq: { parent: null, branched_at: null, status: "idle", seed: "s" } },
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
        b0: { parent: null, branched_at: null, status: "paused", seed: "x" },
        b1: { parent: "b0", branched_at: "anc1", status: "idle", seed: "x" },
      },
    };
    apply(stateMsg);

    expect(useSession.getState().branches.b1.parent).toBe("b0");
  });
});

describe("start does not add phantom Recents for child branch", () => {
  beforeEach(() => {
    useSession.setState(emptyState);
  });

  it("(e) state for a child branch does not add a Recents entry", () => {
    const { apply, start } = useSession.getState();

    // start() adds a pending Recents entry.
    start({ seed: "test", auditor_model: "m", target_model: "m", max_turns: 3 });

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
        "branch-id-root": { parent: null, branched_at: null, status: "idle", seed: "test" },
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
        "branch-id-root": { parent: null, branched_at: null, status: "idle", seed: "test" },
        "branch-id-child": { parent: "branch-id-root", branched_at: "anc", status: "idle", seed: "test" },
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
