/**
 * Registry keyed on `bundle["application/vnd.workbench.v1+json"].kind`
 * (UI-AUDIT.md §D). `<Output>` looks up `cards[kind]`; the component reads
 * `payload.kind` internally to pick body/verdict behaviour.
 */
import type { ComponentType } from "react";

import type { Up } from "../../../lib/wire";
import type { WbPayload } from "../types";
import FindingCard from "./FindingCard";
import GateCard from "./GateCard";
import ProgressCard from "./ProgressCard";
import ReaderCard from "./ReaderCard";

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

export const cards: Record<string, AnyCard> = {
  prompt: GateCard,
  run_proposal: GateCard,
  cite_proposal: GateCard,
  eval_run: ProgressCard,
  scan: ProgressCard,
  excerpt: ReaderCard,
  transcript: ReaderCard,
  finding: FindingCard,
};
