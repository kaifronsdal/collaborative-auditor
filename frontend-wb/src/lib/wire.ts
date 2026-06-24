/**
 * The workbench WebSocket wire protocol (STREAMING.md §C).
 *
 * `Event` / `ChatMessage` come from `@tsmono/inspect-common` (auto-generated
 * from inspect_ai's Pydantic models, so they track inspect by construction).
 * A `ModelEvent` carried on `event` / `update` has its `input` emptied and an
 * `input_refs` range list pointing into the pool; the frontend resolves it with
 * `expandEvents`.
 */
import type { ChatMessage, Event, ModelOutput, Timeline } from "@tsmono/inspect-common";

/** Server-built `Timeline` (petri's `build_target_timeline`), event refs as UUIDs. */
export type ServerTimeline = Timeline;
export type TimelineMap = Record<BranchId, Partial<Record<Role, ServerTimeline>>>;

export type Role = "auditor" | "target";
export type BranchId = string;
export type Status = "idle" | "running" | "paused" | "ended";

/** Per-branch, per-role queued (user-injected, not yet generated) messages. */
export type QueuedMap = Record<BranchId, Record<Role, ChatMessage[]>>;

/** Branch metadata included in `state` broadcasts. */
export type BranchMeta = {
  parent: string | null;
  branched_at: string | null;
  status: Status;
  seed: string;
};

export type Down =
  | {
      t: "state";
      v: number;
      pool: ChatMessage[];
      events: Event[];
      span_role: Record<string, [BranchId, Role]>;
      queued: QueuedMap;
      current: string | null;
      status: Status | null;
      branches: Record<BranchId, BranchMeta>;
      timelines?: TimelineMap;
    }
  | { t: "pool"; v: number; from: number; entries: ChatMessage[] }
  | { t: "event"; v: number; event: Event }
  | { t: "update"; v: number; event: Event }
  | { t: "status"; v: number; status: Status | null }
  | {
      t: "queued";
      v: number;
      branch: BranchId;
      role: Role;
      message: ChatMessage;
    }
  | { t: "timeline"; v: number; branch: BranchId; role: Role; timeline: ServerTimeline }
  | { t: "error"; v: number; message: string };

export type Up =
  | {
      t: "start";
      seed: string;
      auditor_model: string;
      target_model: string;
      max_turns?: number;
      auditor_config?: Record<string, unknown>;
      target_config?: Record<string, unknown>;
    }
  | { t: "step" }
  | { t: "play" }
  | { t: "pause" }
  | { t: "end" }
  | { t: "inject"; branch: BranchId; role: Role; message: ChatMessage }
  | { t: "branch"; at: string }
  | { t: "resample"; at: string }
  | { t: "edit"; at: string; output: ModelOutput }
  | { t: "branch_auditor"; branch: BranchId; turn_index: number }
  | { t: "resample_auditor"; branch: BranchId; turn_index: number }
  | {
      t: "edit_auditor_call";
      branch: BranchId;
      turn_index: number;
      call_id: string;
      args: Record<string, unknown>;
    }
  | {
      t: "edit_target_message";
      branch: BranchId;
      message_id: string;
      role: "user" | "system" | "tool";
      content: string;
      tool_call_id?: string;
    }
  | { t: "switch"; branch: BranchId };
