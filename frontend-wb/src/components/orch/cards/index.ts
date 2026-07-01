/**
 * Registry keyed on `bundle["application/vnd.workbench.v1+json"].kind`.
 * `<Output>` looks up `cards[payload.kind]` and renders it with
 * `{payload, displayId, send}`; unknown kinds fall through to the plain
 * `text/plain` / `text/html` renderer.
 */
import type { ComponentType } from "react";

import type { Up } from "../../../lib/wire";
import ExcerptCard from "./ExcerptCard";
import PromptCard from "./PromptCard";
import RunCard from "./RunCard";
import RunProposalCard from "./RunProposalCard";
import ScanCard from "./ScanCard";
import TranscriptCard from "./TranscriptCard";

export type { ExcerptPayload } from "./ExcerptCard";
export type { PromptPayload } from "./PromptCard";
export type { RunPayload } from "./RunCard";
export type { RunProposalPayload } from "./RunProposalCard";
export type { ScanPayload } from "./ScanCard";
export type { TranscriptPayload } from "./TranscriptCard";

/** Shared prop contract — `payload` is narrowed per-card, so the registry
 *  types it loosely and the caller casts on dispatch. `send` is the store's
 *  own `(msg: Up) => void` — the scaffold agent extended `Up` with
 *  `approve`/`import_running`/`detach_cell` so cards use the real type. */
export type CardProps<P = Record<string, unknown>> = {
  payload: P;
  displayId: string;
  send: (msg: Up) => void;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyCard = ComponentType<CardProps<any>>;

export const cards: Record<string, AnyCard> = {
  prompt: PromptCard,
  run_proposal: RunProposalCard,
  audit_run: RunCard,
  eval_run: RunCard,
  scan: ScanCard,
  excerpt: ExcerptCard,
  transcript: TranscriptCard,
};
