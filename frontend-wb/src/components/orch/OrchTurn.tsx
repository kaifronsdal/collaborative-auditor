/**
 * `<OrchTurn>` — one orchestrator notebook turn (M1-NOTEBOOK.md §OrchTurn.tsx).
 *
 * Layout: assistant prose (where `wb.report` content went) → the `python`
 * code cell → outputs in emission order → traceback if the cell raised.
 */
import { useMemo, useState, type JSX } from "react";
import { marked } from "marked";

import type { ChatMessage, ToolCallError } from "@tsmono/inspect-common";

import { useSession } from "../../store/session";
import { Output } from "./Output";
import type { OrchTurnData } from "./types";

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
  if (!md && !pending) return null;
  return (
    <div className="asst-prose">
      {html && <div className="md" dangerouslySetInnerHTML={{ __html: html }} />}
      {pending && <span className="cursor" />}
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
  background,
}: {
  turn: number;
  code: string;
  running: boolean;
  detached: boolean;
  background: boolean;
}): JSX.Element {
  const send = useSession((s) => s.send);
  const loc = useMemo(() => code.split("\n").length, [code]);
  const [collapsed, setCollapsed] = useState(loc > COLLAPSE_LOC);
  const cls =
    "code-cell" +
    (collapsed ? " collapsed" : "") +
    (detached || background ? " bg" : "");
  // `→ bg` is only offered on the *live* blocking cell: `running` (ToolEvent
  // still `pending`) implies this is the latest turn — the agent loop can't
  // advance past an unfinished tool call. Already-detached/background cells
  // don't need it.
  const canDetach = running && !detached && !background;
  return (
    <div className={cls}>
      <div className="cc-head">
        <span className="lang">python</span>
        {running && <i className="bi bi-record-fill fx-dot pending" />}
        {(detached || background) && <span className="cc-bg-chip">bg</span>}
        {canDetach && (
          <button
            type="button"
            className="cc-bg-btn"
            title="Detach: keep running in background, unblock the orchestrator"
            onClick={() => send({ t: "detach_cell" })}
          >
            <i className="bi bi-arrow-right-short" /> bg
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
          onClick={() => setCollapsed((v) => !v)}
        >
          {collapsed ? `show ${loc} lines` : "collapse"}
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
        <span className="out-kind">traceback</span>
        <span className="out-meta">{err.type}</span>
      </div>
      <pre className="out-plain err">{err.message}</pre>
    </div>
  );
}
