/**
 * M1 orchestrator column types (M1-NOTEBOOK.md).
 *
 * The orchestrator's kernel emits `DisplayEvent`s which the backend wraps as
 * `InfoEvent(source="orchestrator", data={id, turn, bundle, meta, stable})` and
 * ships through the same `event`/`update` pipe as M0 `ModelEvent`s. The bundle
 * is IPython's MIME dict; workbench objects add a vendor key that the frontend
 * dispatches on for rich cards.
 */
import type {
  ChatMessage,
  InfoEvent,
  ModelEvent,
  ToolEvent,
} from "@tsmono/inspect-common";

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

/**
 * The `application/vnd.workbench.v1+json` payload — discriminated on `kind`.
 * Fields beyond `kind` are card-specific; the shared ones are typed here so
 * `<Output>` can render the header/gate chrome without knowing the card.
 */
export type WbPayload =
  | ({ kind: "run_proposal" } & WbGated)
  | ({ kind: "audit_run" } & WbBase)
  | ({ kind: "eval_run" } & WbBase)
  | ({ kind: "scan" } & WbBase)
  | ({ kind: "receipt" } & WbBase)
  | ({ kind: "prompt" } & WbGated)
  | ({ kind: "cite_proposal" } & WbGated)
  | ({ kind: "finding" } & WbBase)
  | ({ kind: "excerpt" } & WbBase)
  | ({ kind: "transcript" } & WbBase)
  // Forward-compatible: unrecognised kinds fall through to the JSON dump.
  | ({ kind: string } & WbBase);

type WbBase = {
  /** Still awaiting a `dh.update()` (progress cards) — spinner state. */
  pending?: boolean;
  /** Gated card's approve/deny one-liner (`description=` on `run_audits` etc). */
  description?: string;
  [k: string]: unknown;
};
type WbGated = WbBase & { pending: boolean };

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

export const ORCH_SOURCE = "orchestrator";
export const WB_MIME = "application/vnd.workbench.v1+json";
export const STREAM_MIME = "application/vnd.jupyter.stream+json";

/**
 * One rendered orchestrator turn: assistant prose → code cell → outputs →
 * traceback. Derived from the orch span's `ModelEvent` + its
 * `ToolEvent(function="python")` + every `DisplayInfoEvent` whose
 * `data.turn` matches.
 */
export type OrchTurnData = {
  /** 1-based turn ordinal (backend counts from 1; matches `data.turn`). */
  turn: number;
  model: ModelEvent;
  /** The user message(s) that preceded this generate — the human's ask. */
  userInput: ChatMessage[];
  /** The `python(code=…)` call. Absent when the model replied without one
   *  (final "done" turn — prose only). */
  py?: ToolEvent;
  /** Display outputs in emission order (already latest-wins for stable ids —
   *  the store's `update` reducer replaces the event in place by uuid). */
  outputs: DisplayInfoEvent[];
};
