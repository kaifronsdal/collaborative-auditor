/**
 * `<OrchTurn>` — one orchestrator notebook turn (M1-NOTEBOOK.md §OrchTurn.tsx).
 *
 * Layout: assistant prose (where `wb.report` content went) → tool cell(s) →
 * outputs in emission order → traceback if the `python` cell raised.
 *
 * M1-HYBRID.md widened the tool surface from just `python` to eight tools; a
 * turn can carry several `ToolEvent`s. `<ToolCell>` dispatches on
 * `ev.function`: `python` → the existing collapsible `.code-cell`; `bash` →
 * a sibling `.bash-cell` (`$ cmd` head, stdout body); `read_file`/
 * `write_file`/`edit_file` → a one-line `.file-receipt`; the three review
 * tools → a one-line `.tool-receipt` (the actual UI is the `GateCard`
 * DisplayEvent that `kernel.gate` emits below).
 */
import { useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { marked } from "marked";

import type { ChatMessage, ToolCallError, ToolEvent } from "@tsmono/inspect-common";

import { useSession } from "../../store/session";
import { contentText, resultText } from "../tool-renderers/util";
import { BlockActions, CopyBtn } from "./BlockActions";
import { Output } from "./Output";
import {
  STREAM_MIME,
  WB_MIME,
  type DisplayData,
  type DisplayInfoEvent,
  type OrchTurnData,
} from "./types";

/** §7: `{name: "TypeName · repr…"}` snapshot of `user_ns` shipped in the
 *  `cell_done` output; used to decorate identifiers in the collapsed gist. */
export type NsSummary = Readonly<Record<string, string>>;

type Props = {
  data: OrchTurnData;
  /** Turn ids of cells still running detached — draws the `bg` accent. */
  bgCells: readonly number[];
  /** Binding name once this turn's detached cell has settled — the column
   *  parses `orch.notifications` for `cell-{N} … bound: <name>` and threads
   *  the map down so the origin cell's `.cc-bg-chip` reads `done · <name>`. */
  settledBg?: string;
  /** Accumulated `ns_summary` across all settled cells — every name currently
   *  bound in the kernel. Collapsed `.cc-gist` wraps matching tokens with a
   *  native-tooltip span. */
  ns: NsSummary;
};

export function OrchTurn({ data, bgCells, settledBg, ns }: Props): JSX.Element {
  const { turn, model, userInput, tools, outputs } = data;
  const asst = model.output?.choices?.[0]?.message;
  const py = tools.find((t) => t.function === "python");
  const anyRunning = tools.some((t) => t.pending);
  const detached = bgCells.includes(turn);
  // A cell "errored" as soon as the kernel emits its `{kind:"traceback"}`
  // display card — that lands before `ToolEvent.error` settles, so the
  // `.cc-head` chip shows even before the traceback body renders (§18).
  // The display card is the canonical render; `<Traceback err={py.error}>`
  // below is only a fallback for cells that errored without emitting one.
  const hasTbCard = outputs.some(
    (o) => o.data.bundle[WB_MIME]?.kind === "traceback"
  );
  // §5: `kernel._settle` emits a `{kind:"cell_done", turn, duration, ns, …}`
  // display once the cell finishes. It's metadata, not a visible output —
  // pull `duration` for the `.cc-head` chip and drop it from `displays`.
  const cellDone = outputs.find(
    (o) => o.data.bundle[WB_MIME]?.kind === "cell_done"
  )?.data.bundle[WB_MIME];

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
        o.data.bundle[WB_MIME]?.kind !== "cell_done" &&
        (o.data.stable ||
          !stableWbIds.has(o.data.bundle[WB_MIME]?.id as string | undefined ?? ""))
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
      <AssistantProse
        content={asst?.content ?? ""}
        pending={!!model.pending}
        turn={turn}
      />
      {tools.map((ev, i) => (
        <ToolCell
          key={ev.uuid ?? ev.id ?? i}
          ev={ev}
          turn={turn}
          detached={detached}
          settledBg={settledBg}
          ns={ns}
          hasTbCard={hasTbCard}
          cellDone={cellDone}
        />
      ))}
      {displays.map((ev) => (
        <Output
          key={ev.uuid ?? ev.data.id}
          id={ev.data.id}
          bundle={ev.data.bundle}
          meta={ev.data.meta}
          stable={ev.data.stable}
          settled={!anyRunning && !detached}
        />
      ))}
      {py?.error && !hasTbCard && <Traceback err={py.error} />}
    </div>
  );
}

