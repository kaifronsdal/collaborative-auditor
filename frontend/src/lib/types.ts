/**
 * Shared types for the Collaborative Auditor Interface.
 * These mirror the Python backend types.
 */

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export interface ToolCall {
  id: string;
  function: string;
  arguments: Record<string, unknown>;
  type?: string;
}

export interface ChatMessage {
  id: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string | ContentPart[];
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  function?: string;
  error?: {
    type: string;
    message: string;
  };
  metadata?: {
    source?: string;
    edited?: boolean;
    prefill?: boolean;
    turn_id?: string;
  };
}

/**
 * Content type interfaces matching inspect_ai's ChatMessage content structure.
 * Content can be a string OR an array of ContentPart objects.
 */

// Base content interface
export interface BaseContent {
  type: string;
  internal?: unknown;
}

// Text content
export interface ContentText extends BaseContent {
  type: "text";
  text: string;
  refusal?: boolean | null;
}

// Reasoning/thinking content (from reasoning models like Claude)
export interface ContentReasoning extends BaseContent {
  type: "reasoning";
  reasoning: string; // The actual reasoning/thinking content
  summary?: string | null; // Human-readable summary when reasoning is redacted
  signature?: string | null;
  redacted?: boolean; // Whether the reasoning is redacted/encrypted
}

// Image content
export interface ContentImage extends BaseContent {
  type: "image";
  image: string;
  detail?: "auto" | "low" | "high";
}

// Audio content
export interface ContentAudio extends BaseContent {
  type: "audio";
  audio: string;
  format: "wav" | "mp3";
}

// Video content
export interface ContentVideo extends BaseContent {
  type: "video";
  video: string;
  format: "mp4" | "mpeg" | "mov";
}

// Data content
export interface ContentData extends BaseContent {
  type: "data";
  data: Record<string, unknown>;
}

// Document content
export interface ContentDocument extends BaseContent {
  type: "document";
  document: string;
  filename?: string;
  mime_type?: string;
}

// Tool use content (embedded tool execution)
export interface ContentToolUse extends BaseContent {
  type: "tool_use";
  tool_type: string;
  id: string;
  name: string;
  arguments: unknown;
  result: unknown;
  context?: string | null;
  error?: string | null;
}

// Union of all content types
export type ContentPart =
  | ContentText
  | ContentReasoning
  | ContentImage
  | ContentAudio
  | ContentVideo
  | ContentData
  | ContentDocument
  | ContentToolUse;

export interface TargetState {
  messages: ChatMessage[];
  tools: ToolDefinition[];
}

export type BranchPointType = "turn" | "tool_call" | "target";

export type PlaybackState = "idle" | "playing" | "stepping" | "paused";

// ============= ViewState (from server) =============

export interface BranchSummary {
  id: string;
  message_count: number;
}

export interface ComputedBranchPoint {
  id: string;
  branch_type: BranchPointType;
  event_id: string;
  message_id?: string;
  tool_call_id?: string;
  turn_id?: string;
  current_index: number;  // pre-computed: where is current branch in branch_ids
  total_branches: number; // pre-computed
  branch_ids: string[];
}

export interface ViewState {
  session_id: string;
  initial_prompt: string;
  auditor_model: string;
  target_model: string;
  current_branch: {
    id: string;
    auditor_messages: ChatMessage[];
    target_state: TargetState;
  };
  branches: BranchSummary[];
  current_branch_index: number;
  branch_points: ComputedBranchPoint[];
  playback_state: PlaybackState;
  is_generating: boolean;
  version: number;
}

// ============= Server Messages (5 types) =============

export type ServerMessage =
  | { type: "state"; state: ViewState }
  | { type: "delta_turn_start"; message: ChatMessage; branch_id: string; version: number }
  | { type: "delta_tool_call"; tool_call: ToolCall; branch_id: string; version: number }
  | { type: "delta_tool_result"; tool_result: ChatMessage; target_state: TargetState; branch_id: string; version: number }
  | {
      type: "rewrite_tool_call_result";
      request_id: string;
      tool_call_id: string;
      rewritten_arguments?: Record<string, unknown>;
      error?: string;
    }
  | { type: "error"; message: string };

// ============= Client Messages =============

export type ClientMessage =
  | { type: "start_session"; initial_prompt: string; auditor_model: string; target_model: string }
  | { type: "play" }
  | { type: "pause" }
  | { type: "step" }
  | { type: "feedback"; content: string }
  | { type: "branch"; message_id: string }
  | { type: "switch_branch"; branch_id: string }
  | { type: "edit_message"; message_id: string; new_content: string }
  | { type: "resample_turn"; turn_id: string }
  | { type: "edit_tool_call"; tool_call_id: string; new_arguments: Record<string, unknown> }
  | {
      type: "rewrite_tool_call";
      request_id: string;
      tool_call_id: string;
      instruction: string;
      selected_text?: string;
      target_field?: string;
    }
  | { type: "resample_target_response"; target_message_id: string; tool_call_id?: string }
  | { type: "edit_initial_prompt"; new_content: string };
