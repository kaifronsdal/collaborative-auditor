/**
 * `<OrchTurn>` — one orchestrator notebook turn (M1-NOTEBOOK.md §OrchTurn.tsx).
 *
 * Layout: assistant prose (where `wb.report` content went) → the `python`
 * code cell → outputs in emission order → traceback if the cell raised.
 */
import { useEffect, useMemo, useRef, useState, type JSX } from "react";
import { marked } from "marked";

import type { ChatMessage, ToolCallError } from "@tsmono/inspect-common";

import { useSession } from "../../store/session";
import { Output } from "./Output";
import { WB_MIME, type OrchTurnData } from "./types";

type Props = {
  data: OrchTurnData;
  /** Turn ids of cells still running detached — draws the `bg` accent. */
  bgCells: readonly number[];
};

export function OrchTurn({ data, bgCells }: Props): JSX.Element {
  const { turn, model, userInput, py, outputs } = data;
  const asst = model.output?.choices?.[0]?.message;
  const code = typeof py?.arguments.code === "string" ? py.arguments.code : "";
  const running = py?.pending === true;
  const detached = bgCells.includes(turn);
  // A cell "errored" as soon as the kernel emits its `{kind:"traceback"}`
  // display card — that lands before `ToolEvent.error` settles, so the
  // `.cc-head` chip shows even before the traceback body renders (§18).
  const errored =
    py?.error != null ||
    outputs.some((o) => o.data.bundle[WB_MIME]?.kind === "traceback");

  return (
    <div className="turn" data-turn={turn}>
      {userInput.map((m) => (
        <UserAsk key={m.id ?? `${turn}-u`} msg={m} />
      ))}
      <AssistantProse content={asst?.content ?? ""} pending={!!model.pending} />
      {py && (
        <CodeCell
          turn={turn}
          code={code}
          running={running}
          detached={detached}
          errored={errored}
          background={py.arguments.background === true}
        />
      )}
      {outputs.map((ev) => (
        <Output
          key={ev.uuid ?? ev.data.id}
          id={ev.data.id}
          bundle={ev.data.bundle}
          meta={ev.data.meta}
          stable={ev.data.stable}
        />
      ))}
      {py?.error && <Traceback err={py.error} />}
    </div>
  );
}

// ── assistant prose ─────────────────────────────────────────────────────────

/** `[a-3f2c]`, `a-3f2c·t5` → clickable `.qref` chip. Operates on the rendered
 *  HTML (post-markdown) so ids inside code spans are left alone by `marked`'s
 *  own escaping and we only match text nodes. */
const REF_RE = /\[?(a-[0-9a-f]{4,})(?:[·.]t(\d+))?\]?/g;

function linkifyRefs(html: string): string {
  return html.replace(REF_RE, (_m, id: string, t?: string) => {
    const href = t ? `wb://audit/${id}#t${t}` : `wb://audit/${id}`;
    const label = t ? `${id}·t${t}` : id;
    return `<a class="qref" href="${href}">${label}</a>`;
  });
}

function contentText(content: ChatMessage["content"]): string {
  return typeof content === "string"
    ? content
    : content
        .map((c) => (c.type === "text" ? c.text : c.type === "reasoning" ? "" : ""))
        .join("");
}

/** Prose is soft-capped at ~12 lines with a `show more` fade (§19). Still
 *  streaming (`pending`) prose is never capped so the tail follows. */