// ── tool-cell dispatch ──────────────────────────────────────────────────────

const FILE_TOOLS: ReadonlySet<string> = new Set([
  "read_file",
  "write_file",
  "edit_file",
]);
const REVIEW_TOOLS: ReadonlySet<string> = new Set([
  "ask_human",
  "review_seeds",
  "review_finding",
]);

/** Dispatch one `ToolEvent` to its renderer. `python`-only chrome
 *  (`cell_done` duration/interrupt, kernel detach, `ns` tooltips) is
 *  threaded through so `CodeCell` stays byte-identical to the pre-hybrid
 *  render; every other tool derives its own state from the event. */
function ToolCell({
  ev,
  turn,
  detached,
  settledBg,
  ns,
  hasTbCard,
  cellDone,
}: {
  ev: ToolEvent;
  turn: number;
  detached: boolean;
  settledBg: string | undefined;
  ns: NsSummary;
  hasTbCard: boolean;
  cellDone: Record<string, unknown> | undefined;
}): JSX.Element {
  const fn = ev.function;
  if (fn === "python") {
    const code = typeof ev.arguments.code === "string" ? ev.arguments.code : "";
    return (
      <CodeCell
        turn={turn}
        code={code}
        running={ev.pending === true}
        detached={detached}
        errored={ev.error != null || hasTbCard}
        interrupted={cellDone?.interrupted === true}
        background={ev.arguments.background === true}
        settledBg={settledBg}
        duration={
          typeof cellDone?.duration === "number" ? cellDone.duration : undefined
        }
        ns={ns}
      />
    );
  }
  if (fn === "bash") return <BashCell ev={ev} turn={turn} />;
  if (FILE_TOOLS.has(fn)) return <FileReceipt ev={ev} turn={turn} />;
  if (REVIEW_TOOLS.has(fn)) return <ReviewReceipt ev={ev} turn={turn} />;
  // Unknown tool — render as a bash-shaped cell so args/result stay visible.
  return <BashCell ev={ev} turn={turn} />;
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

function AssistantProse({
  content,
  pending,
  turn,
}: {
  content: ChatMessage["content"];
  pending: boolean;
  turn: number;
}): JSX.Element | null {
  const send = useSession((s) => s.send);
  const md = contentText(content);
  const html = useMemo(
    () => (md ? linkifyRefs(marked.parse(md, { async: false })) : ""),
    [md]
  );
  if (!md && !pending) return null;
  const body = pending ? html + '<span class="cursor"></span>' : html;
  // §6: prose is `dangerouslySetInnerHTML`, so the icon row can't be a child of
  // it — wrap both in a `.ba-host` so `:hover` reveals the row over the prose.
  return (
    <div className="ba-host">
      <div className="asst-prose md" dangerouslySetInnerHTML={{ __html: body }} />
      {!pending && (
        <BlockActions>
          <CopyBtn text={md} title="copy markdown" />
          <button
            type="button"
            title={`rewind to before turn ${turn}`}
            onClick={() => {
              if (
                confirm(
                  `Restart from turn ${turn}? Later turns will be discarded. ` +
                    `Kernel bindings are kept.`
                )
              ) {
                send({ t: "rewind", turn });
              }
            }}
          >
            <i className="bi bi-arrow-counterclockwise" />
          </button>
        </BlockActions>
      )}
    </div>
  );
}

function UserAsk({ msg }: { msg: ChatMessage }): JSX.Element {
  return (
    <div className="ask-wrap">
      <div className="ask-bubble">{contentText(msg.content)}</div>
    </div>
  );
}

// ── cell shell (shared by CodeCell / BashCell) ──────────────────────────────

/** UI-AUDIT §C: collapsed-first. A settled, non-erroring cell is noise —
 *  show a one-line gist. `forceOpen` (running/errored) wins; a user's manual
 *  toggle sticks (later transitions leave it alone once `userToggled`). */
function useCollapsedFirst(forceOpen: boolean): [open: boolean, toggle: () => void] {
  const [collapsed, setCollapsed] = useState(!forceOpen);
  const userToggled = useRef(false);
  useEffect(() => {
    if (!userToggled.current) setCollapsed(!forceOpen);
  }, [forceOpen]);
  const toggle = (): void => {
    if (forceOpen) return;
    userToggled.current = true;
    setCollapsed((v) => !v);
  };
  return [forceOpen || !collapsed, toggle];
}

type CellShellProps = {
  className?: string;
  /** Head icon; defaults to the open/closed chevron. */
  icon?: ReactNode;
  /** Open-state head label (`python · N lines`). */
  lang: ReactNode;
  /** Collapsed-state one-line preview. */
  gist: ReactNode;
  /** Variant-specific right-cluster (bg-chip, detach button, …). */
  chips?: ReactNode;
  /** Renders `.cell-status.err` before `chips` when set. */
  errChip?: string;
  turn: number;
  dur?: number;
  open: boolean;
  /** `undefined` → head is inert (forced open). */
  onToggle?: () => void;
  actions?: ReactNode;
  children: ReactNode;
};

/** The `.code-cell > .cc-head + body` frame both cell variants render into.
 *  `children`/`actions` mount only when `open`. */
function CellShell(p: CellShellProps): JSX.Element {
  const { open, onToggle, dur } = p;
  const cls = `code-cell${open ? "" : " collapsed"}${p.className ? ` ${p.className}` : ""}`;
  return (
    <div className={cls}>
      <div
        className="cc-head"
        {...(onToggle && { role: "button", tabIndex: 0, onClick: onToggle })}
      >
        {p.icon ?? <i className={`bi bi-chevron-${open ? "down" : "right"} cc-chev`} />}
        {open ? (
          <span className="cc-gist cc-lang">{p.lang}</span>
        ) : (
          <code className="cc-gist">{p.gist}</code>
        )}
        {p.errChip && (
          <span className="cell-status err" title={p.errChip}>
            <i className="bi bi-exclamation-triangle-fill" /> error
          </span>
        )}
        {p.chips}
        {dur != null && (
          <span className="cc-dur" title={`ran for ${dur.toFixed(2)}s`}>
            {dur.toFixed(1)}s
          </span>
        )}
        <span className="turn-no">turn {p.turn}</span>
      </div>
      {open && p.children}
      {/* Actions only when expanded — the collapsed head is a single ~28px row
          where an absolute button would collide with `.turn-no`. */}
      {open && p.actions && <BlockActions>{p.actions}</BlockActions>}
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

/** §7: split on identifier boundaries (the regex is capturing, so `split`
 *  interleaves separators and tokens); wrap tokens that are live `user_ns`
 *  keys with a native-tooltip span showing `type · repr`.
 *
 *  False-positive guard: keywords (`for`, `in`) and builtins (`len`, `print`)
 *  are never in `ns` — the former can't be bound, the latter live under
 *  `__builtins__` and are excluded by `_seeded`. Attribute access is the one
 *  real hazard: `df.head` would highlight `head` if the user also has a var
 *  `head`, so skip any token whose preceding separator ends in `.`. */
function tokenizeGist(line: string, ns: NsSummary): ReactNode {
  if (!line) return line;
  const parts = line.split(/(\b[A-Za-z_]\w*\b)/);
  return parts.map((tok, i) => {
    // Odd indices are the captured identifiers; even are separators.
    if (i % 2 === 0) return tok;
    const afterDot = (parts[i - 1] ?? "").endsWith(".");
    const summary = afterDot ? undefined : ns[tok];
    return summary != null ? (
      <span key={i} className="cc-var" title={summary}>
        {tok}
      </span>
    ) : (
      tok
    );
  });
}

function CodeCell({
  turn,
  code,
  running,
  detached,
  errored,
  interrupted,
  background,
  settledBg,
  duration,
  ns,
}: {
  turn: number;
  code: string;
  running: boolean;
  detached: boolean;
  errored: boolean;
  interrupted: boolean;
  background: boolean;
  settledBg: string | undefined;
  duration: number | undefined;
  ns: NsSummary;
}): JSX.Element {
  const send = useSession((s) => s.send);
  const forceOpen = running || errored;
  const [open, toggle] = useCollapsedFirst(forceOpen);
  // `run in background` is only offered on the *live* blocking cell:
  // `running` (ToolEvent still `pending`) implies this is the latest turn —
  // the agent loop can't advance past an unfinished tool call.
  const canDetach = running && !detached && !background;
  const showBgChip = detached || background || settledBg != null;
  const loc = code.trimEnd().split("\n").length;
  return (
    <CellShell
      className={detached || background ? "bg" : undefined}
      lang={`python · ${loc} line${loc === 1 ? "" : "s"}`}
      gist={tokenizeGist(firstNonBlankLine(code), ns)}
      turn={turn}
      dur={duration}
      open={open}
      onToggle={forceOpen ? undefined : toggle}
      actions={<CopyBtn text={code} title="copy code" />}
      errChip={errored && !running && !interrupted ? "cell raised" : undefined}
      chips={
        <>
          {interrupted && (
            <span className="cell-status interrupted" title="interrupted by user">
              interrupted
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
        </>
      }
    >
      <pre>
        <code>{code}</code>
      </pre>
    </CellShell>
  );
}

// ── bash cell (M1-HYBRID.md §`bash` tool) ───────────────────────────────────

function BashCell({ ev, turn }: { ev: ToolEvent; turn: number }): JSX.Element {
  const cmd = typeof ev.arguments.cmd === "string" ? ev.arguments.cmd : "";
  const running = ev.pending === true;
  const errored = ev.error != null;
  const background = ev.arguments.background === true;
  const result = resultText(ev.result);
  const forceOpen = running || errored;
  const [open, toggle] = useCollapsedFirst(forceOpen);
  return (
    <CellShell
      className={`bash-cell${background ? " bg" : ""}`}
      icon={
        <>
          <i className="bi bi-terminal cc-chev" />
          <span className="cc-prompt">$</span>
        </>
      }
      lang={`bash${background ? " · bg" : ""}`}
      gist={firstNonBlankLine(cmd)}
      turn={turn}
      // Non-python tools don't emit `cell_done`, so read wall-clock from the
      // ToolEvent's own timing (`working_time` settles when `pending` clears).
      dur={typeof ev.working_time === "number" ? ev.working_time : undefined}
      open={open}
      onToggle={forceOpen ? undefined : toggle}
      actions={<CopyBtn text={cmd} title="copy command" />}
      errChip={errored && !running ? "command failed" : undefined}
      chips={background && <span className="cc-bg-chip">bg</span>}
    >
      <pre>
        <code>{cmd}</code>
      </pre>
      {(result || ev.error) && (
        <pre className="bash-result">
          {result}
          {ev.error && <span className="err">{ev.error.message}</span>}
        </pre>
      )}
    </CellShell>
  );
}

// ── receipts (file + review tools) ──────────────────────────────────────────

const FILE_VERB: Readonly<Record<string, string>> = {
  read_file: "read",
  write_file: "write",
  edit_file: "edit",
};

/** `read_file`/`write_file`/`edit_file` → one-line `.file-receipt`. No
 *  expandable body — the tool result is the model's context, not the
 *  human's; the path + byte count is enough to follow along. */
function FileReceipt({ ev, turn }: { ev: ToolEvent; turn: number }): JSX.Element {
  const path = typeof ev.arguments.path === "string" ? ev.arguments.path : "?";
  const verb = FILE_VERB[ev.function] ?? ev.function;
  // `write_file` returns `[wrote N bytes → path]`; for read/edit fall back
  // to the argument/result length as a rough size.
  const content = ev.arguments.content;
  const bytes =
    typeof content === "string"
      ? new TextEncoder().encode(content).length
      : resultText(ev.result).length;
  return (
    <div
      className={`file-receipt${ev.error ? " err" : ""}`}
      title={ev.error ? ev.error.message : resultText(ev.result)}
    >
      <i className="bi bi-file-earmark" />
      <span className="fr-verb">{verb}</span>
      <code className="fr-path">{path}</code>
      {ev.pending ? (
        <i className="bi bi-record-fill fx-dot pending" />
      ) : ev.error ? (
        <span className="fr-err">{ev.error.message}</span>
      ) : (
        <span className="fr-meta">{bytes} bytes</span>
      )}
      <span className="turn-no">turn {turn}</span>
    </div>
  );
}

/** `ask_human`/`review_seeds`/`review_finding` — the tool call itself is
 *  just a marker; the real UI is the `GateCard` DisplayEvent that
 *  `kernel.gate → _emit` mounts among the outputs below. */
function ReviewReceipt({ ev, turn }: { ev: ToolEvent; turn: number }): JSX.Element {
  return (
    <div className="file-receipt tool-receipt">
      <i className="bi bi-question-circle" />
      <span className="fr-verb">{ev.function}</span>
      {ev.pending && <i className="bi bi-record-fill fx-dot pending" />}
      <span className="turn-no">turn {turn}</span>
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
