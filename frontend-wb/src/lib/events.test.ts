import type { ChatMessage, Event, ModelEvent, ToolCall } from "@tsmono/inspect-common";
import { describe, expect, it } from "vitest";

import { eventsToTurns, pairToolCalls } from "./events";

const sys = (text: string): ChatMessage => ({ role: "system", content: text, source: "input" });
const usr = (text: string, id?: string): ChatMessage => ({ role: "user", content: text, source: "input", id });
const asst = (text: string, id: string, tool_calls?: ToolCall[]): ChatMessage =>
  ({ role: "assistant", content: text, id, source: "generate", tool_calls }) as ChatMessage;
const tool = (id: string, content: string): ChatMessage =>
  ({ role: "tool", content, tool_call_id: id, source: "input" }) as ChatMessage;

let n = 0;
function model(input: ChatMessage[], out: ChatMessage | null, pending = false): ModelEvent {
  return {
    event: "model",
    uuid: `e${n++}`,
    timestamp: `2026-01-01T00:00:${String(n).padStart(2, "0")}Z`,
    span_id: "s",
    model: "m",
    input,
    output: { model: "m", choices: out ? [{ message: out, stop_reason: "stop" }] : [] },
    pending: pending || undefined,
    tools: [], config: {}, error: null, cache: null, call: null,
    completed: null, working_start: null, working_time: null, retries: null, role: null,
  } as unknown as ModelEvent;
}

describe("eventsToTurns (target — hasToolEvents=false)", () => {
  it("chunks resolved messages by assistant, one turn per ModelEvent", () => {
    const m1 = model([sys("S"), usr("u1")], asst("r1", "a1"));
    const m2 = model([sys("S"), usr("u1"), asst("r1", "a1"), usr("u2")], asst("r2", "a2"));
    const turns = eventsToTurns([m1, m2], false);
    expect(turns).toHaveLength(2);
    expect(turns[0].ev).toBe(m1);
    expect(turns[1].ev).toBe(m2);
    // turn 1 = system + u1 + r1; turn 2 = u2 + r2
    expect(turns[0].resolved.map((r) => r.message.role)).toEqual(["system", "user", "assistant"]);
    expect(turns[1].resolved.map((r) => r.message.role)).toEqual(["user", "assistant"]);
  });

  it("pairs assistant tool_calls with following ChatMessageTool via resolveMessages", () => {
    const a1 = asst("", "a1", [{ id: "tc1", function: "f", type: "function", arguments: {} }]);
    const m1 = model([sys("S"), usr("u1")], a1);
    const m2 = model([sys("S"), usr("u1"), a1, tool("tc1", "RESULT")], asst("r2", "a2"));
    const turns = eventsToTurns([m1, m2], false);
    const t1asst = turns[0].resolved.at(-1)!;
    expect(t1asst.message.role).toBe("assistant");
    expect(t1asst.toolMessages).toHaveLength(1);
    expect(t1asst.toolMessages[0].tool_call_id).toBe("tc1");
    const pairs = pairToolCalls(t1asst);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].result?.content).toBe("RESULT");
  });

  it("emits a turn for a pending event with no output yet", () => {
    const m1 = model([sys("S"), usr("u1")], asst("r1", "a1"));
    const m2 = model([sys("S"), usr("u1"), asst("r1", "a1"), usr("u2")], null, true);
    const turns = eventsToTurns([m1, m2], false);
    expect(turns).toHaveLength(2);
    expect(turns[1].ev).toBe(m2);
    // turn 2's bucket is the trailing user (output hasn't landed)
    expect(turns[1].resolved.map((r) => r.message.role)).toEqual(["user"]);
  });
});

describe("eventsToTurns (auditor — hasToolEvents=true)", () => {
  it("filters tool messages from convo and attaches ToolEvents per turn", () => {
    const a1 = asst("", "a1", [{ id: "c1", function: "send_message", type: "function", arguments: {} }]);
    const m1 = model([sys("S"), usr("seed")], a1);
    const te1 = { event: "tool", uuid: "t1", id: "c1", function: "send_message", arguments: {}, result: "ok" } as unknown as Event;
    const m2 = model([sys("S"), usr("seed"), a1, tool("c1", "ok")], asst("", "a2"));
    const turns = eventsToTurns([m1, te1, m2], true);
    expect(turns).toHaveLength(2);
    expect(turns[0].tools).toHaveLength(1);
    expect(turns[0].tools[0].function).toBe("send_message");
    // tool message filtered → assistant has no toolMessages (rendered via ToolEvent instead)
    expect(turns[0].resolved.at(-1)!.toolMessages).toHaveLength(0);
    expect(turns[1].tools).toHaveLength(0);
  });
});
