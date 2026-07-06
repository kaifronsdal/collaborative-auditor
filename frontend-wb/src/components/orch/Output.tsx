/**
 * `<Output>` — one kernel display, dispatched on MIME (M1-NOTEBOOK.md
 * §Renderer table).
 *
 * Precedence: vendor `application/vnd.workbench.v1+json` (rich card) →
 * `text/html` → `image/svg+xml` → `image/png` → jupyter stream → `text/plain`.
 * Rich cards live in `./cards/` and are dispatched from the `cards` dict by
 * `payload.kind`; unknown kinds fall through to `WbFallback` so the column
 * stays inspectable.
 */
import { useEffect, useMemo, useRef, useState, type JSX } from "react";
import { marked } from "marked";

import { useSession } from "../../store/session";
import { BlockActions, CopyBtn, tableToTsv } from "./BlockActions";
import { cards } from "./cards";
import {
  STREAM_MIME,
  WB_MIME,
  type DisplayBundle,
  type TracebackPayload,
  type WbPayload,
} from "./types";

export type OutputProps = {
  id: string;
  bundle: DisplayBundle;
  stable: boolean;
  /** The owning cell's `ToolEvent` is no longer `pending` — no more
   *  `dh.update()`s will land, so the `live` badge freezes to `updated N×`. */
  settled: boolean;
};

/** Kinds whose fallback shell gets the gate treatment. */
const GATED = new Set(["run_proposal", "cite_proposal", "prompt"]);

/** §6: pick the most useful clipboard representation of a bundle. Thunked so
 *  DataFrame TSV / JSON.stringify only run on click. */
function bundleCopyText(bundle: DisplayBundle): () => string {
  return () => {
    const html = bundle["text/html"];
    if (html != null && html.includes('class="dataframe"')) return tableToTsv(html);
    if (bundle["text/markdown"] != null) return bundle["text/markdown"];
    if (bundle[STREAM_MIME] != null) return bundle[STREAM_MIME].text;
    if (bundle["text/plain"] != null) return bundle["text/plain"];
    if (bundle[WB_MIME] != null) return JSON.stringify(bundle[WB_MIME], null, 2);
    if (html != null) return html;
    return JSON.stringify(bundle, null, 2);
  };
}

export function Output({ id, bundle, stable, settled }: OutputProps): JSX.Element | null {
  const send = useSession((s) => s.send);

  // §15: count `dh.update()`s. The store's `update` reducer replaces the
  // `InfoEvent` in place by uuid, so `outputs` only ever holds the latest
  // bundle — but this component is stable-keyed by that uuid, so each update
  // arrives here as a new `bundle` reference. Count reference changes.
  const updates = useRef(-1);
  const lastBundle = useRef<DisplayBundle | null>(null);
  if (lastBundle.current !== bundle) {
    lastBundle.current = bundle;
    updates.current += 1;
  }

  const inner = renderBundle(id, bundle, send);
  if (inner == null) return null;
  // Gated cards have interactive controls → self-evidently live; no icon row
  // (its bottom-right is the approve/deny bar) and no `live` badge.
  const wb = bundle[WB_MIME];
  if (wb != null && "pending" in wb && wb.pending) return inner;
  const actions = (
    <BlockActions>
      <CopyBtn text={bundleCopyText(bundle)} />
    </BlockActions>
  );
  // UI-AUDIT §C: only badge stable outputs while their cell is still running;
  // once settled, no more updates can land, so drop the badge entirely.
  if (!stable || settled) {
    return (
      <div className="ba-host">
        {inner}
        {actions}
      </div>
    );
  }
  return (
    <div className="out-wrap ba-host" data-stable data-updates={updates.current}>
      {inner}
      <span className="out-live" title={`live — updated ${updates.current}×`}>
        live
      </span>
      {actions}
    </div>
  );
}

