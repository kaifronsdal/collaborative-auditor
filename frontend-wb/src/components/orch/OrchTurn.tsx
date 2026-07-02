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
import {
  STREAM_MIME,
  WB_MIME,
  type DisplayData,
  type DisplayInfoEvent,
  type OrchTurnData,
} from "./types";

type Props = {
  data: OrchTurnData;
  /** Turn ids of cells still running detached — draws the `bg` accent. */
  bgCells: readonly number[];
  /** Binding name once this turn's detached cell has settled — the column
   *  parses `orch.notifications` for `cell-{N} … bound: <name>` and threads
   *  the map down so the origin cell's `.cc-bg-chip` reads `done · <name>`. */
  settledBg?: string;
};

export function OrchTurn({ data, bgCells, settledBg }: Props): JSX.Element {
  const { turn, model, userInput, py, outputs } = data;
  const asst = model.output?.choices?.[0]?.message;
  const code = typeof py?.arguments.code === "string" ? py.arguments.code : "";
  const running = py?.pending === true;
  const detached = bgCells.includes(turn);
  // A cell "errored" as soon as the kernel emits its `{kind:"traceback"}`
  // display card — that lands before `ToolEvent.error` settles, so the
  // `.cc-head` chip shows even before the traceback body renders (§18).
  // The display card is the canonical render; `<Traceback err={py.error}>`
  // below is only a fallback for cells that errored without emitting one.
  const hasTbCard = outputs.some(
    (o) => o.data.bundle[WB_MIME]?.kind === "traceback"
  );
  const errored = py?.error != null || hasTbCard;

  // Dedupe (§16) then coalesce adjacent stdout/stderr chunks (UI-AUDIT §C).
  // Coalescing builds fresh event objects (never mutate store state); memoise
  // on `outputs` so `<Output>`'s bundle-ref update counter doesn't tick on
  // unrelated parent re-renders.
  const displays = useMemo(() => {
    // A handle that's `display()`ed with a stable id inside the cell AND
    // returned as the last expression mounts twice — drop any non-stable
    // output whose wb `payload.id` matches a stable one in the same turn.
    const stableWbIds = new Set(
      outputs
        .filter((o) => o.data.stable)
        .map((o) => o.data.bundle[WB_MIME]?.id)
        .filter((v): v is string => typeof v === "string")
    );
    const deduped = outputs.filter(
      (o) =>
        o.data.stable ||
        !stableWbIds.has(o.data.bundle[WB_MIME]?.id as string | undefined ?? "")
    );
    // Fold adjacent stream events of the same channel into one — the kernel
    // flushes stdout in small chunks, which otherwise render as N grey rails.
    const out: DisplayInfoEvent[] = [];
    for (const ev of deduped) {
      const s = ev.data.bundle[STREAM_MIME];
      const prev: DisplayInfoEvent | undefined = out[out.length - 1];
      const ps = prev?.data.bundle[STREAM_MIME];
      if (s && prev && ps && s.name === ps.name) {
        // `InfoEvent.data` is generated as `JsonValue`, so the intersection
        // `JsonValue & DisplayData` won't spread or accept an object literal
        // without a cast — a generated-schema quirk, not a real type hole.
        const pd = prev.data;
        const data: DisplayData = {
          id: pd.id,
          turn: pd.turn,
          meta: pd.meta,
          stable: pd.stable,
          bundle: { [STREAM_MIME]: { name: ps.name, text: ps.text + s.text } },
        };
        out[out.length - 1] = { ...prev, data } as DisplayInfoEvent;
      } else {
        out.push(ev);
      }
    }
    return out;
  }, [outputs]);

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
          settledBg={settledBg}
        />
      )}
      {displays.map((ev) => (
        <Output
          key={ev.uuid ?? ev.data.id}
          id={ev.data.id}
          bundle={ev.data.bundle}
          meta={ev.data.meta}
          stable={ev.data.stable}
          settled={!running && !detached}
        />
      ))}
      {py?.error && !hasTbCard && <Traceback err={py.error} />}
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
  const body = pending ? html + '<span class="cursor"></span>' : html;
  return (
    <div className="asst-prose md" dangerouslySetInnerHTML={{ __html: body }} />
  );
}

