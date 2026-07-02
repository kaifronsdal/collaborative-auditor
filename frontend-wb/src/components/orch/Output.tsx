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
import { useEffect, useMemo, useRef, type JSX } from "react";

import { useSession } from "../../store/session";
import { cards } from "./cards";
import {
  STREAM_MIME,
  WB_MIME,
  type DisplayBundle,
  type WbPayload,
} from "./types";

export type OutputProps = {
  id: string;
  bundle: DisplayBundle;
  meta: Record<string, unknown>;
  stable: boolean;
  /** The owning cell's `ToolEvent` is no longer `pending` — no more
   *  `dh.update()`s will land, so the `live` badge freezes to `updated N×`. */
  settled: boolean;
};

/** Kinds whose fallback shell gets the gate treatment. */
const GATED = new Set(["run_proposal", "cite_proposal", "prompt"]);

export function Output({ id, bundle, meta, stable, settled }: OutputProps): JSX.Element | null {
  const send = useSession((s) => s.send);
  void meta;

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
  if (!stable) return inner;
  return (
    <div className="out-wrap" data-stable data-updates={updates.current}>
      {inner}
      <span
        className={`out-live${settled ? " settled" : ""}`}
        title={settled ? `updated ${updates.current}×` : `live — updated ${updates.current}×`}
      >
        {settled ? `updated ${updates.current}×` : "live"}
      </span>
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
    // `kernel._settle` emits `{kind:"traceback", ename, text}` as a display so
    // the error surfaces before `ToolEvent.error` settles. Render it directly
    // (no card module for a two-field payload) rather than the JSON fallback.
    if (wb.kind === "traceback") {
      return (
        <div className="out traceback" data-display-id={id}>
          <div className="out-head">
            <i className="bi bi-exclamation-triangle" />
            <span className="out-kind">{String(wb.ename ?? "error")}</span>
          </div>
          <pre className="out-plain err">{String(wb.text ?? "")}</pre>
        </div>
      );
    }
    const Card = cards[wb.kind];
    return Card ? (
      <Card payload={wb} displayId={id} send={send} />
    ) : (
      <WbFallback id={id} payload={wb} />
    );
  }

  const html = bundle["text/html"];
  if (html != null) return <HtmlOutput html={html} />;

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
    return (
      <div className="out bare">
        <pre className="out-plain">{text}</pre>
      </div>
    );
  }

  return null;
}

/** Post-process pandas/Markdown HTML: linkify bare `a-xxxx` audit ids so any
 *  DataFrame column becomes clickable without a custom styler (M1-NOTEBOOK.md
 *  §Open question — frontend regex for M1.0). */
const AUDIT_ID_RE = />(a-[0-9a-f]{4,})</g;

/** plotly.js attaches `.on(event, cb)` to the graph div once `Plotly.newPlot`
 *  has run on it. `window.Plotly` is bundled in main.tsx. */
type PlotlyDiv = HTMLDivElement & {
  on: (ev: string, cb: (d: PlotlyClick) => void) => void;
  removeAllListeners?: (ev: string) => void;
};
type PlotlyClick = { points: Array<{ customdata?: unknown[] }> };

function HtmlOutput({ html }: { html: string }): JSX.Element {
  const send = useSession((s) => s.send);
  const hostRef = useRef<HTMLDivElement>(null);
  const isPlotly = html.includes("plotly-graph-div");
  const processed = useMemo(
    () =>
      isPlotly
        ? html
        : html.replace(
            AUDIT_ID_RE,
            (_, id: string) => `><a class="qref" href="wb://audit/${id}">${id}</a><`
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
  // `plotly_click` → `customdata[0]` (the audit id the workbench styler
  // stashes per-point) navigates the desk.
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
          const raw = String(d.points[0]?.customdata?.[0] ?? "");
          const bare = raw.replace(/^wb:\/\/[^/]+\//, "");
          if (!bare) return;
          send({ t: "import_running", sample_id: bare });
        });
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
  return (
    <div className="out html" dangerouslySetInnerHTML={{ __html: processed }} />
  );
}

/** Placeholder for kinds without a card yet — shows gate chrome + raw payload. */
function WbFallback({ id, payload }: { id: string; payload: WbPayload }): JSX.Element {
  const gated = GATED.has(payload.kind) && payload.pending !== false;
  return (
    <div className={`out${gated ? " gated" : ""}`} data-display-id={id}>
      <div className="out-head">
        <span className="out-kind">{payload.kind}</span>
        {gated && <span className="gate-tag">proposed</span>}
      </div>
      {typeof payload.description === "string" && (
        <div className="gate-desc">{payload.description}</div>
      )}
      <pre className="out-plain">{JSON.stringify(payload, null, 2)}</pre>
    </div>
  );
}
