/**
 * The workbench WebSocket wire protocol (STREAMING.md §C).
 *
 * `Event` / `ChatMessage` come from `@tsmono/inspect-common` (auto-generated
 * from inspect_ai's Pydantic models, so they track inspect by construction).
 * A `ModelEvent` carried on `event` / `update` has its `input` emptied and an
 * `input_refs` range list pointing into the pool; the frontend resolves it with
 * `expandEvents`.
 */
import type { ChatMessage, Event, Timeline } from "@tsmono/inspect-common";

/** Server-built `Timeline` (petri's `build_target_timeline`), event refs as UUIDs. */
export type ServerTimeline = Timeline;
export type TimelineMap = Record<BranchId, Partial<Record<Role, ServerTimeline>>>;

/** Column role. `"orch"` is the M1 orchestrator column — it lives under the
 *  synthetic branch id `"orch"` (backend registers `span_role[orch_span] =
 *  ["orch","orch"]`), so orchestrator events route through the same
 *  `byRole` bucketing as auditor/target. */
export type Role = "auditor" | "target" | "orch";
export type BranchId = string;
/** `"waiting"` is orchestrator-only: `running` with ≥1 unresolved kernel gate
 *  — the header renders "waiting on you" and pulses the gate accent. */
export type Status = "idle" | "running" | "paused" | "ended" | "waiting";

/** Per-branch, per-role queued (user-injected, not yet generated) messages.
 *  Orchestrator input goes via `orch_send` (immediate), not the queued map. */
export type QueuedMap = Record<BranchId, Record<"auditor" | "target", ChatMessage[]>>;

/** M1 orchestrator state, from `Session.view()["orchestrator"]`. */
export type OrchestratorState = {
  span_id: string;
  status: Status;
  /** `Orchestrator.model_name` — header shows the short form. */
  model?: string;
  /** `display_id`s awaiting `{t:"approve"}` — drives `[approve all]` pill. */
  pending_gates: string[];
  /** Turn ids of cells still running detached. */
  bg_cells: number[];
  /** Undelivered `[cell-N done · …]` chips (drained into next agent input). */
  notifications: string[];
};

/** Branch metadata included in `state` broadcasts. */
export type BranchMeta = {
  parent: string | null;
  branched_at: string | null;
  /** Auditor turn index at the slice — stable sibling key (anchor ids re-mint on edit). */
  branched_at_turn: number | null;
  status: Status;
  seed: string;
  /** Resample-N batch this branch belongs to, or null for a normal fork. */
  batch?: string | null;
  /** Per-rubric scores (M2's `grade()` worker; unset until then). */
  grades?: Record<string, number>;
};

/** One Resample-N request: N background sibling forks at one anchor. */
export type CandidateBatch = {
  parent: BranchId;
  anchor: string;
  kind: Role;
  children: BranchId[];
  picked: BranchId | null;
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
      candidate_batches?: Record<string, CandidateBatch>;
      timelines?: TimelineMap;
      orchestrator?: OrchestratorState | null;
    }
  | { t: "pool"; v: number; from: number; entries: ChatMessage[] }
  | { t: "event"; v: number; event: Event }
  | { t: "update"; v: number; event: Event }
  | { t: "status"; v: number; status: Status | null; orch_status?: Status | null }
  | { t: "notify"; v: number; text: string }
  | {
      t: "queued";
      v: number;
      branch: BranchId;
      role: "auditor" | "target";
      message: ChatMessage;
    }
  | { t: "timeline"; v: number; branch: BranchId; role: Role; timeline: ServerTimeline }
  | {
      t: "rewrite_draft";
      v: number;
      branch: BranchId;
      /** Auditor-side rewrite key (tool_call id). */
      call_id?: string;
      /** Target-side rewrite key (the edited target message's id). */
      message_id?: string;
      args?: Record<string, unknown>;
      /** Target-side only: the staging-arg text that becomes the visible
       *  message — what `edit_target_message` would be applied with. */
      content?: string;
      raw?: string;
      error?: string;
    }
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
  // `target`: branch id, "orch" for the orchestrator, or omitted → `current`.
  | { t: "step"; target?: string }
  | { t: "play"; target?: string }
  | { t: "pause"; target?: string }
  | { t: "end" }
  | { t: "inject"; branch: BranchId; role: Role; message: ChatMessage }
  | { t: "branch"; at: string }
  | { t: "resample"; at: string }
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
      t: "rewrite_tool_call";
      branch: BranchId;
      turn_index: number;
      call_id: string;
      instruction: string;
      selected_text?: string;
    }
  | {
      t: "edit_target_message";
      branch: BranchId;
      message_id: string;
      role: "user" | "system" | "tool";
      content: string;
      tool_call_id?: string;
    }
  | {
      t: "rewrite_target_message";
      branch: BranchId;
      message_id: string;
      role: "user" | "system" | "tool";
      instruction: string;
      selected_text?: string;
      tool_call_id?: string;
    }
  | { t: "switch"; branch: BranchId }
  | { t: "candidates"; branch: BranchId; at: string; n: number }
  | { t: "candidates_auditor"; branch: BranchId; turn_index: number; n: number }
  | { t: "pick_candidate"; batch: string; branch: BranchId }
  | { t: "dismiss_candidates"; batch: string }
  | { t: "export"; branch: BranchId; path: string }
  | { t: "import"; path: string; sample_id?: string }
  // -- M1 orchestrator (M1-NOTEBOOK.md) --
  | { t: "start_orchestrator"; model: string; system_prompt?: string }
  | { t: "orch_send"; text: string }
  | { t: "approve"; display_id: string; verdict?: unknown }
  | { t: "detach_cell" }
  | { t: "cancel_cell"; turn: number }
  | { t: "import_running"; sample_id: string; log?: string };

/** One entry from `GET /sessions` — a persisted session on disk. */
export type SavedSession = {
  session_id: string;
  seed: string;
  created_at: string;
  n_branches: number;
};
