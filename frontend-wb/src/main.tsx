import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ComponentStateProvider } from "@tsmono/react/state";

import { App } from "./App";
import { componentStateHooks } from "./lib/inspectState";
import "bootstrap-icons/font/bootstrap-icons.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ComponentStateProvider hooks={componentStateHooks}>
      <App />
    </ComponentStateProvider>
  </StrictMode>
);
