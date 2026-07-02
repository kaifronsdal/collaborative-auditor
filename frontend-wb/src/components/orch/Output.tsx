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
};

/** Kinds whose fallback shell gets the gate treatment. */
const GATED = new Set(["run_proposal", "cite_proposal", "prompt"]);

export function Output({ id, bundle, meta, stable }: OutputProps): JSX.Element | null {
  const send = useSession((s) => s.send);
  void meta;

  const wb = bundle[WB_MIME];
  if (wb != null) {
    const Card = cards[wb.kind];
    return Card ? (
      <Card payload={wb} displayId={id} send={send} />
    ) : (
      <WbFallback id={id} payload={wb} />
    );
  }

  const html = bundle["text/html"];
  if (html != null) return <HtmlOutput html={html} stable={stable} />;

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

/** plotly.js attaches `.on(event, cb)` to the graph div; `window.Plotly` is
 *  loaded from CDN by the first `notebook_connected` output's script tag. */
type PlotlyDiv = HTMLDivElement & {
  on: (ev: string, cb: (d: PlotlyClick) => void) => void;
  removeAllListeners?: (ev: string) => void;
};
type PlotlyClick = { points: Array<{ customdata?: unknown[] }> };
declare global {
  interface Window {
    Plotly?: { react: (el: Element, ...a: unknown[]) => void };
  }
}

function HtmlOutput({ html, stable }: { html: string; stable: boolean }): JSX.Element {
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

  // M1-PLOTTING: `dangerouslySetInnerHTML` inserts plotly's `<script>` tags
  // inertly, so the figure never mounts. Re-execute them (this runs
  // `Plotly.newPlot(divId, data, layout, config)` — or the CDN loader on the
  // first plot), then wire `plotly_click` so `customdata[0]` (the audit id
  // the workbench styler stashes per-point) navigates the desk.
  useEffect(() => {
    if (!isPlotly) return;
    const host = hostRef.current;
    if (!host) return;
    // Run each embedded script by cloning it into a fresh <script> node —
    // browsers only execute scripts that are *created*, not innerHTML'd.
    let cdn: HTMLScriptElement | null = null;
    for (const s of Array.from(host.querySelectorAll("script"))) {
      const live = document.createElement("script");
      for (const { name, value } of Array.from(s.attributes)) live.setAttribute(name, value);
      live.textContent = s.textContent;
      s.replaceWith(live);
      if (live.src.includes("plotly")) cdn = live;
    }
    const gd = host.querySelector<PlotlyDiv>(".plotly-graph-div");
    const attachClick = (): void => {
      if (!gd) return;
      gd.on("plotly_click", (d) => {
        const raw = String(d.points[0]?.customdata?.[0] ?? "");
        const bare = raw.replace(/^wb:\/\/[^/]+\//, "");
        if (!bare) return;
        send({ t: "import_running", sample_id: bare });
      });
    };
    if (window.Plotly == null) {
      // CDN not loaded yet — wire the click once it lands.
      if (cdn) cdn.onload = () => attachClick();
    } else {
      attachClick();
    }
    return () => gd?.removeAllListeners?.("plotly_click");
  }, [isPlotly, html, send]);

  return (
    <div
      ref={hostRef}
      className={`out ${isPlotly ? "bare plotly-host" : "html"}`}
      {...(stable ? { "data-display-id": "stable" } : {})}
      dangerouslySetInnerHTML={{ __html: processed }}
    />
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
