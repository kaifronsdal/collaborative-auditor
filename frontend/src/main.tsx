import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import "./app/globals.css";
import { AppLayout } from "./components/AppLayout";
import { useSessionStore } from "./store/session";
import { installDebugBridge } from "./lib/debugBridge";

installDebugBridge(useSessionStore);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="*" element={<AppLayout />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>
);