function renderBundle(
  id: string,
  bundle: DisplayBundle,
  send: ReturnType<typeof useSession.getState>["send"]
): JSX.Element | null {
  const wb = bundle[WB_MIME];
  if (wb != null) {
    if (wb.kind === "traceback") return <TracebackCard id={id} wb={wb} />;
    const Card = cards[wb.kind];
    return Card ? (
      <Card payload={wb} displayId={id} send={send} />
    ) : (
      <WbFallback id={id} payload={wb} />
    );
  }

  const html = bundle["text/html"];
  if (html != null) return <HtmlOutput html={html} />;

  // `display(Markdown(…))` — computed prose. Same treatment as `.asst-prose`.
  const md = bundle["text/markdown"];
  if (md != null) {
    return (
      <div
        className="out bare asst-prose md"
        dangerouslySetInnerHTML={{ __html: marked.parse(md, { async: false }) }}
      />
    );
  }

  const svg = bundle["image/svg+xml"];
  if (svg != null) {
    // TODO(M1-PLOTTING): delegated click handler on `a[*|href^="wb://"]`.
    return (
      <div className="out bare" dangerouslySetInnerHTML={{ __html: svg }} />
    );
  }

  const png = bundle["image/png"];
  if (png != null) {
    return (
      <div className="out bare">
        <img src={`data:image/png;base64,${png}`} alt="" style={{ maxWidth: "100%" }} />
      </div>
    );
  }

  const stream = bundle[STREAM_MIME];
  if (stream != null) {
    return (
      <pre className={`out-stream${stream.name === "stderr" ? " err" : ""}`}>
        {stream.text}
      </pre>
    );
  }

  const text = bundle["text/plain"];
  if (text != null) {
    // Last-expr repr — accent left-rail (grey rail = print, red = stderr,
    // accent = returned value). `.out-result` opts it out of the 20lh cap.
    return (
      <div className="out bare">
        <pre className="out-plain out-result">{text}</pre>
      </div>
    );
  }

  return null;
}

// ── traceback ───────────────────────────────────────────────────────────────

/** `kernel._settle` emits `{kind:"traceback", ename, evalue, frames, text}`
 *  as a display so the error surfaces before `ToolEvent.error` settles.
 *  `frames` is `traceback.extract_tb` filtered to user frames; `text` is the
 *  full formatted traceback for the expandable body. */
function TracebackCard({ id, wb }: { id: string; wb: TracebackPayload }): JSX.Element {
  const [open, setOpen] = useState(false);
  const { ename, evalue, frames, text } = wb;
  const last = frames[frames.length - 1];
  // IPython names the synthetic file `<ipython-input-N-hash>` — noise here.
  const at = last && last.file.replace(/^<ipython-input-[^>]*>$/, "cell");
  return (
    <div className="out traceback" data-display-id={id}>
      <div className="out-head hstack g8">
        <i className="bi bi-exclamation-triangle" />
        <span className="out-kind">{ename}</span>
      </div>
      {evalue != null && last ? (
        <>
          <div className="tb-body">
            <div className="tb-evalue">{evalue}</div>
            <div className="tb-at-row">
              <code className="tb-at truncate">
                at {at}:{last.lineno} · {last.line}
              </code>
              <button
                type="button"
                className="tb-toggle"
                onClick={() => setOpen((v) => !v)}
              >
                <i className={`bi bi-chevron-${open ? "up" : "down"}`} />{" "}
                {frames.length} frame{frames.length === 1 ? "" : "s"}
              </button>
            </div>
          </div>
          {open && <pre className="out-plain err">{text}</pre>}
        </>
      ) : (
        <pre className="out-plain err">{text}</pre>
      )}
    </div>
  );
}

// ── html (pandas / plotly) ──────────────────────────────────────────────────

/** Post-process pandas/Markdown HTML: linkify bare `a-xxxx` audit ids so any
 *  DataFrame column becomes clickable without a custom styler (M1-NOTEBOOK.md
 *  §Open question — frontend regex for M1.0). Match whole `<td>` cells only:
 *  the `>` is consumed, the closing `<` is a lookahead so `</td>` stays. */
const AUDIT_ID_RE = />(a-[0-9a-f]{4})(?=<)/g;

/** plotly.js attaches `.on(event, cb)` to the graph div once `Plotly.newPlot`
 *  has run on it. `window.Plotly` is bundled in main.tsx. */
type PlotlyDiv = HTMLDivElement & {
  on: (ev: string, cb: (d: PlotlyClick) => void) => void;
  removeAllListeners?: (ev: string) => void;
  /** Trace array `Plotly.newPlot` stores on the div. */
  data?: Array<{ customdata?: unknown }>;
};
type PlotlyClick = { points: Array<{ customdata?: unknown[] }> };