function UserAsk({ msg }: { msg: ChatMessage }): JSX.Element {
  return (
    <div className="ask-wrap">
      <div className="ask-bubble">{contentText(msg.content)}</div>
    </div>
  );
}

// ── code cell ───────────────────────────────────────────────────────────────

function firstNonBlankLine(code: string): string {
  for (const ln of code.split("\n")) {
    const t = ln.trim();
    if (t) return t;
  }
  return "";
}

function CodeCell({
  turn,
  code,
  running,
  detached,
  errored,
  background,
  settledBg,
}: {
  turn: number;
  code: string;
  running: boolean;
  detached: boolean;
  errored: boolean;
  background: boolean;
  settledBg: string | undefined;
}): JSX.Element {
  const send = useSession((s) => s.send);
  // UI-AUDIT §C: collapsed-first. A settled, non-erroring cell is noise —
  // show a one-line gist. Running/errored cells are forced open (you need to
  // see what's executing / what blew up). A user's manual expand sticks: once
  // `userToggled`, later transitions (running → settled) leave it alone.
  const forceOpen = running || errored;
  const [collapsed, setCollapsed] = useState(!forceOpen);
  const userToggled = useRef(false);
  useEffect(() => {
    if (!userToggled.current) setCollapsed(!forceOpen);
  }, [forceOpen]);
  const open = forceOpen || !collapsed;

  const cls =
    "code-cell" +
    (open ? "" : " collapsed") +
    (detached || background ? " bg" : "");
  // `run in background` is only offered on the *live* blocking cell:
  // `running` (ToolEvent still `pending`) implies this is the latest turn —
  // the agent loop can't advance past an unfinished tool call.
  // Already-detached/background cells don't need it.
  const canDetach = running && !detached && !background;
  const showBgChip = detached || background || settledBg != null;
  // When expanded the body already shows line 1, so the head gist would just
  // duplicate it — swap for a faint `python · N lines` label instead.
  const loc = code.trimEnd().split("\n").length;
  const gist = open
    ? `python · ${loc} line${loc === 1 ? "" : "s"}`
    : firstNonBlankLine(code);
  const toggle = (): void => {
    if (forceOpen) return;
    userToggled.current = true;
    setCollapsed((v) => !v);
  };
  return (
    <div className={cls}>
      <div
        className="cc-head"
        {...(!forceOpen && { role: "button", tabIndex: 0, onClick: toggle })}
      >
        <i className={`bi bi-chevron-${open ? "down" : "right"} cc-chev`} />
        {open ? (
          <span className="cc-gist cc-lang">{gist}</span>
        ) : (
          <code className="cc-gist">{gist}</code>
        )}
        {errored && !running && (
          <span className="cell-status err" title="cell raised">
            <i className="bi bi-exclamation-triangle-fill" /> error
          </span>
        )}
        {showBgChip && (
          <span
            className="cc-bg-chip"
            title={
              settledBg
                ? `background cell settled; result bound to \`${settledBg}\``
                : "running in background; result will post here"
            }
          >
            {settledBg ? `done · ${settledBg}` : "bg"}
          </span>
        )}
        {canDetach && (
          <button
            type="button"
            className="cc-bg-btn"
            title="Detach: keep running in background, unblock the orchestrator"
            onClick={(e) => {
              e.stopPropagation();
              send({ t: "detach_cell" });
            }}
          >
            <i className="bi bi-layer-backward" /> run in background
          </button>
        )}
        <span className="turn-no">turn {turn}</span>
      </div>
      {open && (
        <pre>
          <code>{code}</code>
        </pre>
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