function AssistantProse({
  content,
  pending,
}: {
  content: ChatMessage["content"];
  pending: boolean;
}): JSX.Element | null {
  const md = contentText(content);
  const html = useMemo(
    () => (md ? linkifyRefs(marked.parse(md, { async: false })) : ""),
    [md]
  );
  // Rough line estimate — the CSS agent will pin the actual `max-height`;
  // this only decides whether to render the toggle.
  const long = md.length > 800 || md.split("\n").length > 14;
  const [capped, setCapped] = useState(true);
  if (!md && !pending) return null;
  const cap = long && capped && !pending;
  return (
    <div className={`asst-prose${cap ? " prose-capped" : ""}`}>
      {html && <div className="md" dangerouslySetInnerHTML={{ __html: html }} />}
      {pending && <span className="cursor" />}
      {long && !pending && (
        <button
          type="button"
          className="prose-more"
          onClick={() => setCapped((v) => !v)}
        >
          {capped ? (
            <>
              show more <i className="bi bi-chevron-down" />
            </>
          ) : (
            <>
              show less <i className="bi bi-chevron-up" />
            </>
          )}
        </button>
      )}
    </div>
  );
}

function UserAsk({ msg }: { msg: ChatMessage }): JSX.Element {
  return (
    <div className="ask-wrap">
      <div className="ask-by">you</div>
      <div className="ask-bubble">{contentText(msg.content)}</div>
    </div>
  );
}

// ── code cell ───────────────────────────────────────────────────────────────

const COLLAPSE_LOC = 6;

function CodeCell({
  turn,
  code,
  running,
  detached,
  errored,
  background,
}: {
  turn: number;
  code: string;
  running: boolean;
  detached: boolean;
  errored: boolean;
  background: boolean;
}): JSX.Element {
  const send = useSession((s) => s.send);
  const loc = useMemo(() => code.split("\n").length, [code]);
  // Collapse defaults to true once the cell first crosses `COLLAPSE_LOC`.
  // Streaming code arrives short then grows, so re-evaluate on the crossing;
  // after the user has toggled, leave their choice alone.
  const [collapsed, setCollapsed] = useState(loc > COLLAPSE_LOC);
  const touched = useRef(false);
  useEffect(() => {
    if (!touched.current) setCollapsed(loc > COLLAPSE_LOC);
  }, [loc > COLLAPSE_LOC]); // eslint-disable-line react-hooks/exhaustive-deps

  const cls =
    "code-cell" +
    (collapsed ? " collapsed" : "") +
    (detached || background ? " bg" : "");
  // `run in background` is only offered on the *live* blocking cell:
  // `running` (ToolEvent still `pending`) implies this is the latest turn —
  // the agent loop can't advance past an unfinished tool call.
  // Already-detached/background cells don't need it.
  const canDetach = running && !detached && !background;
  return (
    <div className={cls}>
      <div className="cc-head">
        <span className="lang">python</span>
        {running && <i className="bi bi-record-fill fx-dot pending" />}
        {errored && !running && (
          <span className="cell-status err" title="cell raised">
            <i className="bi bi-exclamation-triangle-fill" /> error
          </span>
        )}
        {(detached || background) && (
          <span
            className="cc-bg-chip"
            title="running in background; result will post here"
          >
            bg
          </span>
        )}
        {canDetach && (
          <button
            type="button"
            className="cc-bg-btn"
            title="Detach: keep running in background, unblock the orchestrator"
            onClick={() => send({ t: "detach_cell" })}
          >
            <i className="bi bi-layer-backward" /> run in background
          </button>
        )}
        <span className="turn-no">turn {turn}</span>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
      {loc > COLLAPSE_LOC && (
        <button
          type="button"
          className="cc-more"
          onClick={() => {
            touched.current = true;
            setCollapsed((v) => !v);
          }}
        >
          <i className={`bi bi-chevron-${collapsed ? "down" : "up"}`} />{" "}
          {collapsed ? `show ${loc - COLLAPSE_LOC} more lines` : "collapse"}
        </button>
      )}
    </div>
  );
}

function Traceback({ err }: { err: ToolCallError }): JSX.Element {
  return (
    <div className="out traceback">
      <div className="out-head">
        <i className="bi bi-exclamation-triangle" />
        <span className="out-meta">{err.type}</span>
      </div>
      <pre className="out-plain err">{err.message}</pre>
    </div>
  );
}
