/**
 * M1 orchestrator column types (M1-NOTEBOOK.md).
 *
 * The orchestrator's kernel emits `DisplayEvent`s which the backend wraps as
 * `InfoEvent(source="orchestrator", data={id, turn, bundle, meta, stable})` and
 * ships through the same `event`/`update` pipe as M0 `ModelEvent`s. The bundle
 * is IPython's MIME dict; workbench objects add a vendor key that the frontend
 * dispatches on for rich cards.
 *
 * Per-`kind` payload interfaces below mirror `workbench/m1/wire.py` one-for-one
 * (M1-REFACTOR.md Batch C). Adding a `kind` on the Python side means adding a
 * `TypedDict` there, an interface here, and a `WbPayload` union arm in both.
 */
import type {
  ChatMessage,
  InfoEvent,
  ModelEvent,
  ToolEvent,
} from "@tsmono/inspect-common";

export const ORCH_SOURCE = "orchestrator";
export const WB_MIME = "application/vnd.workbench.v1+json";
export const STREAM_MIME = "application/vnd.jupyter.stream+json";

/** IPython's MIME bundle. Known keys typed; anything else survives as unknown. */
export type DisplayBundle = {
  "application/vnd.workbench.v1+json"?: WbPayload;
  "application/vnd.jupyter.stream+json"?: { name: "stdout" | "stderr"; text: string };
  "text/html"?: string;
  "text/markdown"?: string;
  "text/plain"?: string;
  "image/svg+xml"?: string;
  "image/png"?: string;
  [mime: string]: unknown;
};

// -- supporting shapes --------------------------------------------------------

export type SampleRowPayload = {
  id: string;
  status: "running" | "done" | "error" | "stopped";
  /** Absent on rows folded from `wb_display` protocol lines. */
  epoch?: number;
  input: string;
  turns?: number | null;
  scores: Record<string, unknown>;
  error?: string | null;
};

export type QuotePayload = {
  sample_id: string;
  at: number;
  role: string;
  text: string;
  log?: string;
};

export type TracebackFrame = { file: string; lineno: number | null; line: string | null };

// -- gate cards ---------------------------------------------------------------

export type PromptPayload = {
  kind: "prompt";
  id: string;
  pending: boolean;
  verdict: { answer: string } | null;
  question: string;
  options: string[] | null;
  answer: string | null;
  answered_at: string | null;
};

export type RunProposalPayload = {
  kind: "run_proposal";
  id: string;
  pending: boolean;
  verdict: { denied?: boolean; surviving?: string[]; reason?: string } | null;
  description: string;
  n: number;
  n_per_seed: number;
  seeds: Array<{ id: string; text: string }>;
  config: { model?: string; max_turns?: number; n_per_seed: number } & Record<
    string,
    unknown
  >;
};

export type CiteProposalPayload = {
  kind: "cite_proposal";
  id: string;
  pending: boolean;
  verdict: { signed?: boolean; by?: string; reason?: string } | null;
  claim: string;
  quotes: QuotePayload[];
  description: string;
};

export type FindingPayload = {
  kind: "finding";
  id: string;
  claim: string;
  quotes: QuotePayload[];
  signed_by: string | null;
};

// -- progress cards -----------------------------------------------------------

export type EvalRunPayload = {
  kind: "eval_run";
  id: string;
  task: string;
  description: string;
  log_dir: string;
  log: string | null;
  total: number;
  done: number;
  finished: boolean;
  error: string | null;
  elapsed?: string;
  rows: { running: SampleRowPayload[]; done: SampleRowPayload[] };
  /** First numeric score per row, positional with `rows.done` (§8). */
  scores?: (number | null)[];
};

export type ScanPayload = {
  kind: "scan";
  id: string;
  description: string;
  scans_dir: string;
  location: string | null;
  done: number;
  total: number;
  finished: boolean;
  error: string | null;
  per_scanner: Record<string, { scans: number; results: number; errors: number }>;
  df_head?: Record<string, string>;
};

// -- reader cards -------------------------------------------------------------

export type TranscriptPayload = {
  kind: "transcript";
  log: string;
  sample_id: string;
  at: number | null;
  n_messages: number;
  preview?: ChatMessage[];
};

export type ExcerptPayload = {
  kind: "excerpt";
  log: string;
  sample_id: string;
  at: number;
  /** Index into `messages` of the message *at* turn `at` — the anchor. */
  at_idx: number;
  messages: ChatMessage[];
};

// -- kernel-emitted -----------------------------------------------------------

export type TracebackPayload = {
  kind: "traceback";
  ename: string;
  evalue: string;
  frames: TracebackFrame[];
  text: string;
};

export type CellDonePayload = {
  kind: "cell_done";
  turn: number;
  duration: number;
  new_names: string[];
  interrupted: boolean;
  ns: Record<string, string>;
};

export type BgDonePayload = {
  kind: "bg_done";
  id: string;
  pid: number;
  exit: number;
};

/** Carried in `InfoEvent.data` directly (not under `bundle[WB_MIME]`). */
export type RewindMarkerPayload = {
  kind: "rewind_marker";
  to_turn: number;
};

// -- union --------------------------------------------------------------------

/**
 * The `application/vnd.workbench.v1+json` payload — discriminated on `kind`.
 * Mirrors `workbench.m1.wire.WbPayload`. Closed: an unregistered `kind` from a
 * backend that outruns the frontend build still renders (`cards[kind]` misses
 * → `<WbFallback>`), it just isn't type-narrowable here — which is the point.
 */
export type WbPayload =
  | PromptPayload
  | RunProposalPayload
  | CiteProposalPayload
  | FindingPayload
  | EvalRunPayload
  | ScanPayload
  | TranscriptPayload
  | ExcerptPayload
  | TracebackPayload
  | CellDonePayload
  | BgDonePayload
  | RewindMarkerPayload;

// -- wire wrappers ------------------------------------------------------------

/** `InfoEvent.data` as emitted by `Orchestrator._on_display`. */
export type DisplayData = {
  /** IPython `display_id` (== `InfoEvent.uuid` when `stable`). */
  id: string;
  /** 1-based orchestrator turn that emitted this output — the grouping key. */
  turn: number;
  bundle: DisplayBundle;
  meta: Record<string, unknown>;
  /** Emitted with a stable `display_id`; later `update`s replace in place. */
  stable: boolean;
};

/** Narrow an `InfoEvent` to one carrying orchestrator display data. */
export type DisplayInfoEvent = InfoEvent & { data: DisplayData };

/**
 * One rendered orchestrator turn: assistant prose → tool cell(s) → outputs →
 * traceback. Derived from the orch span's `ModelEvent` + every following
 * `ToolEvent` + every `DisplayInfoEvent` whose `data.turn` matches.
 */
export type OrchTurnData = {
  /** 1-based turn ordinal (backend counts from 1; matches `data.turn`). */
  turn: number;
  model: ModelEvent;
  /** The user message(s) that preceded this generate — the human's ask. */
  userInput: ChatMessage[];
  /** All tool calls this turn (M1-HYBRID.md — `python`/`bash`/`read_file`/
   *  `write_file`/`edit_file`/`ask_human`/`review_seeds`/`review_finding`).
   *  Empty when the model replied without one (final "done" turn — prose
   *  only). Rendered by `<ToolCell>` dispatching on `.function`. */
  tools: ToolEvent[];
  /** Display outputs in emission order (already latest-wins for stable ids —
   *  the store's `update` reducer replaces the event in place by uuid). */
  outputs: DisplayInfoEvent[];
};
