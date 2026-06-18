import type { ModelEvent, ModelOutput } from "@tsmono/inspect-common";

import { useState, type JSX } from "react";

import { useSession } from "../store/session";
import { Bubble, renderContent } from "./Bubble";

type Props = {
  ev: ModelEvent;
  /** `input.length` of the previous same-role ModelEvent — the tail before it
   *  is what this turn newly added (STREAMING.md §D). */
  prevInputLen: number;
  /** If true, show the auditor-column lighter action set (raw · copy only). */
  auditor?: boolean;
};

export function ModelEventRow({ ev, prevInputLen, auditor }: Props): JSX.Element {
  const send = useSession((s) => s.send);
  const tail = ev.input.slice(prevInputLen);
  const choice = ev.output.choices[0];
  const content = choice?.message.content;
  const toolCalls = choice?.message.tool_calls ?? [];
  const anchorId = choice?.message.id;

  const [showRaw, setShowRaw] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");

  function handleBranch() {
    if (anchorId == null) return;
    send({ t: "branch", at: anchorId });
  }

  function handleResample() {
    if (anchorId == null) return;
    send({ t: "resample", at: anchorId });
  }

  function handleEditOpen() {
    // Pre-fill with the assistant message's text content.
    const text =
      typeof content === "string"
        ? content
        : Array.isArray(content)
          ? content
              .map((c) => ("text" in c ? c.text : ""))
              .join("")
          : "";
    setEditText(text);
    setEditing(true);
  }

  function handleEditSave() {
    if (anchorId == null) return;
    // Build a minimal ModelOutput carrying the edited text. We keep all the
    // original output fields but replace the message content and let the
    // backend assign a fresh message id (footgun #3: fresh id so downstream
    // anchor refs are never stale).
    const edited: ModelOutput = {
      ...ev.output,
      choices: ev.output.choices.map((c, i) =>
        i === 0
          ? {
              ...c,
              message: {
                ...c.message,
                content: editText,
                completion: editText,
                // omit id — backend will assign a fresh one
                id: undefined,
              },
            }
          : c
      ),
    };
    send({ t: "edit", at: anchorId, output: edited });
    setEditing(false);
  }

  function handleEditCancel() {
    setEditing(false);
  }

  return (
    <div className="model-event-row">
      {tail.map((m, i) => (
        <Bubble key={m.id ?? i} msg={m} byline={m.role} />
      ))}

      <div className="bubble-wrap">
        {editing ? (
          <div className="bubble assistant editing">
            <textarea
              className="edit-textarea"
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              rows={6}
              autoFocus
            />
            <div className="edit-actions">
              <button className="edit-save" onClick={handleEditSave}>save</button>
              <button className="edit-cancel" onClick={handleEditCancel}>cancel</button>
            </div>
          </div>
        ) : (
          <div className="bubble assistant">
            {content != null && renderContent(content)}
            {toolCalls.map((tc) => (
              <span key={tc.id} className="tool-call-inline">
                → {tc.function}
              </span>
            ))}
            {ev.pending && <span className="cursor" />}
          </div>
        )}
        <div className="bubble-by">assistant</div>
      </div>

      {/* caption action row — always visible at 0.5 opacity, brightens on row hover */}
      <div className="actions">
        {auditor ? (
          <>
            <button onClick={() => setShowRaw((v) => !v)} title="toggle raw JSON">raw</button>
            <button
              onClick={() => navigator.clipboard.writeText(
                typeof content === "string" ? content : JSON.stringify(content)
              )}
              title="copy text"
            >
              copy
            </button>
          </>
        ) : (
          <>
            <button
              onClick={handleBranch}
              disabled={anchorId == null || !!ev.pending}
              title="branch at this turn"
            >
              branch
            </button>
            <button
              onClick={handleResample}
              disabled={anchorId == null || !!ev.pending}
              title="resample from this turn"
            >
              resample
            </button>
            <button
              onClick={handleEditOpen}
              disabled={anchorId == null || !!ev.pending || editing}
              title="edit this response"
            >
              edit
            </button>
            <button onClick={() => setShowRaw((v) => !v)} title="toggle raw JSON">raw</button>
          </>
        )}
      </div>

      {showRaw && (
        <pre className="raw-json">{JSON.stringify(ev, null, 2)}</pre>
      )}
    </div>
  );
}
