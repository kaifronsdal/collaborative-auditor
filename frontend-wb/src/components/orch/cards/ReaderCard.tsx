/**
 * `ReaderCard` — the shared dense-transcript shell (UI-AUDIT.md §D). A flat
 * `.rd-msg` list (gutter-left role tag + `renderContent(msg)` body — *not*
 * M0's rounded `<Bubble>`), with a minimal header and an `open in desk →`
 * footer that imports the sample.
 *
 * Variants (dispatched on `payload.kind`):
 * - `excerpt`    — a `±N` window around turn `at`. The anchor message
 *                  (`i === at_idx`) gets `.rd-anchor`; per-message turn
 *                  numbers are `at - at_idx + i`.
 * - `transcript` — a pointer into a `.eval` sample. Backend now ships the
 *                  *tail* 3 messages as `preview` (elicited behaviour, not
 *                  system-prompt boilerplate); falls back to a stub row when
 *                  empty. `.iv-url` in the footer shows basename only.
 */
import type { ChatMessage } from "@tsmono/inspect-common";
import { basename } from "@tsmono/util";

import { useState, type JSX } from "react";
import { Modal } from "@tsmono/react/components/Modal";

import { renderContent } from "../../Bubble";
import type { Up } from "../../../lib/wire";
import type { ExcerptPayload, TranscriptPayload } from "../types";

export type { ExcerptPayload, TranscriptPayload };
export type ReaderPayload = ExcerptPayload | TranscriptPayload;

type Props = {
  payload: ReaderPayload;
  displayId: string;
  send: (msg: Up) => void;
};

/** Short role tag for the gutter (UI-AUDIT §C). */
const roleTag = (r: ChatMessage["role"]): string =>
  r === "assistant" ? "asst" : r === "system" ? "sys" : r;

// -- shared shell -------------------------------------------------------------

export default function ReaderCard({ payload, displayId, send }: Props): JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const openInDesk = (): void =>
    send({ t: "import", path: payload.log, sample_id: payload.sample_id });

  const isExcerpt = payload.kind === "excerpt";
  const msgs = isExcerpt ? payload.messages : (payload.preview ?? []);
  const nMsgs = isExcerpt ? payload.messages.length : payload.n_messages;

  const msgList = (large: boolean): JSX.Element => (
    <div className={`rd-body${large ? " rd-body-lg" : ""}`}>
      {msgs.length > 0 ? (
        msgs.map((m, i) => {
          const anchor = isExcerpt && i === payload.at_idx;
          // Excerpt turn number: window starts at `at - at_idx` (UI-AUDIT §A
          // bugfix — was `at - floor(len/2)` which broke on asymmetric
          // windows near the transcript head).
          const turn = isExcerpt ? payload.at - payload.at_idx + i : undefined;
          return (
            <div key={m.id ?? i} className={`rd-msg${anchor ? " rd-anchor" : ""}`}>
              <span className="rd-role">
                {roleTag(m.role)}
                {turn != null && <span className="rd-turn"> · t{turn}</span>}
              </span>
              {renderContent(m.content)}
            </div>
          );
        })
      ) : (
        <div className="rd-msg rd-empty">
          <span className="rd-role">log</span>
          no preview — open in auditor to read
        </div>
      )}
    </div>
  );

  return (
    <div className="out reader" data-display-id={displayId}>
      <div className="out-head hstack g8">
        <i className={`bi ${isExcerpt ? "bi-quote" : "bi-file-text"}`} />
        <span className="out-meta">
          {payload.sample_id}
          {payload.at != null && ` · t${payload.at}`}
          {" · "}
          {nMsgs} msgs
        </span>
      </div>

      {msgList(false)}

      <div className="rd-foot hstack g8">
        <span className="iv-url truncate" title={payload.log}>
          {basename(payload.log)}
        </span>
        {msgs.length > 0 && (
          <a onClick={() => setExpanded(true)}>
            expand <i className="bi bi-arrows-angle-expand" />
          </a>
        )}
        <a onClick={openInDesk}>
          open in auditor <i className="bi bi-arrow-right" />
        </a>
      </div>

      <Modal
        show={expanded}
        onHide={() => setExpanded(false)}
        title={`${payload.sample_id}${payload.at != null ? ` · turn ${payload.at}` : ""}`}
        width="min(820px, 92vw)"
        className="wb-modal"
        padded={false}
      >
        {msgList(true)}
      </Modal>
    </div>
  );
}
