import type { ChatMessageUser } from "@tsmono/inspect-common";

import { type JSX, useState } from "react";

import { useSession } from "../store/session";
import type { Status } from "../lib/wire";
import { Column } from "./Column";

function uuid(): string {
  return crypto.randomUUID();
}

function runlineText(status: Status | null): string {
  switch (status) {
    case "running":
      return "audit running — auditor and target generating";
    case "paused":
      return "audit paused — release a turn to continue";
    case "ended":
      return "audit ended — conversation complete";
    default:
      return "audit idle";
  }
}

/**
 * The running-audit desk: two transcript columns + a thin seed header + the
 * runline/composer at the bottom.  Extracted from App.tsx so App can switch
 * cleanly between <StartView/> and <DeskView/>.
 */
export function DeskView(): JSX.Element {
  const send = useSession((s) => s.send);
  const current = useSession((s) => s.current);
  const status = useSession((s) => s.status);
  const branchConfig = useSession((s) =>
    s.current ? s.branchConfig[s.current] : undefined
  );

  const [feedback, setFeedback] = useState("");

  // `current` is guaranteed non-null when DeskView is rendered (App checks).
  const branch = current!;

  const seedTitle = branchConfig?.seed ?? "";

  return (
    <>
      {/* thin seed/context strip above the columns */}
      {seedTitle && (
        <div className="desk-header" title={seedTitle}>
          <span className="desk-seed">{seedTitle}</span>
        </div>
      )}

      <div className="columns">
        <Column branch={branch} role="auditor" />
        <Column branch={branch} role="target" />
      </div>

      <div className="composer-zone">
        <div className={`runline status-${status ?? "idle"}`}>
          <span className="star">✻</span>
          <span className="runtext">{runlineText(status)}</span>
          <span className="ctl">
            <button
              className={status === "running" ? "" : "live"}
              onClick={() => send({ t: "play" })}
              disabled={status === "ended" || status === "running"}
            >
              {status === "paused" ? "resume" : "play"}
            </button>
            <button onClick={() => send({ t: "pause" })} disabled={status !== "running"}>
              pause
            </button>
          </span>
        </div>

        <div className="composer">
          <textarea
            className="composer-input"
            rows={2}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="Feedback for the auditor — read at the next turn boundary…"
          />
          <div className="composer-lower">
            <span className="dest">→ auditor</span>
            <button
              className="send"
              disabled={!feedback.trim()}
              onClick={() => {
                const message: ChatMessageUser = {
                  id: uuid(),
                  role: "user",
                  content: feedback,
                };
                send({ t: "inject", branch, role: "auditor", message });
                setFeedback("");
              }}
            >
              ↑
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
