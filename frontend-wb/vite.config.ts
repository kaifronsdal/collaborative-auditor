/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Proxy /ws/* to the backend so only the Vite port needs forwarding.
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  test: {
    globals: true,
    environment: "node",
  },
});