function HtmlOutput({ html }: { html: string }): JSX.Element {
  const send = useSession((s) => s.send);
  const hostRef = useRef<HTMLDivElement>(null);
  const isPlotly = html.includes("plotly-graph-div");
  const isDF = html.includes('class="dataframe"');
  const processed = useMemo(
    () =>
      isPlotly
        ? html
        : html.replace(
            AUDIT_ID_RE,
            (_, id: string) => `><a class="qref" href="wb://audit/${id}">${id}</a>`
          ),
    [html, isPlotly]
  );

  // M1-PLOTTING: for plotly we own `innerHTML` (not `dangerouslySetInnerHTML`)
  // so React never wipes the mounted chart on a parent re-render, and so
  // scripts execute — innerHTML'd `<script>` tags are inert, so we clone each
  // into a fresh node. Skip remote `src` scripts (the plotly CDN loader,
  // MathJax): we bundle `plotly.js-basic-dist-min` in main.tsx and mount it on
  // `window`, so the inline `Plotly.newPlot(divId, …)` script resolves without
  // the network fetch the screenshot harness can't make. Then wire
  // `plotly_click`: `customdata` per point is `[wb://audit/{id}, log]` when
  // `plots.link(log=…)` ran (2-col) — the `.eval` path lets us
  // `{t:"import"}` a *finished* sample. 1-col customdata (no `log`) can't
  // locate the file → warn and no-op.
  //
  // §17: `Plotly.newPlot` is synchronous and heavy, so paint a
  // `.plotly-loading` placeholder first, yield one frame via rAF, then mount.
  // Re-fires on `html` change (a stable figure that's `dh.update()`d).
  useEffect(() => {
    if (!isPlotly) return;
    const host = hostRef.current;
    if (!host) return;
    host.innerHTML = '<div class="plotly-loading">rendering figure…</div>';
    const raf = requestAnimationFrame(() => {
      host.innerHTML = html;
      for (const s of Array.from(host.querySelectorAll("script"))) {
        if (s.src) continue;
        const live = document.createElement("script");
        live.textContent = s.textContent;
        s.replaceWith(live);
      }
      const gd = host.querySelector<PlotlyDiv>(".plotly-graph-div");
      // `.on` is only patched onto the div after `Plotly.newPlot` runs (which
      // the inline script above does synchronously) — guard in case the bundle
      // hasn't attached it yet.
      if (gd && typeof gd.on === "function") {
        gd.on("plotly_click", (d) => {
          const cd = d.points[0]?.customdata;
          const ref = String(cd?.[0] ?? "");
          const log = cd?.[1];
          const sampleId = ref.replace(/^wb:\/\/[^/]+\//, "");
          if (!sampleId) return;
          if (typeof log !== "string" || !log) {
            console.warn(
              "plotly_click: customdata has no log path — call wb.plots.link(fig, ids, log=…)"
            );
            return;
          }
          send({ t: "import", path: log, sample_id: sampleId });
        });
        // UI-AUDIT §C: only advertise the click affordance when at least one
        // trace actually carries `customdata` (i.e. the workbench styler ran).
        if (gd.data?.some((t) => t.customdata)) {
          host.insertAdjacentHTML(
            "beforeend",
            '<div class="fx-more hstack g8 plotly-hint">click a point to open in auditor</div>'
          );
        }
      }
    });
    return () => {
      cancelAnimationFrame(raf);
      host
        .querySelector<PlotlyDiv>(".plotly-graph-div")
        ?.removeAllListeners?.("plotly_click");
    };
  }, [isPlotly, html, send]);

  if (isPlotly) {
    return <div ref={hostRef} className="out bare plotly-host" />;
  }
  // Pandas tables self-delimit — no `.out.html` box, just horizontal scroll.
  if (isDF) {
    return (
      <div
        className="out bare"
        style={{ overflowX: "auto" }}
        dangerouslySetInnerHTML={{ __html: processed }}
      />
    );
  }
  return (
    <div className="out html" dangerouslySetInnerHTML={{ __html: processed }} />
  );
}

/** Placeholder for kinds without a card yet — loud so it gets noticed. */
function WbFallback({ id, payload }: { id: string; payload: WbPayload }): JSX.Element {
  const gated =
    GATED.has(payload.kind) && "pending" in payload && payload.pending !== false;
  return (
    <div className={`out${gated ? " gated" : ""}`} data-display-id={id}>
      <div className="out-head hstack g8">
        <span className="out-kind">{payload.kind}</span>
        {gated && <span className="gate-tag">proposed</span>}
      </div>
      <div className="err">no renderer for kind={payload.kind}</div>
      <pre className="out-plain">{JSON.stringify(payload, null, 2)}</pre>
    </div>
  );
}
