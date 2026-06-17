import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

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
