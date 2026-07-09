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

/** P2 bg-job panel — one `bash(background=True)` proc or eval run. */
export type BgJob = {
  kind: "bash" | "eval";
  /** `bg-XXXXXX` handle (bash) or `eval_id` / `log_dir` (eval). */
  id: string;
  /** The shell command (bash) or task name (eval). */
  cmd_or_task: string;
  status: "running" | "done" | "error" | "attached";
  pid?: number;
  log_dir?: string;
  /** `time.time()` at spawn / `eval_start`; null for resumed run dirs. */
  started_at?: number | null;
};

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
  /** P2: every tracked `bash(background=True)` + eval run across the session. */
  bg_jobs?: BgJob[];
  /** Undelivered `[cell-N done · …]` chips (drained into next agent input). */
  notifications: string[];
  /** PRODUCT-GAPS P2: `sum(len(m.text) for m in state.messages)`. */
  context_chars?: number;
  /** Rough context-window ceiling in chars (backend default 200k). */
  context_limit?: number;
};

/** Branch metadata included in `state` broadcasts. */
export type BranchMeta = {
  parent: string | null;
  branched_at: string | null;
  /** Auditor turn index at the slice — stable sibling key (anchor ids re-mint on edit). */
  branched_at_turn: number | null;
  status: Status;
  /** Which column (if any) is mid-generate. `null` = neither; absent = old backend. */
  generating?: "auditor" | "target" | null;
  seed: string;
  /** Resample-N batch this branch belongs to, or null for a normal fork. */
  batch?: string | null;
  /** E1: `Branch.run()`'s terminal error string, or null. Sidebar red-dot. */
  error?: string | null;
  /** A1-b-wide: this branch's L1 (target-`History`) trajectory span ids.
   *  The target column's session-wide swimlane picks its default lane by
   *  matching row span ids against this set; picking a foreign lane resolves
   *  its owning branch via the reverse map for `switchBranch`. */
  l1_spans?: string[];
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
      generating?: "auditor" | "target" | null;
      branches: Record<BranchId, BranchMeta>;
      candidate_batches?: Record<string, CandidateBatch>;
      timelines?: TimelineMap;
      orchestrator?: OrchestratorState | null;
    }
  | { t: "pool"; v: number; from: number; entries: ChatMessage[] }
  | { t: "event"; v: number; event: Event }
  | { t: "update"; v: number; event: Event }
  | {
      t: "status";
      v: number;
      status: Status | null;
      /** RACE-FIXES.md R3: which column of the current branch is
       *  mid-generate. `null` = neither; absent = old backend (fall back to
       *  `status === "running"` for the shimmer gate). */
      generating?: "auditor" | "target" | null;
      orch_status?: Status | null;
    }
  | { t: "notify"; v: number; text: string }
  | {
      t: "queued";
      v: number;
      branch: BranchId;
      role: "auditor" | "target";
      message: ChatMessage;
    }
  | {
      t: "unqueued";
      v: number;
      branch: BranchId;
      role: "auditor" | "target";
      message_id: string;
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
  // §2 rewind: backend marks every event in `span`'s role bucket at/after
  // `from_uuid` (insertion order) as rewound; the frontend adds their uuids to
  // `state.rewound` and `eventsToOrchTurns` skips them. On reconnect each such
  // event carries a top-level `rewound: true` (works for ModelEvent/ToolEvent
  // too, which have no `.data`).
  | { t: "rewound"; v: number; span: string; from_uuid: string }
  // P2 session fork: server closed the parent, minted + registered a new
  // Session seeded from `fork_seed(at_turn)`; frontend navigates to it.
  | { t: "forked"; session_id: string }
  | { t: "error"; v: number; message: string }
  // A3-batch (ARCHITECTURE-RACES.md): `_on_event` ships pool + event/update
  // + timeline as one atomic frame so the reducer applies them in a single
  // `set()`. Ops are any non-batch `Down`; `v` is shared across the batch.
  | { t: "batch"; v: number; ops: Exclude<Down, { t: "batch" }>[] }
  // -- A3-typed-deltas (ARCHITECTURE-RACES.md) --
  // Replace mid-session `{t:"state"}` with narrow deltas so `case "state"`
  // is connect-only. Each carries `v` and goes through the drain queue
  // (same FIFO as `{t:"batch"}`), so `v` is monotone on the wire.
  | {
      t: "branch_created";
      v: number;
      id: BranchId;
      meta: BranchMeta;
      /** The two `span_role` entries this branch registered — the client
       *  merges them so events for this branch route via `resolveRole`.
       *  Sent BEFORE the branch's `run()` spawns (adversarial-review
       *  caveat #1) so replay events are never unbucketed. */
      span_role_delta: Record<string, [BranchId, Role]>;
      /** `session.current` after the fork (repointed for `_register_and_
       *  spawn`; unchanged — the parent — for `_candidates`). */
      current: BranchId | null;
    }
  | { t: "current"; v: number; branch: BranchId | null }
  | {
      t: "batch_resolved";
      v: number;
      batch: string;
      picked: BranchId | null;
      /** Cancelled sibling ids (adversarial-review caveat #3) — the sidebar
       *  flips their status dot without a full `view()`. */
      ended: BranchId[];
    }
  | {
      t: "orch";
      v: number;
      state: OrchestratorState;
      /** `("orch","orch")` registration (adversarial-review caveat #4). */
      span_role_delta: Record<string, [BranchId, Role]>;
    }
  | { t: "queued_consumed"; v: number; branch: BranchId; ids: string[] }
  // F2 (A1-b-wide follow-up): a live L1 rollback (`BranchEvent` on a
  // target span) grew the branch's L1-`History` tree. `l1_spans` was
  // otherwise only shipped on `state` / `branch_created`, so
  // `SwimlaneColumn.defaultKey` would pick the pre-rollback lane until
  // the next full snapshot. Ships inside the same `{t:"batch"}` as the
  // `BranchEvent`'s `{t:"timeline"}` op.
  | { t: "l1_spans"; v: number; branch: BranchId; l1_spans: string[] }
  // A2 (ARCHITECTURE-RACES.md): `_dispatch`'s `finally` echoes the
  // client's `req_id` once the handler returns. The reducer drops the
  // matching entry from `store.pending`; `useIsPending` re-enables the
  // button. No `v` — this is a per-connection sideband, not a structural
  // delta (it carries no state and needn't order against the drain queue).
  | { t: "ack"; req_id: string };

/** P2 — optional per-fork model overrides. Any fork-shaped command
 *  (`branch`/`resample`/`*_auditor`/`edit_*`) may carry these; unset fields
 *  inherit `parent.meta` verbatim. Lets a fork A/B a different target model
 *  mid-tree — the replayed prefix is served from tape so the swap only
 *  affects post-branch-point generates. UI wiring deferred. */
export type ForkOverrides = {
  auditor_model?: string;
  target_model?: string;
  auditor_config?: Record<string, unknown>;
  target_config?: Record<string, unknown>;
};

/** P1.2 — per-session defaults for the subprocess audit roles. Interpolated
 *  into the orchestrator's system prompt (so it sees concrete `provider/model`
 *  ids, not `{target}` placeholders) and exposed as `wb.DEFAULTS` in the
 *  kernel namespace. */
export type AuditDefaults = {
  target: string;
  auditor: string;
  judge: string;
  target_config?: Record<string, unknown>;
  auditor_config?: Record<string, unknown>;
  judge_config?: Record<string, unknown>;
  max_turns?: number;
  judge_dimensions?: string;
};

/** A2: every outbound command may carry a client-generated `req_id`; the
 *  server echoes it as `{t:"ack", req_id}` once the handler returns.
 *  `store.send()` always attaches one; it's optional on the wire so
 *  non-store callers (tests, chaos harness) needn't. */
export type Up = { req_id?: string } & (
  | {
      t: "start";
      seed: string;
      auditor_model: string;
      target_model: string;
      max_turns?: number;
      auditor_config?: Record<string, unknown>;
      target_config?: Record<string, unknown>;
      auditor_model_args?: Record<string, unknown>;
      target_model_args?: Record<string, unknown>;
      live_scanners?: string[];
    }
  // `target`: branch id, "orch" for the orchestrator, or omitted → `current`.
  | { t: "step"; target?: string }
  | { t: "play"; target?: string }
  | { t: "pause"; target?: string }
  | { t: "end" }
  | { t: "inject"; branch: BranchId; role: Role; message: ChatMessage }
  | { t: "unqueue"; branch: BranchId; role: "auditor" | "target"; message_id: string }
  | ({ t: "branch"; at: string } & ForkOverrides)
  | ({ t: "resample"; at: string } & ForkOverrides)
  | ({ t: "branch_auditor"; branch: BranchId; turn_index: number } & ForkOverrides)
  | ({ t: "resample_auditor"; branch: BranchId; turn_index: number } & ForkOverrides)
  | ({
      t: "edit_auditor_call";
      branch: BranchId;
      turn_index: number;
      call_id: string;
      args: Record<string, unknown>;
    } & ForkOverrides)
  | {
      t: "rewrite_tool_call";
      branch: BranchId;
      turn_index: number;
      call_id: string;
      instruction: string;
      selected_text?: string;
    }
  | ({
      t: "edit_target_message";
      branch: BranchId;
      message_id: string;
      role: "user" | "system" | "tool";
      content: string;
      tool_call_id?: string;
    } & ForkOverrides)
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
  | {
      t: "start_orchestrator";
      model: string;
      system_prompt?: string;
      /** P1.1/P1.5: open `GenerateConfig` dict for the orchestrator model. */
      config?: Record<string, unknown>;
      /** P1.4: provider kwargs (`base_url`, `api_key`, …) for `get_model`. */
      model_args?: Record<string, unknown>;
      /** P1.2: audit-role defaults → system-prompt interpolation + `wb.DEFAULTS`. */
      audit_defaults?: AuditDefaults;
    }
  | { t: "orch_send"; text: string }
  | { t: "approve"; display_id: string; verdict?: unknown }
  // STRESS-V2 s13: server iterates `orch.gate.pending` — one message
  // instead of N `{t:"approve"}` from a stale client snapshot.
  | { t: "approve_all" }
  | { t: "detach_cell" }
  | { t: "cancel_cell"; turn: number }
  // P2 bg-job panel: SIGTERM one tracked ``bash`` subprocess by its bg-id.
  | { t: "cancel_bg"; id: string }
  | { t: "rewind"; turn: number }
  // P2 session fork: branch the orchestrator conversation at `at_turn` into
  // a fresh Session (own session_dir, kernel, findings). Server replies
  // `{t:"forked", session_id}` → navigate.
  | { t: "fork_orchestrator"; at_turn: number }
  // P2: drop `user_ns` (re-seed `wb`/analysis names), keep `state.messages`.
  | { t: "restart_kernel" }
  | { t: "interrupt_and_send"; turn: number; text: string }
  | { t: "stop_sample"; id: string; log_dir: string }
  | { t: "import_running"; sample_id: string; log_dir: string }
  // -- P1.8(b): manual scan of an M0 branch's target conversation --
  | {
      t: "scan_branch";
      branch_id: BranchId;
      /** Scanner or group name (anything `m1.scanners.resolve` accepts). */
      scanner: string;
      /** Whole conversation, or up to (and including) message index N. */
      scope: "transcript" | { turn: number };
    }
);

/** One entry from `GET /sessions` — a persisted session on disk. */
export type SavedSession = {
  session_id: string;
  seed: string;
  created_at: string;
  n_branches: number;
};
