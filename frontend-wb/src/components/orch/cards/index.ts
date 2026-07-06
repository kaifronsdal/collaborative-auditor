/**
 * Registry keyed on `bundle["application/vnd.workbench.v1+json"].kind`
 * (UI-AUDIT.md §D). Eight kinds → three shell families + a variant tag; the
 * component reads `payload.kind` internally to pick body/verdict behaviour,
 * so `variant` here is documentation for the caller.
 *
 * `<Output>` currently consumes the flat `cards` map (`kind → Component`);
 * `CARDS` is the §D shape (`kind → {C, variant}`) for when Output migrates.
 */
import type { ComponentType } from "react";

import type { Up } from "../../../lib/wire";
import FindingCard from "./FindingCard";
import GateCard from "./GateCard";
import ProgressCard from "./ProgressCard";
import ReaderCard from "./ReaderCard";

export type { FindingPayload } from "./FindingCard";
export type {
  CiteProposalPayload,
  GatePayload,
  PromptPayload,
  RunProposalPayload,
} from "./GateCard";
export type { ProgressPayload, ScanPayload } from "./ProgressCard";
export type { ExcerptPayload, ReaderPayload, TranscriptPayload } from "./ReaderCard";

import type { WbPayload } from "../types";

/** Shared prop contract — `payload` is narrowed per-card, so the registry
 *  types it loosely and the caller casts on dispatch. `send` is the store's
 *  own `(msg: Up) => void`. */
export type CardProps<P = WbPayload> = {
  payload: P;
  displayId: string;
  send: (msg: Up) => void;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyCard = ComponentType<CardProps<any>>;

export const CARDS: Record<string, { C: AnyCard; variant?: string }> = {
  prompt: { C: GateCard, variant: "prompt" },
  run_proposal: { C: GateCard, variant: "run_proposal" },
  cite_proposal: { C: GateCard, variant: "cite_proposal" },
  eval_run: { C: ProgressCard, variant: "run" },
  scan: { C: ProgressCard, variant: "scan" },
  excerpt: { C: ReaderCard, variant: "excerpt" },
  transcript: { C: ReaderCard, variant: "transcript" },
  finding: { C: FindingCard },
};

/** Flat back-compat map for `Output.tsx` (owned by the turn/output agent). */
export const cards: Record<string, AnyCard> = Object.fromEntries(
  Object.entries(CARDS).map(([k, { C }]) => [k, C])
);
