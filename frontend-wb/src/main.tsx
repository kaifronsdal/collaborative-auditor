import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ComponentStateProvider } from "@tsmono/react/state";
// Bundle plotly instead of relying on the `notebook_connected` CDN loader —
// the kernel emits `<script src="https://cdn.plot.ly/...">` on the first
// figure, but we skip remote-src scripts in HtmlOutput and mount the bundled
// module on `window` so the inline `Plotly.newPlot(...)` calls resolve.
import Plotly from "plotly.js-basic-dist-min";

import { App } from "./App";
import { componentStateHooks } from "./lib/inspectState";
import "bootstrap-icons/font/bootstrap-icons.css";
import "./styles.css";

(window as { Plotly?: unknown }).Plotly = Plotly;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ComponentStateProvider hooks={componentStateHooks}>
      <App />
    </ComponentStateProvider>
  </StrictMode>
);
